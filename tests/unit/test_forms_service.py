"""Unit tests for the forms domain service helpers (pure logic, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from be.app.domains.forms import service

# --- enum validators --------------------------------------------------------


def test_validate_binding_language() -> None:
    service.validate_binding_language("he")
    service.validate_binding_language(None)
    with pytest.raises(ValueError):
        service.validate_binding_language("fr")


def test_validate_course_item_type() -> None:
    service.validate_course_item_type("quiz")
    with pytest.raises(ValueError):
        service.validate_course_item_type("audio")


def test_validate_flow_ordering_and_item_type() -> None:
    service.validate_flow_ordering("parallel")
    service.validate_flow_item_type("course")
    with pytest.raises(ValueError):
        service.validate_flow_ordering("random")
    with pytest.raises(ValueError):
        service.validate_flow_item_type("video")


def test_normalize_assignment_status_and_sort() -> None:
    assert service.normalize_assignment_status("overdue") == "overdue"
    assert service.normalize_assignment_status("bogus") is None
    assert service.normalize_assignment_status(None) is None
    assert service.normalize_assignment_sort("employee") == "employee"
    assert service.normalize_assignment_sort("bogus") == "due_at_asc"


# --- ids / time -------------------------------------------------------------


def test_new_id_unique() -> None:
    assert service.new_id() != service.new_id()


def test_compute_due_at_override_beats_default() -> None:
    base = "2026-01-01T00:00:00+00:00"
    assert service.compute_due_at(base, 10, 3) == datetime(
        2026, 1, 4, tzinfo=UTC
    ).isoformat()
    assert service.compute_due_at(base, 10, None) == datetime(
        2026, 1, 11, tzinfo=UTC
    ).isoformat()
    assert service.compute_due_at(base, None, None) is None


# --- course completion set math ---------------------------------------------


def test_merge_completed_item_dedupes_and_preserves_order() -> None:
    assert service.merge_completed_item('["a", "b"]', "b") == ["a", "b"]
    assert service.merge_completed_item('["a"]', "b") == ["a", "b"]
    assert service.merge_completed_item(None, "x") == ["x"]
    assert service.merge_completed_item(["a", "a", "b"], "c") == ["a", "b", "c"]


def test_course_complete() -> None:
    assert service.course_complete(["a", "b"], ["a", "b", "c"]) is True
    assert service.course_complete(["a", "b"], ["a"]) is False
    assert service.course_complete([], []) is False  # empty course never auto-completes


# --- flow materialisation ---------------------------------------------------


def test_initial_progress_status() -> None:
    assert service.initial_progress_status("parallel", 0) == "available"
    assert service.initial_progress_status("parallel", 3) == "available"
    assert service.initial_progress_status("sequential", 0) == "available"
    assert service.initial_progress_status("sequential", 1) == "locked"


# --- rollup / filter / sort -------------------------------------------------


def test_rollup_progress_buckets() -> None:
    assert service.rollup_progress([])["status"] == "not_started"
    locked = [{"status": "locked"}, {"status": "available"}]
    assert service.rollup_progress(locked)["status"] == "not_started"
    mixed = [{"status": "completed"}, {"status": "available"}]
    r = service.rollup_progress(mixed)
    assert r["status"] == "in_progress"
    assert r["completed_items"] == 1 and r["total_items"] == 2
    done = [{"status": "completed"}, {"status": "completed"}]
    assert service.rollup_progress(done)["status"] == "completed"


def test_rollup_progress_overdue_overlay() -> None:
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    rows = [{"status": "available", "due_at": past}, {"status": "locked", "due_at": future}]
    r = service.rollup_progress(rows)
    assert r["is_overdue"] is True
    # next_due_at is the soonest incomplete due (the past one)
    assert r["next_due_at"] == past
    # a completed row past its due does not count as overdue
    ok = service.rollup_progress([{"status": "completed", "due_at": past}])
    assert ok["is_overdue"] is False


def test_matches_status_filter() -> None:
    r = service.rollup_progress([{"status": "completed"}, {"status": "available"}])
    assert service.matches_status_filter(r, None) is True
    assert service.matches_status_filter(r, "in_progress") is True
    assert service.matches_status_filter(r, "completed") is False
    overdue = service.rollup_progress(
        [{"status": "available", "due_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}]
    )
    assert service.matches_status_filter(overdue, "overdue") is True


def test_sort_assignments_due_asc_nulls_last() -> None:
    soon = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    later = (datetime.now(UTC) + timedelta(days=9)).isoformat()
    rows = [
        {"id": "none", "next_due_at": None},
        {"id": "later", "next_due_at": later},
        {"id": "soon", "next_due_at": soon},
    ]
    ordered = [r["id"] for r in service.sort_assignments(rows, "due_at_asc")]
    assert ordered == ["soon", "later", "none"]


def test_template_has_signature_field() -> None:
    assert service.template_has_signature_field([{"type": "text"}, {"type": "Signature"}]) is True
    assert service.template_has_signature_field('[{"type": "signature"}]') is True
    assert service.template_has_signature_field([{"type": "text"}]) is False
    assert service.template_has_signature_field(None) is False


def test_json_load_tolerates_parsed_and_bad() -> None:
    assert service.json_load('{"a": 1}', {}) == {"a": 1}
    assert service.json_load({"a": 1}, {}) == {"a": 1}
    assert service.json_load("not-json", ["default"]) == ["default"]
    assert service.json_load(None, []) == []
