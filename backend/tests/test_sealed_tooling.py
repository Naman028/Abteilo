"""Sealed-evaluation tooling: a frozen case file cannot change unnoticed, and an author's seed extension is
applied through the application's own functions."""

import json
import sys

import pytest
from sqlalchemy import text

from eval.sealed import freeze
from eval.sealed.extension import Extension, apply
from tests.conftest import owner_conn, user_id

CASES = {"author": "Independent author", "written_on": "2026-10-24", "cases": [
    {"id": "S01", "category": "record", "user": "lena.weber", "workspace": "Engineering",
     "question": "Which tasks of the A18 project are blocked?", "expect": ["answered", "partial"],
     "required": ["A18"], "forbidden": []}]}


def test_a_frozen_case_file_cannot_change_unnoticed(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze, "FROZEN", tmp_path / "FROZEN.json")       # never the repository's record
    cases = tmp_path / "sealed.json"
    cases.write_text(json.dumps(CASES), encoding="utf-8")
    with pytest.raises(SystemExit):
        freeze.verify(cases, None)                                         # not frozen yet: refused
    monkeypatch.setattr(sys, "argv", ["freeze", str(cases)])
    freeze.main()
    record = freeze.verify(cases, None)
    assert record["cases"] == 1 and record["categories"] == {"record": 1} and "question" not in json.dumps(record)
    cases.write_text(json.dumps({**CASES, "written_on": "2026-10-25"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        freeze.verify(cases, None)                                         # edited after freezing: refused
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**CASES, "cases": [{**CASES["cases"][0], "category": "vibes"}]}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["freeze", str(bad)])
    with pytest.raises(Exception):
        freeze.main()                                                      # schema is validated before freezing


def test_an_authors_seed_extension_is_applied_through_the_application(demo):
    apply(demo, Extension.model_validate({
        "projects": [{"code": "A18", "name": "Cooling System Redesign Phase 2", "status": "blocked"}],
        "tasks": [{"project": "A18", "title": "Thermal validation retest 2", "status": "blocked",
                   "assignee": "jonas.becker", "due": "2026-12-01"}],
        "revoke_roles": [{"user": "tim.schulz", "role": "it_access_admin", "department": "it_security"}],
    }))
    with owner_conn() as conn:
        assert conn.execute(text("SELECT status FROM core.projects WHERE code = 'A18'")).scalar() == "blocked"
        assert conn.execute(text("SELECT count(*) FROM core.tasks WHERE title = 'Thermal validation retest 2'")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM core.role_assignments WHERE user_id = :u AND role_code ="
                                 " 'it_access_admin' AND revoked_at IS NULL"), {"u": user_id("tim.schulz@bina.example")}).scalar() == 0
    with pytest.raises(Exception):
        Extension.model_validate({"projects": [{"code": "a18", "name": "x"}]})   # invalid code is refused
