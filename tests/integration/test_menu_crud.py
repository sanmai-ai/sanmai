"""Integration tests for menu CRUD against a real (sqlite) migrated database.

Migrations are applied through the real :mod:`be.migrate` runner (the same code path
prod uses), proving the 0002_menu.sql DDL is dialect-portable and that the version /
recipe logic holds end to end. Zero external credentials.
"""

from __future__ import annotations

from be.app.domains.menu import crud

VENUE = "default"


async def test_version_save_list_restore(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        v1 = await crud.save_version(
            session,
            item_id="it1",
            venue_id=VENUE,
            snapshot={
                "name": {"en": "First"},
                "active": True,  # availability -> stripped
                "options": [{"size": "6", "active": False}],
            },
            changed_by="admin-1",
        )
        assert v1["is_current"] is True
        assert "active" not in v1["snapshot"]
        assert v1["snapshot"]["options"][0] == {"size": "6"}
        assert v1["changed_by"] == "admin-1"

        v2 = await crud.save_version(
            session, item_id="it1", venue_id=VENUE, snapshot={"name": {"en": "Second"}}, changed_by="admin-1"
        )
        assert v2["id"] > v1["id"]

        versions = await crud.list_versions(session, item_id="it1", venue_id=VENUE)
        assert [v["id"] for v in versions] == [v2["id"], v1["id"]]  # newest first
        assert sum(1 for v in versions if v["is_current"]) == 1  # single-current invariant
        assert versions[0]["is_current"] and not versions[1]["is_current"]

        # restore v1 -> append a NEW current row copying v1's snapshot (append-only)
        restored = await crud.restore_version(
            session, item_id="it1", venue_id=VENUE, version_id=v1["id"], changed_by="admin-1"
        )
        assert restored is not None
        assert restored["source_version_id"] == v1["id"]
        assert restored["snapshot"]["name"]["en"] == "First"

        versions = await crud.list_versions(session, item_id="it1", venue_id=VENUE)
        assert len(versions) == 3
        assert sum(1 for v in versions if v["is_current"]) == 1
        assert versions[0]["id"] == restored["id"] and versions[0]["is_current"]


async def test_restore_missing_returns_none(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        out = await crud.restore_version(
            session, item_id="ghost", venue_id=VENUE, version_id=999, changed_by="admin-1"
        )
        assert out is None


async def test_first_edit_records_baseline(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await crud.save_version(
            session,
            item_id="it2",
            venue_id=VENUE,
            snapshot={"name": {"en": "Edited"}},
            changed_by="admin-1",
            baseline_snapshot={"name": {"en": "Original"}},  # published state before first edit
        )
        versions = await crud.list_versions(session, item_id="it2", venue_id=VENUE)
        assert len(versions) == 2  # baseline (non-current) + new current
        baseline = next(v for v in versions if not v["is_current"])
        assert baseline["changed_by"] == "(initial)"
        assert baseline["snapshot"]["name"]["en"] == "Original"

        # a second edit does NOT add another baseline
        await crud.save_version(
            session,
            item_id="it2",
            venue_id=VENUE,
            snapshot={"name": {"en": "Edited2"}},
            changed_by="admin-1",
            baseline_snapshot={"name": {"en": "ShouldBeIgnored"}},
        )
        versions = await crud.list_versions(session, item_id="it2", venue_id=VENUE)
        assert sum(1 for v in versions if v["changed_by"] == "(initial)") == 1


async def test_versions_are_venue_scoped(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        await crud.save_version(
            session, item_id="shared", venue_id="v-a", snapshot={"name": {"en": "A"}}, changed_by="admin-1"
        )
        await crud.save_version(
            session, item_id="shared", venue_id="v-b", snapshot={"name": {"en": "B"}}, changed_by="admin-1"
        )
        a = await crud.list_versions(session, item_id="shared", venue_id="v-a")
        b = await crud.list_versions(session, item_id="shared", venue_id="v-b")
        assert len(a) == 1 and a[0]["snapshot"]["name"]["en"] == "A"
        assert len(b) == 1 and b[0]["snapshot"]["name"]["en"] == "B"


async def test_recipe_put_get_replace_and_map(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    async with sessionmaker_for() as session:
        saved = await crud.replace_recipe_for_item(
            session,
            menu_item_id="dish1",
            venue_id=VENUE,
            updated_by="admin-1",
            lines=[
                {"inventory_sku": "SKU-A", "location": "kitchen", "quantity": 2.5, "option_index": -1},
                {"inventory_sku": "SKU-B", "location": "storage", "quantity": 1, "option_index": 0},
            ],
        )
        assert len(saved) == 2
        assert saved[0] == {
            "option_index": -1,
            "inventory_sku": "SKU-A",
            "location": "kitchen",
            "quantity": 2.5,
        }

        got = await crud.get_recipe_for_item(session, menu_item_id="dish1", venue_id=VENUE)
        assert got == saved

        # replace fully (delete-then-insert)
        saved2 = await crud.replace_recipe_for_item(
            session,
            menu_item_id="dish1",
            venue_id=VENUE,
            updated_by="admin-1",
            lines=[{"inventory_sku": "SKU-C", "location": "foh", "quantity": 3}],
        )
        assert len(saved2) == 1 and saved2[0]["inventory_sku"] == "SKU-C"

        # finalize bulk map
        m = await crud.get_recipe_map_for_finalize(
            session, item_ids=["dish1", "missing", ""], venue_id=VENUE
        )
        assert set(m.keys()) == {("dish1", -1)}
        assert m[("dish1", -1)][0]["inventory_sku"] == "SKU-C"

        # empty ids -> empty map
        assert await crud.get_recipe_map_for_finalize(session, item_ids=[], venue_id=VENUE) == {}


async def test_recipe_bad_line_raises_before_write(sessionmaker_for) -> None:  # type: ignore[no-untyped-def]
    import pytest

    async with sessionmaker_for() as session:
        await crud.replace_recipe_for_item(
            session,
            menu_item_id="dish2",
            venue_id=VENUE,
            updated_by="admin-1",
            lines=[{"inventory_sku": "KEEP", "location": "kitchen", "quantity": 1}],
        )
        with pytest.raises(ValueError):
            await crud.replace_recipe_for_item(
                session,
                menu_item_id="dish2",
                venue_id=VENUE,
                updated_by="admin-1",
                lines=[{"inventory_sku": "", "location": "kitchen", "quantity": 1}],
            )
        # validation happens before delete -> the prior recipe survives
        got = await crud.get_recipe_for_item(session, menu_item_id="dish2", venue_id=VENUE)
        assert len(got) == 1 and got[0]["inventory_sku"] == "KEEP"
