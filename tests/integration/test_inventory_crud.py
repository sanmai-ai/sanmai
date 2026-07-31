"""Integration tests for inventory CRUD against a real (sqlite) migrated database.

Migrations are applied through the real :mod:`be.migrate` runner (the same code path
prod uses), proving 0003_inventory.sql is dialect-portable and the item / stock /
supplier / purchasing / alias logic holds end to end. Zero external credentials.
"""

from __future__ import annotations

import pytest

from be.app.domains.inventory import crud

VENUE = "default"


async def _make_item(session, sku="water", name="Water", unit="bottle") -> dict:
    return await crud.create_item(
        session,
        venue_id=VENUE,
        sku=sku,
        name=name,
        category="drinks",
        unit=unit,
        allowed_locations=["storage", "kitchen"],
        reorder_point=5,
        max_threshold=100,
        comment=None,
    )


async def test_item_create_get_update(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        item = await _make_item(session)
        assert item["sku"] == "water"
        assert item["allowed_locations"] == ["storage", "kitchen"]
        assert item["reorder_point"] == 5.0
        assert item["is_active"] is True

        # duplicate sku -> ValueError
        with pytest.raises(ValueError, match="already exists"):
            await _make_item(session)

        updated = await crud.update_item(
            session, sku="water", fields={"name": "Spring Water", "reorder_point": 8}
        )
        assert updated is not None
        assert updated["name"] == "Spring Water"
        assert updated["reorder_point"] == 8.0
        # untouched field preserved
        assert updated["unit"] == "bottle"

        assert await crud.update_item(session, sku="ghost", fields={"name": "x"}) is None


async def test_item_sku_slug_from_name(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        item = await crud.create_item(
            session,
            venue_id=VENUE,
            sku=None,
            name="Olive Oil 5L",
            category="oils",
            unit="l",
            allowed_locations=["storage"],
            reorder_point=None,
            max_threshold=None,
            comment=None,
        )
        assert item["sku"] == "olive_oil_5l"


async def test_item_create_rejects_bad_location(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        with pytest.raises(ValueError, match="invalid locations"):
            await crud.create_item(
                session,
                venue_id=VENUE,
                sku="x",
                name="X",
                category="c",
                unit="u",
                allowed_locations=["freezer"],
                reorder_point=None,
                max_threshold=None,
                comment=None,
            )
        with pytest.raises(ValueError, match="allowed location"):
            await crud.create_item(
                session,
                venue_id=VENUE,
                sku="y",
                name="Y",
                category="c",
                unit="u",
                allowed_locations=[],
                reorder_point=None,
                max_threshold=None,
                comment=None,
            )


async def test_stock_set_and_move(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session)
        await crud.set_stock(session, sku="water", location="storage", venue_id=VENUE, quantity=10)
        await crud.set_stock(session, sku="water", location="kitchen", venue_id=VENUE, quantity=2)

        page = await crud.get_page_data(session, venue_id=VENUE)
        row = next(r for r in page if r["sku"] == "water")
        assert row["stock"]["storage"] == 10.0
        assert row["stock"]["kitchen"] == 2.0
        assert row["stock"]["total"] == 12.0

        moved = await crud.move_stock(
            session, sku="water", from_location="storage", to_location="kitchen",
            venue_id=VENUE, quantity=4,
        )
        assert moved["quantity"] == 4
        page = await crud.get_page_data(session, venue_id=VENUE)
        row = next(r for r in page if r["sku"] == "water")
        assert row["stock"]["storage"] == 6.0
        assert row["stock"]["kitchen"] == 6.0
        assert row["stock"]["total"] == 12.0  # conserved


async def test_stock_move_insufficient_and_unknown(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session)
        await crud.set_stock(session, sku="water", location="storage", venue_id=VENUE, quantity=1)
        with pytest.raises(ValueError, match="insufficient"):
            await crud.move_stock(
                session, sku="water", from_location="storage", to_location="kitchen",
                venue_id=VENUE, quantity=5,
            )
        with pytest.raises(ValueError, match="sku not found"):
            await crud.set_stock(
                session, sku="ghost", location="storage", venue_id=VENUE, quantity=1
            )


async def test_stock_is_venue_scoped(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session)
        await crud.set_stock(session, sku="water", location="storage", venue_id="v-a", quantity=7)
        await crud.set_stock(session, sku="water", location="storage", venue_id="v-b", quantity=3)
        a = await crud.get_page_data(session, venue_id="v-a")
        b = await crud.get_page_data(session, venue_id="v-b")
        assert next(r for r in a if r["sku"] == "water")["stock"]["storage"] == 7.0
        assert next(r for r in b if r["sku"] == "water")["stock"]["storage"] == 3.0


async def test_supplier_upsert_roundtrip(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        s = await crud.upsert_supplier(
            session,
            venue_id=VENUE,
            supplier_id="acme",
            name="Acme",
            contact_person="Pat",
            payment_type="monthly",
            site=None,
            chat_link=None,
            comment=None,
            categories=["drinks", "snacks"],
            approx_delivery_time_info=None,
            is_active=True,
            min_order_amount=100.0,
            payment_day=15,
            order_cutoff_time="13:00",
            delivery_lead_days=1,
            delivery_working_days_only=True,
        )
        assert s["categories"] == ["drinks", "snacks"]
        assert s["order_cutoff_time"] == "13:00"
        assert s["min_order_amount"] == 100.0
        created_at = s["created_at"]

        # update preserves created_at
        s2 = await crud.upsert_supplier(
            session,
            venue_id=VENUE,
            supplier_id="acme",
            name="Acme Ltd",
            contact_person=None,
            payment_type=None,
            site=None,
            chat_link=None,
            comment=None,
            categories=None,
            approx_delivery_time_info=None,
            is_active=False,
            min_order_amount=None,
            payment_day=None,
            order_cutoff_time=None,
            delivery_lead_days=None,
            delivery_working_days_only=False,
        )
        assert s2["name"] == "Acme Ltd"
        assert s2["is_active"] is False
        assert s2["created_at"] == created_at


async def test_supplier_item_create_update_and_dup(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session, sku="water", name="Water")
        await _make_item(session, sku="soda", name="Soda")
        await crud.upsert_supplier(
            session, venue_id=VENUE, supplier_id="acme", name="Acme",
            contact_person=None, payment_type=None, site=None, chat_link=None, comment=None,
            categories=None, approx_delivery_time_info=None, is_active=True,
            min_order_amount=None, payment_day=None, order_cutoff_time=None,
            delivery_lead_days=None, delivery_working_days_only=True,
        )
        si = await crud.create_supplier_item(
            session, venue_id=VENUE, inventory_sku="water", supplier_id="acme",
            supplier_sku="W1", pack_name="case", pack_size=24, price_per_pack=30,
            tax_inclusive=True, is_primary_supplier=True, is_on_stop=False, link=None, comment=None,
        )
        assert si["supplier_sku"] == "W1"
        assert si["pack_size"] == 24.0
        assert si["tax_inclusive"] is True

        # same supplier_sku on a different item -> conflict
        with pytest.raises(ValueError, match="already used"):
            await crud.create_supplier_item(
                session, venue_id=VENUE, inventory_sku="soda", supplier_id="acme",
                supplier_sku="W1", pack_name=None, pack_size=6, price_per_pack=10,
                tax_inclusive=False, is_primary_supplier=False, is_on_stop=False, link=None, comment=None,
            )
        # duplicate (same item+supplier) -> already exists
        with pytest.raises(ValueError, match="already exists"):
            await crud.create_supplier_item(
                session, venue_id=VENUE, inventory_sku="water", supplier_id="acme",
                supplier_sku="W2", pack_name=None, pack_size=1, price_per_pack=1,
                tax_inclusive=False, is_primary_supplier=False, is_on_stop=False, link=None, comment=None,
            )

        updated = await crud.update_supplier_item(
            session, inventory_sku="water", supplier_id="acme",
            fields={"price_per_pack": 33, "is_on_stop": True},
        )
        assert updated is not None
        assert updated["price_per_pack"] == 33.0
        assert updated["is_on_stop"] is True
        assert updated["pack_size"] == 24.0  # preserved

        assert await crud.update_supplier_item(
            session, inventory_sku="ghost", supplier_id="acme", fields={"link": "x"}
        ) is None


async def test_purchase_order_place_and_receive(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session, sku="water", name="Water")
        await crud.upsert_purchase_order(
            session,
            venue_id=VENUE,
            order_id="po1",
            supplier_id="acme",
            supplier_name="Acme",
            status="placed",
            expected_delivery_date="2026-08-05",
            expected_total_amount=120.0,
            invoice_id=None,
            notes="first",
            items=[{"inventory_sku": "water", "packs": 3, "pack_size": 24, "pack_price": 30}],
        )
        page = await crud.get_page_data(session, venue_id=VENUE)
        row = next(r for r in page if r["sku"] == "water")
        # expected_quantity = packs*pack_size = 72
        assert row["expected"]["quantity"] == 72.0
        assert row["expected"]["date"] == "2026-08-05"

        # re-place replaces items (delete-then-insert)
        await crud.upsert_purchase_order(
            session, venue_id=VENUE, order_id="po1", supplier_id="acme", supplier_name="Acme",
            status="placed", expected_delivery_date="2026-08-05", expected_total_amount=40.0,
            invoice_id=None, notes=None,
            items=[{"inventory_sku": "water", "packs": 1, "pack_size": 24, "pack_price": 30}],
        )
        page = await crud.get_page_data(session, venue_id=VENUE)
        row = next(r for r in page if r["sku"] == "water")
        assert row["expected"]["quantity"] == 24.0

        await crud.receive_purchase_order(
            session, venue_id=VENUE, order_id="po1",
            received_items=[{"inventory_sku": "water", "quantity": 24}],
        )
        page = await crud.get_page_data(session, venue_id=VENUE)
        row = next(r for r in page if r["sku"] == "water")
        assert row["stock"]["storage"] == 24.0
        # received orders drop out of "expected"
        assert row["expected"]["quantity"] == 0.0


async def test_alias_roundtrip_and_unknown_sku_skipped(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session, sku="water", name="Water")
        n = await crud.upsert_order_import_aliases(
            session,
            venue_id=VENUE,
            supplier_id="acme",
            mappings=[
                {"raw_name": "Ferrarelle 500", "raw_name_norm": "ferrarelle 500", "inventory_sku": "water"},
                {"raw_name": "Junk", "raw_name_norm": "junk", "inventory_sku": "nonexistent"},
            ],
        )
        assert n == 1  # unknown sku skipped
        aliases = await crud.get_order_import_aliases(session, supplier_id="acme")
        assert aliases == {"ferrarelle 500": "water"}

        # update existing alias
        await crud.upsert_order_import_aliases(
            session, venue_id=VENUE, supplier_id="acme",
            mappings=[{"raw_name": "Ferrarelle 500", "raw_name_norm": "ferrarelle 500", "inventory_sku": "water"}],
        )
        assert (await crud.get_order_import_aliases(session, supplier_id="acme")) == {
            "ferrarelle 500": "water"
        }


async def test_match_catalog_supplier_first(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await _make_item(session, sku="water", name="Water")
        await _make_item(session, sku="soda", name="Soda")
        await crud.upsert_supplier(
            session, venue_id=VENUE, supplier_id="acme", name="Acme",
            contact_person=None, payment_type=None, site=None, chat_link=None, comment=None,
            categories=None, approx_delivery_time_info=None, is_active=True,
            min_order_amount=None, payment_day=None, order_cutoff_time=None,
            delivery_lead_days=None, delivery_working_days_only=True,
        )
        await crud.create_supplier_item(
            session, venue_id=VENUE, inventory_sku="water", supplier_id="acme",
            supplier_sku="W1", pack_name="case", pack_size=24, price_per_pack=30,
            tax_inclusive=False, is_primary_supplier=True, is_on_stop=False, link=None, comment=None,
        )
        catalog = await crud.get_order_match_catalog(session, supplier_id="acme")
        by_sku = {c["inventory_sku"]: c for c in catalog}
        assert by_sku["water"]["from_supplier"] is True
        assert by_sku["water"]["pack_size"] == 24.0
        assert by_sku["soda"]["from_supplier"] is False
        assert by_sku["soda"]["supplier_sku"] is None


async def test_supplier_monthly_payment_upsert(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        p = await crud.upsert_supplier_monthly_payment(
            session, venue_id=VENUE, supplier_id="acme", period_year=2026, period_month=8,
            status="pending", paid_amount=None, paid_method=None, proof_url=None, notes=None,
        )
        assert p["status"] == "pending"
        assert p["paid_at"] is None
        pid = p["id"]

        p2 = await crud.upsert_supplier_monthly_payment(
            session, venue_id=VENUE, supplier_id="acme", period_year=2026, period_month=8,
            status="paid", paid_amount=250.0, paid_method="transfer", proof_url=None, notes="done",
        )
        assert p2["id"] == pid  # same period updated, not a new row
        assert p2["status"] == "paid"
        assert p2["paid_amount"] == 250.0
        assert p2["paid_at"] is not None

        listing = await crud.list_supplier_monthly_payments(
            session, venue_id=VENUE, supplier_id="acme"
        )
        assert len(listing) == 1

        with pytest.raises(ValueError, match="period_month"):
            await crud.upsert_supplier_monthly_payment(
                session, venue_id=VENUE, supplier_id="acme", period_year=2026, period_month=13,
                status="pending", paid_amount=None, paid_method=None, proof_url=None, notes=None,
            )
