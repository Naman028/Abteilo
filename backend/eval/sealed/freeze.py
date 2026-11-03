"""Freeze a sealed case set before its first run.

    python -m eval.sealed.freeze <cases.json> [<extension.json>]

Validates the files, prints their SHA-256 and appends a record to FROZEN.json (hashes, time, case counts per
category; never questions or answers). `python -m eval.intelligence --sealed` refuses to run a case file (and
extension) whose hashes are not recorded here, so a sealed set cannot be edited after it was frozen without that
being visible. The files themselves stay outside the repository.
"""

import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FROZEN = Path(__file__).with_name("FROZEN.json")
Category = Literal["record", "policy", "multihop", "causal_true", "causal_false", "conflict", "stale", "unanswerable",
                   "unauthorized", "suspended", "owner"]
Outcome = Literal["answered", "partial", "insufficient", "conflicting"]


class Case(BaseModel):
    """The case format eval.intelligence judges (see intelligence_cases.json for the development examples)."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Z0-9-]{1,12}$")
    category: Category
    user: str
    workspace: str | None
    question: str = Field(min_length=3, max_length=500)
    expect: list[Outcome] = Field(min_length=1)
    required: list[str] = []          # strings the evidence must contain (by stable keys: codes, titles)
    forbidden: list[str] = []         # strings that must appear nowhere the user can see
    false_causes: list[str] = []
    no_verified_causal: bool = False
    not_asserted: list[str] = []      # stale facts that must not be asserted


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author: str = Field(min_length=2)          # who wrote it (must not be the system's developer)
    written_on: str
    cases: list[Case] = Field(min_length=1)


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def records() -> list[dict]:
    return json.loads(FROZEN.read_text(encoding="utf-8")) if FROZEN.exists() else []


def verify(cases: str | Path, extension: str | Path | None) -> dict:
    """The frozen record for exactly these files, or SystemExit."""
    want = (sha256(cases), sha256(extension) if extension else None)
    for record in records():
        if (record["cases_sha256"], record["extension_sha256"]) == want:
            return record
    raise SystemExit("These sealed files are not frozen (or were changed after freezing): run eval.sealed.freeze first.")


def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    cases_path, ext_path = sys.argv[1], (sys.argv[2] if len(sys.argv) == 3 else None)
    case_file = CaseFile.model_validate(json.loads(Path(cases_path).read_text(encoding="utf-8")))
    if len({c.id for c in case_file.cases}) != len(case_file.cases):
        raise SystemExit("Case ids must be unique.")
    if ext_path:
        from eval.sealed.extension import load
        load(ext_path)                       # validates the extension schema
    record = {"cases_sha256": sha256(cases_path), "extension_sha256": sha256(ext_path) if ext_path else None,
              "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"), "author": case_file.author,
              "written_on": case_file.written_on, "cases": len(case_file.cases),
              "categories": dict(Counter(c.category for c in case_file.cases))}
    if any(r["cases_sha256"] == record["cases_sha256"] for r in records()):
        raise SystemExit("Already frozen.")
    FROZEN.write_text(json.dumps([*records(), record], indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
