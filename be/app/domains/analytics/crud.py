"""Analytics domain CRUD — ``text()`` SQL + async functions (portable).

Read-side aggregations for the dashboard plus the write-side used by the ingest /
reprocess / menu / suggestions services. All SQL is dialect-agnostic (sqlite in tests,
postgres in prod), mirroring ``be.app.domains.inventory.crud``:

* unqualified table names (``analytics_*``);
* surrogate ids allocated app-side (``SELECT COALESCE(MAX(id),0)+1``);
* JSON columns stored as TEXT and parsed here; booleans as INTEGER 0/1;
* money/quantity bound as text through ``CAST(:x AS numeric)`` (asyncpg-safe);
* timestamps/dates written as ISO strings from Python (no ``now()``);
* upserts are select-then-insert/update (no ``ON CONFLICT``);
* set membership expands to named binds (no ``ANY(...)``);
* the day-of-week / hour heatmap keys are pre-computed at ingest, so read queries
  need no ``EXTRACT``/``AT TIME ZONE`` and stay portable.

VENUE SCOPING: sales / ingestions / processing_runs / suggestions are venue-filtered;
the ``analytics_menu`` / ``analytics_menu_items`` catalog is company-level (venue tag
carried for uniformity).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from be.app.domains.analytics.timeutil import now_iso


def _num_str(value: Any) -> str | None:
    """Bind a numeric as text for ``CAST(:x AS numeric)``; ``None`` stays ``None``."""
    if value is None or isinstance(value, bool):
        return None
    return str(value)


def _fnum(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round2(value: Any) -> float:
    return round(_fnum(value), 2)


def dump_json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def load_json(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


async def _next_id(session: AsyncSession, table: str) -> int:
    stmt = text(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")  # noqa: S608 - fixed literal
    row = (await session.execute(stmt)).mappings().first()
    return int(row["n"]) if row is not None else 1


# --------------------------------------------------------------------------- #
# Shared read filter (venue + date range + category + noise exclusion)
# --------------------------------------------------------------------------- #


def _filter(
    *,
    venue_id: str,
    from_date: str | None,
    to_date: str | None,
    categories: list[str] | None,
    exclude_dish_type: str | None,
) -> tuple[str, dict[str, Any]]:
    where = ["venue_id = :venue_id"]
    params: dict[str, Any] = {"venue_id": venue_id}
    if from_date:
        where.append("order_date >= :from_date")
        params["from_date"] = from_date
    if to_date:
        where.append("order_date <= :to_date")
        params["to_date"] = to_date
    cats = [c for c in (categories or []) if c]
    if cats:
        names = [f":cat{i}" for i in range(len(cats))]
        where.append(f"dish_type IN ({', '.join(names)})")
        for i, c in enumerate(cats):
            params[f"cat{i}"] = c
    if exclude_dish_type:
        where.append("(dish_type IS NULL OR dish_type <> :excl)")
        params["excl"] = exclude_dish_type
    return " AND ".join(where), params


# --------------------------------------------------------------------------- #
# Read aggregations
# --------------------------------------------------------------------------- #


async def get_categories(
    session: AsyncSession, *, venue_id: str, exclude_dish_type: str | None = None
) -> list[dict]:
    clause, params = _filter(
        venue_id=venue_id, from_date=None, to_date=None,
        categories=None, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608 - clause built from fixed fragments + named binds
        f"SELECT dish_type, COUNT(*) AS n FROM analytics_sales "
        f"WHERE {clause} AND dish_type IS NOT NULL "
        f"GROUP BY dish_type ORDER BY n DESC, dish_type"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [{"dish_type": r["dish_type"], "n": int(r["n"] or 0)} for r in rows]


async def _summary_window(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    categories: list[str] | None, exclude_dish_type: str | None,
) -> dict:
    clause, params = _filter(
        venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608
        "SELECT COALESCE(SUM(sale_price), 0) AS revenue, "
        "COUNT(DISTINCT order_id) AS orders, "
        "COALESCE(SUM(qty), 0) AS items_sold, "
        "COALESCE(SUM(cost_sum), 0) AS cost "
        f"FROM analytics_sales WHERE {clause}"
    )
    row = (await session.execute(sql, params)).mappings().one()
    revenue = _round2(row["revenue"])
    orders = int(row["orders"] or 0)
    margin = _round2(_fnum(row["revenue"]) - _fnum(row["cost"]))
    return {
        "revenue": revenue,
        "orders": orders,
        "items_sold": _round2(row["items_sold"]),
        "avg_bill": _round2(revenue / orders) if orders else 0.0,
        "gross_margin": margin,
        "gross_margin_pct": _round2(100.0 * margin / revenue) if revenue else 0.0,
    }


async def get_summary(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    categories: list[str] | None = None, exclude_dish_type: str | None = None,
) -> dict:
    out = await _summary_window(
        session, venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    # Preceding equal-length window for period-over-period deltas (inclusive range).
    try:
        f = date.fromisoformat(from_date)
        t = date.fromisoformat(to_date)
    except ValueError:
        return out
    span = (t - f).days + 1
    prev_to = f - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span - 1)
    prev = await _summary_window(
        session, venue_id=venue_id, from_date=prev_from.isoformat(),
        to_date=prev_to.isoformat(), categories=categories,
        exclude_dish_type=exclude_dish_type,
    )
    prev["from"] = prev_from.isoformat()
    prev["to"] = prev_to.isoformat()
    out["prev"] = prev
    return out


def _period_key(order_date: str, granularity: str) -> str:
    try:
        d = date.fromisoformat(order_date)
    except (TypeError, ValueError):
        return order_date or ""
    if granularity == "month":
        return f"{d.year:04d}-{d.month:02d}"
    if granularity == "week":
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat()
    return d.isoformat()


async def get_timeseries(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    granularity: str, categories: list[str] | None = None,
    exclude_dish_type: str | None = None,
) -> list[dict]:
    clause, params = _filter(
        venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608
        "SELECT order_date, sale_price, order_id "
        f"FROM analytics_sales WHERE {clause}"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = _period_key(r["order_date"], granularity)
        b = buckets.setdefault(key, {"revenue": 0.0, "orders": set()})
        b["revenue"] += _fnum(r["sale_price"])
        if r["order_id"] is not None:
            b["orders"].add(r["order_id"])
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        orders = len(b["orders"])
        revenue = round(b["revenue"], 2)
        out.append({
            "period": key,
            "revenue": revenue,
            "orders": orders,
            "avg_bill": round(revenue / orders, 2) if orders else 0.0,
        })
    return out


async def get_category_mix(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    categories: list[str] | None = None, exclude_dish_type: str | None = None,
) -> list[dict]:
    clause, params = _filter(
        venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608
        "SELECT dish_type, COALESCE(SUM(sale_price), 0) AS revenue, "
        "COALESCE(SUM(qty), 0) AS qty, COUNT(DISTINCT order_id) AS orders "
        f"FROM analytics_sales WHERE {clause} GROUP BY dish_type ORDER BY revenue DESC"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [
        {
            "dish_type": r["dish_type"] if r["dish_type"] is not None else "—",
            "revenue": _round2(r["revenue"]),
            "qty": _round2(r["qty"]),
            "orders": int(r["orders"] or 0),
        }
        for r in rows
    ]


async def _menu_clean_names(session: AsyncSession, *, venue_id: str) -> dict[tuple, str]:
    rows = (await session.execute(
        text("SELECT pos_name, price, clean_name FROM analytics_menu WHERE venue_id = :v"),
        {"v": venue_id},
    )).mappings().all()
    return {(r["pos_name"], _fnum(r["price"])): (r["clean_name"] or "") for r in rows}


async def get_top_dishes(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    limit: int, categories: list[str] | None = None,
    exclude_dish_type: str | None = None,
) -> list[dict]:
    clause, params = _filter(
        venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608
        "SELECT dish_name, menu_price, dish_type, qty, sale_price, components "
        f"FROM analytics_sales WHERE {clause} AND dish_name IS NOT NULL"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    clean_map = await _menu_clean_names(session, venue_id=venue_id)
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        pieces = sum(
            int(c.get("count") or 0)
            for c in load_json(r["components"]) if isinstance(c, dict)
        )
        clean = clean_map.get((r["dish_name"], _fnum(r["menu_price"]))) or r["dish_name"]
        label = f"{clean} ({pieces}pc)" if pieces > 0 else clean
        g = grouped.setdefault(label, {
            "dish_name": label, "pos_name": r["dish_name"],
            "dish_type": r["dish_type"] or "—", "qty": 0.0, "revenue": 0.0,
        })
        g["qty"] += _fnum(r["qty"])
        g["revenue"] += _fnum(r["sale_price"])
    out = sorted(grouped.values(), key=lambda x: x["revenue"], reverse=True)[:limit]
    for g in out:
        g["qty"] = round(g["qty"], 2)
        g["revenue"] = round(g["revenue"], 2)
    return out


async def get_component_mix(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    categories: list[str] | None = None, exclude_dish_type: str | None = None,
) -> list[dict]:
    """Explode the generic ``components`` JSON array, weighted by line quantity."""
    clause, params = _filter(
        venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608
        f"SELECT qty, components FROM analytics_sales WHERE {clause}"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    totals: dict[str, float] = {}
    for r in rows:
        qty = _fnum(r["qty"]) or 1.0
        for c in load_json(r["components"]):
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            if not name:
                continue
            totals[str(name)] = totals.get(str(name), 0.0) + _fnum(c.get("count")) * qty
    return [
        {"name": name, "count": round(totals[name], 2)}
        for name in sorted(totals, key=lambda k: totals[k], reverse=True)
    ]


async def get_heatmap(
    session: AsyncSession, *, venue_id: str, from_date: str, to_date: str,
    categories: list[str] | None = None, exclude_dish_type: str | None = None,
) -> list[dict]:
    clause, params = _filter(
        venue_id=venue_id, from_date=from_date, to_date=to_date,
        categories=categories, exclude_dish_type=exclude_dish_type,
    )
    sql = text(  # noqa: S608
        "SELECT order_dow AS dow, order_hour_int AS hour, "
        "COUNT(DISTINCT order_id) AS orders, COALESCE(SUM(sale_price), 0) AS revenue "
        f"FROM analytics_sales WHERE {clause} "
        "AND order_dow IS NOT NULL AND order_hour_int IS NOT NULL "
        "GROUP BY order_dow, order_hour_int ORDER BY order_dow, order_hour_int"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    return [
        {
            "dow": int(r["dow"]),
            "hour": int(r["hour"]),
            "orders": int(r["orders"] or 0),
            "revenue": _round2(r["revenue"]),
        }
        for r in rows
    ]


async def get_ingestions(session: AsyncSession, *, venue_id: str, limit: int) -> list[dict]:
    rows = (await session.execute(
        text(
            "SELECT id, source, filename, rows_inserted, rows_skipped, date_min, "
            "date_max, status, error, file_path, created_at "
            "FROM analytics_ingestions WHERE venue_id = :v "
            "ORDER BY created_at DESC, id DESC LIMIT :lim"
        ),
        {"v": venue_id, "lim": limit},
    )).mappings().all()
    return [
        {
            "id": r["id"], "source": r["source"], "filename": r["filename"],
            "rows_inserted": r["rows_inserted"], "rows_skipped": r["rows_skipped"],
            "date_min": r["date_min"], "date_max": r["date_max"],
            "status": r["status"], "error": r["error"],
            "created_at": r["created_at"], "has_file": bool(r["file_path"]),
        }
        for r in rows
    ]


async def get_ingestion_file(session: AsyncSession, *, venue_id: str, ing_id: int) -> dict | None:
    row = (await session.execute(
        text(
            "SELECT file_path, filename FROM analytics_ingestions "
            "WHERE id = :id AND venue_id = :v"
        ),
        {"id": ing_id, "v": venue_id},
    )).mappings().first()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Menu catalog (company-level)
# --------------------------------------------------------------------------- #


async def menu_lookup(session: AsyncSession, *, venue_id: str) -> dict[tuple, dict]:
    """Return ``{(pos_name, price): {clean_name, components}}`` for enrichment."""
    rows = (await session.execute(
        text("SELECT pos_name, price, clean_name, components FROM analytics_menu "
             "WHERE venue_id = :v"),
        {"v": venue_id},
    )).mappings().all()
    return {
        (r["pos_name"], _fnum(r["price"])): {
            "clean_name": r["clean_name"] or "",
            "components": load_json(r["components"]),
        }
        for r in rows
    }


async def upsert_menu(
    session: AsyncSession, *, venue_id: str, pos_name: str, price: float,
    clean_name: str, components: list[dict],
) -> None:
    now = now_iso()
    exists = (await session.execute(
        text("SELECT 1 FROM analytics_menu WHERE venue_id = :v AND pos_name = :p "
             "AND price = CAST(:pr AS NUMERIC)"),
        {"v": venue_id, "p": pos_name, "pr": _num_str(price)},
    )).first()
    if exists:
        await session.execute(
            text("UPDATE analytics_menu SET clean_name = :cn, components = :c, "
                 "updated_at = :ts WHERE venue_id = :v AND pos_name = :p "
                 "AND price = CAST(:pr AS NUMERIC)"),
            {"cn": clean_name, "c": dump_json(components), "ts": now,
             "v": venue_id, "p": pos_name, "pr": _num_str(price)},
        )
    else:
        await session.execute(
            text("INSERT INTO analytics_menu (venue_id, pos_name, price, clean_name, "
                 "components, created_at, updated_at) VALUES (:v, :p, "
                 "CAST(:pr AS NUMERIC), :cn, :c, :ts, :ts)"),
            {"v": venue_id, "p": pos_name, "pr": _num_str(price), "cn": clean_name,
             "c": dump_json(components), "ts": now},
        )


async def rematerialize_components(session: AsyncSession, *, venue_id: str) -> int:
    """Re-derive ``components`` on sales rows that now resolve to a catalog entry."""
    menu = await menu_lookup(session, venue_id=venue_id)
    rows = (await session.execute(
        text("SELECT id, dish_name, menu_price, components FROM analytics_sales "
             "WHERE venue_id = :v"),
        {"v": venue_id},
    )).mappings().all()
    updated = 0
    for r in rows:
        current = load_json(r["components"])
        if current:
            continue
        m = menu.get((r["dish_name"], _fnum(r["menu_price"])))
        if not m or not m["components"]:
            continue
        await session.execute(
            text("UPDATE analytics_sales SET components = :c WHERE id = :id"),
            {"c": dump_json(m["components"]), "id": r["id"]},
        )
        updated += 1
    return updated


# --------------------------------------------------------------------------- #
# Ingest / raw / sales writes
# --------------------------------------------------------------------------- #


async def row_hash_exists(session: AsyncSession, *, table: str, row_hash: str) -> bool:
    stmt = text(f"SELECT 1 FROM {table} WHERE row_hash = :h")  # noqa: S608 - fixed literal
    return (await session.execute(stmt, {"h": row_hash})).first() is not None


async def insert_raw_row(session: AsyncSession, *, venue_id: str, row: dict, source: str) -> int:
    rid = await _next_id(session, "analytics_sales_raw")
    await session.execute(
        text(
            "INSERT INTO analytics_sales_raw (id, venue_id, order_id, order_date, "
            "order_ts, order_dow, order_hour_int, dish_type, dish_name, menu_price, "
            "discount, sale_price, qty, waiter_name, diners, cost, cost_sum, components, "
            "source, row_hash, ingested_at) VALUES (:id, :v, :order_id, :order_date, "
            ":order_ts, :order_dow, :order_hour_int, :dish_type, :dish_name, "
            "CAST(:menu_price AS NUMERIC), CAST(:discount AS NUMERIC), "
            "CAST(:sale_price AS NUMERIC), CAST(:qty AS NUMERIC), :waiter_name, "
            ":diners, CAST(:cost AS NUMERIC), CAST(:cost_sum AS NUMERIC), :components, "
            ":source, :row_hash, :ts)"
        ),
        _row_params(rid, venue_id, row, source),
    )
    return rid


async def insert_sales_row(
    session: AsyncSession, *, venue_id: str, row: dict, source: str,
    raw_id: int | None, components: list[dict],
) -> int:
    sid = await _next_id(session, "analytics_sales")
    params = _row_params(sid, venue_id, row, source)
    params["components"] = dump_json(components)
    params["raw_id"] = raw_id
    await session.execute(
        text(
            "INSERT INTO analytics_sales (id, venue_id, order_id, order_date, order_ts, "
            "order_dow, order_hour_int, dish_type, dish_name, menu_price, discount, "
            "sale_price, qty, waiter_name, diners, cost, cost_sum, components, raw_id, "
            "source, row_hash, ingested_at) VALUES (:id, :v, :order_id, :order_date, "
            ":order_ts, :order_dow, :order_hour_int, :dish_type, :dish_name, "
            "CAST(:menu_price AS NUMERIC), CAST(:discount AS NUMERIC), "
            "CAST(:sale_price AS NUMERIC), CAST(:qty AS NUMERIC), :waiter_name, "
            ":diners, CAST(:cost AS NUMERIC), CAST(:cost_sum AS NUMERIC), :components, "
            ":raw_id, :source, :row_hash, :ts)"
        ),
        params,
    )
    return sid


def _row_params(row_id: int, venue_id: str, row: dict, source: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "v": venue_id,
        "order_id": row.get("order_id"),
        "order_date": row.get("order_date"),
        "order_ts": row.get("order_ts"),
        "order_dow": row.get("order_dow"),
        "order_hour_int": row.get("order_hour_int"),
        "dish_type": row.get("dish_type"),
        "dish_name": row.get("dish_name"),
        "menu_price": _num_str(row.get("menu_price")),
        "discount": _num_str(row.get("discount")),
        "sale_price": _num_str(row.get("sale_price")),
        "qty": _num_str(row.get("qty")),
        "waiter_name": row.get("waiter_name"),
        "diners": row.get("diners"),
        "cost": _num_str(row.get("cost")),
        "cost_sum": _num_str(row.get("cost_sum")),
        "components": dump_json(row.get("components") or []),
        "source": source,
        "row_hash": row.get("row_hash"),
        "ts": now_iso(),
    }


async def write_ingestion(
    session: AsyncSession, *, venue_id: str, source: str, filename: str | None,
    rows_inserted: int, rows_skipped: int, date_min: str | None, date_max: str | None,
    status: str, error: str | None, file_path: str | None,
) -> int:
    iid = await _next_id(session, "analytics_ingestions")
    await session.execute(
        text(
            "INSERT INTO analytics_ingestions (id, venue_id, source, filename, "
            "rows_inserted, rows_skipped, date_min, date_max, status, error, "
            "file_path, created_at) VALUES (:id, :v, :source, :filename, :ri, :rs, "
            ":dmin, :dmax, :status, :error, :fp, :ts)"
        ),
        {"id": iid, "v": venue_id, "source": source, "filename": filename,
         "ri": rows_inserted, "rs": rows_skipped, "dmin": date_min, "dmax": date_max,
         "status": status, "error": error, "fp": file_path, "ts": now_iso()},
    )
    return iid


async def set_ingestion_file(session: AsyncSession, *, ing_id: int, file_path: str) -> None:
    await session.execute(
        text("UPDATE analytics_ingestions SET file_path = :fp WHERE id = :id"),
        {"fp": file_path, "id": ing_id},
    )


async def tag_raw_ingestion(session: AsyncSession, *, ids: list[int], ingestion_id: int) -> None:
    for rid in ids:
        await session.execute(
            text("UPDATE analytics_sales_raw SET ingestion_id = :iid WHERE id = :id"),
            {"iid": ingestion_id, "id": rid},
        )


# --------------------------------------------------------------------------- #
# Reprocess (rebuild processed sales from raw, in Python for portability)
# --------------------------------------------------------------------------- #


async def raw_rows_for_reprocess(
    session: AsyncSession, *, venue_id: str, ingestion_id: int | None,
    date_from: str | None, date_to: str | None,
) -> list[dict]:
    where = ["venue_id = :v", "order_id IS NOT NULL"]
    params: dict[str, Any] = {"v": venue_id}
    if ingestion_id is not None:
        where.append("ingestion_id = :iid")
        params["iid"] = ingestion_id
    else:
        where.append("order_date >= :df")
        where.append("order_date <= :dt")
        params["df"] = date_from
        params["dt"] = date_to
    sql = text(  # noqa: S608
        "SELECT id, order_id, order_date, order_ts, order_dow, order_hour_int, "
        "dish_type, dish_name, menu_price, discount, sale_price, qty, waiter_name, "
        "diners, cost, cost_sum, components, source, row_hash "
        f"FROM analytics_sales_raw WHERE {' AND '.join(where)}"
    )
    return [dict(r) for r in (await session.execute(sql, params)).mappings().all()]


async def delete_sales_for_reprocess(
    session: AsyncSession, *, venue_id: str, ingestion_id: int | None,
    date_from: str | None, date_to: str | None,
) -> int:
    if ingestion_id is not None:
        result = await session.execute(
            text(
                "DELETE FROM analytics_sales WHERE venue_id = :v AND raw_id IN "
                "(SELECT id FROM analytics_sales_raw WHERE ingestion_id = :iid)"
            ),
            {"v": venue_id, "iid": ingestion_id},
        )
    else:
        result = await session.execute(
            text(
                "DELETE FROM analytics_sales WHERE venue_id = :v AND raw_id IS NOT NULL "
                "AND order_date >= :df AND order_date <= :dt"
            ),
            {"v": venue_id, "df": date_from, "dt": date_to},
        )
    return int(getattr(result, "rowcount", 0) or 0)


# --------------------------------------------------------------------------- #
# Processing runs
# --------------------------------------------------------------------------- #


async def log_processing_run(
    session: AsyncSession, *, venue_id: str, kind: str, params: dict | None = None,
    sql_text: str | None = None, rows_affected: int | None = None,
    status: str = "success", error: str | None = None, actor: str | None = None,
) -> None:
    rid = await _next_id(session, "analytics_processing_runs")
    await session.execute(
        text(
            "INSERT INTO analytics_processing_runs (id, venue_id, kind, params, "
            "sql_text, rows_affected, status, error, actor, created_at) VALUES "
            "(:id, :v, :kind, :params, :sql, :rows, :status, :error, :actor, :ts)"
        ),
        {"id": rid, "v": venue_id, "kind": kind, "params": dump_json(params or {}),
         "sql": sql_text, "rows": rows_affected, "status": status, "error": error,
         "actor": actor, "ts": now_iso()},
    )


async def list_processing_runs(
    session: AsyncSession, *, venue_id: str, limit: int
) -> list[dict]:
    rows = (await session.execute(
        text(
            "SELECT id, kind, params, sql_text, rows_affected, status, error, actor, "
            "created_at FROM analytics_processing_runs WHERE venue_id = :v "
            "ORDER BY created_at DESC, id DESC LIMIT :lim"
        ),
        {"v": venue_id, "lim": limit},
    )).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = load_json(d["params"])
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Text-to-SQL chat execution (read-only; SQL already validated by the service)
# --------------------------------------------------------------------------- #


async def run_readonly_select(session: AsyncSession, *, sql: str, cap: int) -> list[dict]:
    """Execute a pre-validated read-only SELECT and return capped, JSON-safe rows."""
    result = (await session.execute(text(sql))).mappings().all()
    out: list[dict] = []
    for r in result[:cap]:
        clean: dict[str, Any] = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                clean[k] = v.isoformat()
            elif isinstance(v, (dict, list, str, int, float, bool)) or v is None:
                clean[k] = v
            else:
                clean[k] = str(v)
        out.append(clean)
    return out


# --------------------------------------------------------------------------- #
# Menu-items mirror
# --------------------------------------------------------------------------- #


async def upsert_menu_items(session: AsyncSession, *, venue_id: str, items: list[dict]) -> int:
    now = now_iso()
    n = 0
    for it in items:
        item_id = str(it.get("id") or "").strip()
        if not item_id:
            continue
        exists = (await session.execute(
            text("SELECT 1 FROM analytics_menu_items WHERE id = :id"), {"id": item_id},
        )).first()
        params = {
            "id": item_id, "v": venue_id, "name": it.get("name"),
            "category": it.get("category"), "price": _num_str(it.get("price")),
            "active": 1 if it.get("active", True) else 0,
            "payload": dump_json(it.get("payload") or {}), "ts": now,
        }
        if exists:
            await session.execute(
                text("UPDATE analytics_menu_items SET venue_id = :v, name = :name, "
                     "category = :category, price = CAST(:price AS NUMERIC), "
                     "active = :active, payload = :payload, synced_at = :ts WHERE id = :id"),
                params,
            )
        else:
            await session.execute(
                text("INSERT INTO analytics_menu_items (id, venue_id, name, category, "
                     "price, active, payload, synced_at) VALUES (:id, :v, :name, "
                     ":category, CAST(:price AS NUMERIC), :active, :payload, :ts)"),
                params,
            )
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Menu suggestions
# --------------------------------------------------------------------------- #


async def list_suggestions(
    session: AsyncSession, *, venue_id: str, status: str
) -> list[dict]:
    where = ["venue_id = :v"]
    params: dict[str, Any] = {"v": venue_id}
    if status and status != "all":
        where.append("status = :status")
        params["status"] = status
    sql = text(  # noqa: S608
        "SELECT id, dish_name, dish_type, price, sample_count, first_seen, last_seen, "
        "proposed_name, proposed_category, proposed_components, confidence, note, "
        "status, resolved_by, resolved_at, created_at FROM analytics_menu_suggestions "
        f"WHERE {' AND '.join(where)} ORDER BY sample_count DESC, id DESC"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["proposed_components"] = load_json(d["proposed_components"])
        out.append(d)
    return out


async def get_suggestion(session: AsyncSession, *, venue_id: str, sugg_id: int) -> dict | None:
    row = (await session.execute(
        text("SELECT * FROM analytics_menu_suggestions WHERE id = :id AND venue_id = :v"),
        {"id": sugg_id, "v": venue_id},
    )).mappings().first()
    if not row:
        return None
    d = dict(row)
    d["proposed_components"] = load_json(d["proposed_components"])
    return d


async def upsert_suggestion(
    session: AsyncSession, *, venue_id: str, dish_name: str, dish_type: str | None,
    price: float | None, sample_count: int, first_seen: str | None, last_seen: str | None,
    proposed_name: str | None, proposed_category: str | None,
    proposed_components: list[dict], confidence: str | None, note: str | None,
) -> None:
    now = now_iso()
    if price is not None:
        existing = (await session.execute(
            text("SELECT id FROM analytics_menu_suggestions WHERE venue_id = :v "
                 "AND dish_name = :dn AND price = CAST(:pr AS NUMERIC)"),
            {"v": venue_id, "dn": dish_name, "pr": _num_str(price)},
        )).mappings().first()
    else:
        existing = (await session.execute(
            text("SELECT id FROM analytics_menu_suggestions WHERE venue_id = :v "
                 "AND dish_name = :dn AND price IS NULL"),
            {"v": venue_id, "dn": dish_name},
        )).mappings().first()
    fields = {
        "dt": dish_type, "pr": _num_str(price), "sc": sample_count,
        "fs": first_seen, "ls": last_seen, "pn": proposed_name,
        "pc": proposed_category, "pcomp": dump_json(proposed_components),
        "conf": confidence, "note": note,
    }
    if existing:
        await session.execute(
            text("UPDATE analytics_menu_suggestions SET dish_type = :dt, "
                 "sample_count = :sc, first_seen = :fs, last_seen = :ls, "
                 "proposed_name = :pn, proposed_category = :pc, "
                 "proposed_components = :pcomp, confidence = :conf, note = :note "
                 "WHERE id = :id"),
            {**fields, "id": existing["id"]},
        )
    else:
        sid = await _next_id(session, "analytics_menu_suggestions")
        await session.execute(
            text("INSERT INTO analytics_menu_suggestions (id, venue_id, dish_name, "
                 "dish_type, price, sample_count, first_seen, last_seen, proposed_name, "
                 "proposed_category, proposed_components, confidence, note, status, "
                 "created_at) VALUES (:id, :v, :dn, :dt, CAST(:pr AS NUMERIC), :sc, :fs, "
                 ":ls, :pn, :pc, :pcomp, :conf, :note, 'open', :ts)"),
            {**fields, "id": sid, "v": venue_id, "dn": dish_name, "ts": now},
        )


async def set_suggestion_status(
    session: AsyncSession, *, sugg_id: int, status: str, actor: str | None
) -> None:
    await session.execute(
        text("UPDATE analytics_menu_suggestions SET status = :s, resolved_by = :actor, "
             "resolved_at = :ts WHERE id = :id"),
        {"s": status, "actor": actor, "ts": now_iso(), "id": sugg_id},
    )


async def unmatched_dishes(session: AsyncSession, *, venue_id: str) -> list[dict]:
    """Sales rows whose (dish_name, price) has no catalog entry and no components."""
    rows = (await session.execute(
        text(
            "SELECT dish_name, dish_type, menu_price AS price, COUNT(*) AS n, "
            "MIN(order_date) AS first_seen, MAX(order_date) AS last_seen "
            "FROM analytics_sales WHERE venue_id = :v AND dish_name IS NOT NULL "
            "AND menu_price IS NOT NULL AND (components = '[]' OR components IS NULL) "
            "GROUP BY dish_name, dish_type, menu_price ORDER BY n DESC"
        ),
        {"v": venue_id},
    )).mappings().all()
    out = []
    for r in rows:
        exists = (await session.execute(
            text("SELECT 1 FROM analytics_menu WHERE venue_id = :v AND pos_name = :p "
                 "AND price = CAST(:pr AS NUMERIC)"),
            {"v": venue_id, "p": r["dish_name"], "pr": _num_str(r["price"])},
        )).first()
        if not exists:
            out.append(dict(r))
    return out


__all__ = [
    "dump_json", "load_json", "get_categories", "get_summary", "get_timeseries",
    "get_category_mix", "get_top_dishes", "get_component_mix", "get_heatmap",
    "get_ingestions", "get_ingestion_file", "menu_lookup", "upsert_menu",
    "rematerialize_components", "row_hash_exists", "insert_raw_row", "insert_sales_row",
    "write_ingestion", "set_ingestion_file", "tag_raw_ingestion",
    "raw_rows_for_reprocess", "delete_sales_for_reprocess", "log_processing_run",
    "list_processing_runs", "run_readonly_select", "upsert_menu_items",
    "list_suggestions", "get_suggestion", "upsert_suggestion", "set_suggestion_status",
    "unmatched_dishes",
]
