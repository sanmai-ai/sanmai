"""Unit tests for the analytics domain — the read-only/safe text-to-SQL guard and the
generic importer, with zero DB and zero credentials.

The core guarantee under test: :func:`service.validate_sql` accepts exactly one
read-only SELECT over the allow-listed sales tables and rejects every write/DDL,
multi-statement, commented, or off-catalog query — so LLM output can never mutate the
database. The importer test covers the generic CSV/xlsx parse + canonicalisation
(including the portable day-of-week / hour heatmap keys) and the row-hash idempotency key.
"""

from __future__ import annotations

import io
import json

import pytest

from be.app.domains.analytics import ingest, service

# --- text-to-SQL safety -----------------------------------------------------


def test_validate_sql_accepts_select_and_appends_limit() -> None:
    out = service.validate_sql("SELECT dish_name FROM analytics_sales")
    assert out.startswith("SELECT")
    assert "LIMIT 1000" in out


def test_validate_sql_keeps_existing_limit() -> None:
    out = service.validate_sql("SELECT * FROM analytics_sales LIMIT 5")
    assert out.count("LIMIT") == 1


def test_validate_sql_allows_join_of_allowlisted_tables() -> None:
    sql = (
        "SELECT s.dish_name FROM analytics_sales s "
        "JOIN analytics_menu m ON m.pos_name = s.dish_name"
    )
    assert "analytics_menu" in service.validate_sql(sql)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "DROP TABLE analytics_sales",
        "DELETE FROM analytics_sales",
        "UPDATE analytics_sales SET qty = 0",
        "INSERT INTO analytics_sales (id) VALUES (1)",
        "SELECT 1; DELETE FROM analytics_sales",  # multi-statement
        "SELECT * FROM analytics_sales -- comment",  # comment marker
        "SELECT * FROM admin_users",  # off-catalog table
        "SELECT * FROM analytics_sales UNION SELECT * FROM admin_users",
        "WITH x AS (SELECT 1) SELECT * FROM x",  # not a leading SELECT? actually starts WITH
    ],
)
def test_validate_sql_rejects_unsafe(bad: str) -> None:
    with pytest.raises(ValueError):
        service.validate_sql(bad)


def test_clean_components_drops_junk() -> None:
    out = service._clean_components(
        [{"name": "a", "count": 3}, {"name": "b", "count": 0}, {"count": 2}, "x"]
    )
    assert out == [{"name": "a", "count": 3}]


# --- generic importer -------------------------------------------------------


def test_parse_and_canonicalize_csv() -> None:
    csv = (
        b"order_id,order_date,order_ts,dish_type,dish_name,menu_price,sale_price,qty\n"
        b"7,2026-03-02,2026-03-02T13:15:00,Mains,Bowl,40,38,2\n"
    )
    rows = ingest.parse_bytes(csv, filename="x.csv")
    row = ingest._canonicalize(rows[0])
    assert row["order_id"] == 7
    assert row["order_date"] == "2026-03-02"
    assert row["order_hour_int"] == 13
    # 2026-03-02 is a Monday -> 0=Sun convention => 1
    assert row["order_dow"] == 1
    assert row["sale_price"] == 38.0
    assert row["qty"] == 2.0


def test_components_cell_parses_list_and_wrapped_forms() -> None:
    assert ingest._parse_components('[{"name": "soy", "count": 1}]') == [
        {"name": "soy", "count": 1}
    ]
    assert ingest._parse_components('{"components": [{"name": "x", "count": 2}]}') == [
        {"name": "x", "count": 2}
    ]
    assert ingest._parse_components("not json") == []
    assert ingest._parse_components("") == []


def test_row_hash_is_stable_and_distinguishes_lines() -> None:
    a = ingest._canonicalize(
        {"order_id": "1", "order_ts": "2026-01-01T10:00:00", "dish_name": "A",
         "menu_price": "5", "sale_price": "5", "discount": "0", "qty": "1"}
    )
    b = dict(a)
    b["dish_name"] = "B"
    assert ingest._row_hash(a) == ingest._row_hash(a)
    assert ingest._row_hash(a) != ingest._row_hash(b)


def test_parse_xlsx_roundtrip() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["order_id", "order_date", "dish_name", "sale_price"])
    ws.append([9, "2026-04-01", "Tea", 12])
    buf = io.BytesIO()
    wb.save(buf)
    rows = ingest.parse_bytes(buf.getvalue(), filename="x.xlsx")
    assert rows[0]["order_id"] == 9
    canonical = ingest._canonicalize(rows[0])
    assert canonical["dish_name"] == "Tea"
    assert canonical["sale_price"] == 12.0


def test_sql_gen_schema_shape() -> None:
    # The generated JSON the router feeds validate_sql must be parseable.
    payload = json.dumps({"sql": "SELECT 1 AS n FROM analytics_sales"})
    assert service._parse_json(payload)["sql"].startswith("SELECT")
