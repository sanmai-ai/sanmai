"""Analytics services routed through the LLM seam — all vendor-neutral.

Three LLM-backed flows, none of which ever lets model output touch the database
directly:

* **text-to-SQL chat** — the model proposes ONE read-only ``SELECT`` over the two
  allow-listed sales tables; :func:`validate_sql` re-validates it HARD (SELECT-only,
  single statement, no comments, no write/DDL keywords, only allow-listed tables,
  auto-``LIMIT``) BEFORE anything runs. A rejected query is logged and never executed,
  so a prompt-injected or hallucinated mutation cannot run. This is the core
  read-only/safe guarantee the tests assert.
* **menu-sync** — infer a generic ``components`` breakdown for un-enriched dishes, then
  upsert the catalog and re-materialize (write path is deterministic crud, not the LLM).
* **suggestions-refresh** — propose a clean name / category / components for unknown
  dishes for admin review.

Prompts are generic (no cuisine/brand/locale strings): ``components`` is any
``[{"name", "count"}]`` breakdown.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from be.adapters.errors import AdapterError
from be.adapters.llm.base import LLMProvider
from be.app.domains.analytics import crud

# Only these tables may appear in generated SQL.
ALLOWED_TABLES: tuple[str, ...] = ("analytics_sales", "analytics_menu")

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|TRUNCATE|COPY|"
    r"REPLACE|MERGE|CALL|EXECUTE|VACUUM|ANALYZE|ATTACH|PRAGMA|REINDEX)\b",
    re.IGNORECASE,
)
# Table name following FROM / JOIN (aliases and subqueries are covered because every
# real table reference is preceded by one of these keywords).
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", re.IGNORECASE)
_HAS_LIMIT = re.compile(r"\bLIMIT\b", re.IGNORECASE)

ROW_CAP = 200
ANSWER_SAMPLE = 50

_SQL_GEN_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "sql": {"type": "STRING"},
        "reason": {"type": "STRING"},
    },
}

_SCHEMA_DOC = (
    "You write ONE read-only SQL SELECT over a restaurant's sales data. You may "
    "reference ONLY these tables:\n\n"
    "analytics_sales — one row per sold line item: venue_id TEXT, order_id INTEGER, "
    "order_date TEXT ('YYYY-MM-DD'), order_dow INTEGER (0=Sun..6=Sat), "
    "order_hour_int INTEGER (0-23), dish_type TEXT (category), dish_name TEXT, "
    "menu_price NUMERIC (list unit price), discount NUMERIC, sale_price NUMERIC "
    "(amount charged for the line), qty NUMERIC, cost NUMERIC, cost_sum NUMERIC, "
    "components TEXT (JSON array of {\"name\",\"count\"}).\n"
    "analytics_menu — catalog lookup: venue_id TEXT, pos_name TEXT, price NUMERIC, "
    "clean_name TEXT, components TEXT. Join analytics_menu.pos_name = "
    "analytics_sales.dish_name AND analytics_menu.price = analytics_sales.menu_price "
    "for a clean dish name.\n\n"
    "Rules:\n"
    "- Output EXACTLY ONE SELECT statement. No other statements, no DDL/DML, no "
    "comments, no semicolons (one optional trailing ';').\n"
    "- Reference ONLY analytics_sales and/or analytics_menu.\n"
    "- Revenue = SUM(sale_price). Round money to 2 decimals.\n"
    "- Add a sensible ORDER BY and a LIMIT (<= 1000) for 'top'/'most' questions.\n"
    "- Return ONLY JSON {\"sql\": \"<the SELECT>\"}; if it can't be answered from "
    "these tables return {\"sql\": null, \"reason\": \"<short why>\"}."
)

_ANSWER_PROMPT = (
    "You are a concise restaurant data analyst. Given a question and the rows a SQL "
    "query returned, write a SHORT direct answer (1-3 sentences). Quote concrete "
    "numbers from the rows. If the rows are empty, say there's no matching data. Do "
    "not mention SQL or the database. Plain text only."
)

_COMPONENTS_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "clean_name": {"type": "STRING"},
                    "category": {"type": "STRING"},
                    "components": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {"type": "STRING"},
                                "count": {"type": "INTEGER"},
                            },
                        },
                    },
                    "confidence": {"type": "STRING"},
                    "note": {"type": "STRING"},
                },
                "required": ["id"],
            },
        }
    },
    "required": ["items"],
}

_MAP_SYSTEM_PROMPT = (
    "You map restaurant POS line items to a generic component breakdown. Each input "
    "item has an integer id, a POS dish_name (any language, possibly truncated), a "
    "category and a unit price. For each item return a clean display name, a category, "
    "and a components array of {\"name\", \"count\"} describing the sub-items that make "
    "up the dish (e.g. flavors/pieces) INFERRED ONLY from the name — never invent "
    "counts the name does not support; prefer confidence 'low' and a note when unsure. "
    "If an item is clearly not composed of sub-items, return an empty components array. "
    "Echo back each item's id exactly. Return only JSON."
)


def validate_sql(sql: str | None) -> str:
    """Return a safe single read-only SELECT or raise ``ValueError``.

    SELECT-only, at most one trailing ``;``, no comment markers, no write/DDL keywords,
    only the allow-listed tables; appends ``LIMIT 1000`` when no LIMIT is present.
    """
    if not sql or not isinstance(sql, str):
        raise ValueError("empty SQL")
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if ";" in s:
        raise ValueError("multiple statements are not allowed")
    if not re.match(r"^\s*SELECT\b", s, re.IGNORECASE):
        raise ValueError("only SELECT statements are allowed")
    if "--" in s or "/*" in s or "*/" in s:
        raise ValueError("comments are not allowed")
    if _FORBIDDEN.search(s):
        raise ValueError("write/DDL statements are not allowed")
    for tbl in _TABLE_REF.findall(s):
        if tbl.lower() not in ALLOWED_TABLES:
            raise ValueError(f"table not allowed: {tbl}")
    if not _HAS_LIMIT.search(s):
        s = f"{s}\nLIMIT 1000"
    return s


def _parse_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_history(history: list[dict] | None) -> str:
    if not history:
        return ""
    lines = []
    for turn in history[-6:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role") or "user"
        content = (turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return ("\nRecent conversation:\n" + "\n".join(lines) + "\n") if lines else ""


async def answer_question(
    llm: LLMProvider,
    session: AsyncSession,
    *,
    venue_id: str,
    question: str,
    history: list[dict] | None = None,
    actor: str | None = None,
) -> dict:
    """Answer a NL question via guarded text-to-SQL. Never executes unvalidated SQL."""
    question = (question or "").strip()
    if not question:
        return {"answer": "Please ask a question about the sales data.",
                "sql": None, "rows": [], "row_count": 0}

    # Step 1 — generate SQL through the LLM seam.
    gen_prompt = f"{_format_history(history)}\nQuestion: {question}\n"
    try:
        completion = await llm.complete(
            gen_prompt, system_prompt=_SCHEMA_DOC, schema=_SQL_GEN_SCHEMA, temperature=0
        )
        gen = _parse_json(completion.text)
    except AdapterError as exc:
        await crud.log_processing_run(
            session, venue_id=venue_id, kind="chat", sql_text="", rows_affected=0,
            status="error", error=f"generate: {exc}", actor=actor,
        )
        await session.commit()
        return {"answer": "Sorry, I couldn't work out how to answer that. Please "
                          "rephrase.", "sql": None, "rows": [], "row_count": 0}

    raw_sql = gen.get("sql")
    if not raw_sql:
        reason = gen.get("reason") or ""
        return {"answer": f"I can only answer questions about the sales data. {reason}",
                "sql": None, "rows": [], "row_count": 0}

    # Step 2 — validate HARD before anything touches the DB.
    try:
        safe_sql = validate_sql(raw_sql)
    except ValueError as exc:
        await crud.log_processing_run(
            session, venue_id=venue_id, kind="chat", sql_text=raw_sql, rows_affected=0,
            status="rejected", error=str(exc), actor=actor,
        )
        await session.commit()
        return {"answer": f"I generated a query I couldn't safely run ({exc}). Please "
                          "rephrase.", "sql": raw_sql, "rows": [], "row_count": 0}

    # Step 3 — execute the validated read-only SELECT.
    try:
        rows = await crud.run_readonly_select(session, sql=safe_sql, cap=ROW_CAP)
    except Exception as exc:  # noqa: BLE001 - report, never leak the traceback
        await session.rollback()
        await crud.log_processing_run(
            session, venue_id=venue_id, kind="chat", sql_text=safe_sql,
            rows_affected=0, status="error", error=f"execute: {exc}", actor=actor,
        )
        await session.commit()
        return {"answer": "The query failed to run. Please ask a different way.",
                "sql": safe_sql, "rows": [], "row_count": 0}

    await crud.log_processing_run(
        session, venue_id=venue_id, kind="chat", sql_text=safe_sql,
        rows_affected=len(rows), status="ok", actor=actor,
    )
    await session.commit()

    # Step 4 — phrase a natural-language answer (best-effort).
    answer = ""
    try:
        sample = rows[:ANSWER_SAMPLE]
        ans_prompt = (
            f"Question: {question}\nRows ({len(rows)} total, showing {len(sample)}):\n"
            + json.dumps(sample, ensure_ascii=False)
        )
        completion = await llm.complete(ans_prompt, system_prompt=_ANSWER_PROMPT)
        answer = completion.text.strip()
    except AdapterError:
        answer = f"Found {len(rows)} matching rows." if rows else "No matching data."

    return {
        "answer": answer or "No matching data.",
        "sql": safe_sql,
        "rows": rows[:ANSWER_SAMPLE],
        "row_count": len(rows),
    }


def _clean_components(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        try:
            count = int(str(item.get("count")))
        except (TypeError, ValueError):
            continue
        if name and count > 0:
            out.append({"name": str(name), "count": count})
    return out


async def _map_items(llm: LLMProvider, items: list[dict]) -> dict[int, dict]:
    """Ask the LLM to infer a component breakdown for each item; keyed by index."""
    slim = [
        {"id": i, "dish_name": it["dish_name"], "dish_type": it.get("dish_type"),
         "price": float(it["price"]) if it.get("price") is not None else None}
        for i, it in enumerate(items)
    ]
    prompt = "Items:\n" + json.dumps(slim, ensure_ascii=False)
    completion = await llm.complete(
        prompt, system_prompt=_MAP_SYSTEM_PROMPT, schema=_COMPONENTS_SCHEMA, temperature=0
    )
    parsed = _parse_json(completion.text)
    mapped: dict[int, dict] = {}
    for m in parsed.get("items", []) if isinstance(parsed, dict) else []:
        if isinstance(m, dict) and isinstance(m.get("id"), int):
            mapped[m["id"]] = m
    return mapped


async def sync_menu(
    llm: LLMProvider, session: AsyncSession, *, venue_id: str, dry_run: bool = True
) -> dict:
    """Reconcile the dish catalog against un-enriched sales via the LLM seam."""
    items = await crud.unmatched_dishes(session, venue_id=venue_id)
    if not items:
        return {"unmatched_count": 0, "proposed": [], "menu_rows_upserted": 0,
                "sales_rows_updated": 0, "dry_run": dry_run}

    mapped = await _map_items(llm, items)
    proposed = []
    for i, it in enumerate(items):
        m = mapped.get(i, {})
        proposed.append({
            "dish_name": it["dish_name"], "dish_type": it.get("dish_type"),
            "price": float(it["price"]) if it.get("price") is not None else None,
            "count": int(it["n"]), "first_seen": it.get("first_seen"),
            "last_seen": it.get("last_seen"),
            "clean_name": m.get("clean_name") or it["dish_name"],
            "components": _clean_components(m.get("components")),
            "confidence": m.get("confidence"), "note": m.get("note"),
        })

    if dry_run:
        return {"unmatched_count": len(items), "proposed": proposed,
                "menu_rows_upserted": 0, "sales_rows_updated": 0, "dry_run": True}

    upserted = 0
    for p in proposed:
        if not p["components"] or p["price"] is None:
            continue
        await crud.upsert_menu(
            session, venue_id=venue_id, pos_name=p["dish_name"], price=p["price"],
            clean_name=p["clean_name"], components=p["components"],
        )
        upserted += 1
    updated = await crud.rematerialize_components(session, venue_id=venue_id)
    await crud.log_processing_run(
        session, venue_id=venue_id, kind="menu_sync",
        params={"upserted": upserted, "updated": updated}, rows_affected=updated,
    )
    await session.commit()
    return {"unmatched_count": len(items), "proposed": proposed,
            "menu_rows_upserted": upserted, "sales_rows_updated": updated,
            "dry_run": False}


async def refresh_suggestions(
    llm: LLMProvider, session: AsyncSession, *, venue_id: str
) -> dict:
    """Detect unknown dishes and (re)generate AI proposals for admin review."""
    items = await crud.unmatched_dishes(session, venue_id=venue_id)
    if not items:
        return {"detected": 0, "proposed": 0}

    mapped = await _map_items(llm, items)
    proposed = 0
    for i, it in enumerate(items):
        m = mapped.get(i, {})
        price = float(it["price"]) if it.get("price") is not None else None
        await crud.upsert_suggestion(
            session, venue_id=venue_id, dish_name=it["dish_name"],
            dish_type=it.get("dish_type"), price=price, sample_count=int(it["n"]),
            first_seen=it.get("first_seen"), last_seen=it.get("last_seen"),
            proposed_name=m.get("clean_name") or it["dish_name"],
            proposed_category=m.get("category"),
            proposed_components=_clean_components(m.get("components")),
            confidence=m.get("confidence"), note=m.get("note"),
        )
        proposed += 1
    await session.commit()
    return {"detected": len(items), "proposed": proposed}


__all__ = [
    "ALLOWED_TABLES", "validate_sql", "answer_question", "sync_menu",
    "refresh_suggestions",
]
