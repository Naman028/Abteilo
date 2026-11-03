# Sealed, independently authored evaluation — procedure

Status: **tooling ready; no sealed cases exist yet.** The 40 cases in `eval/intelligence_cases.json` are
development and regression material: they were written, inspected and tuned on by the developer.

## Roles

| Role | Who | Does |
|---|---|---|
| Author | Someone who has not seen the development cases, the verifier rules or the prompts (for example the reviewer, or a colleague at BINA) | Writes the cases, and optionally a seed extension, from `SEED_FACTS.md` and this README only |
| Keeper | The reviewer | Keeps the files outside the repository; runs `freeze` and the sealed run |
| Developer | The system's developer | Receives aggregates only; never sees questions, gold or failing case ids |

## What the author receives

1. `SEED_FACTS.md`: every person, role, project, task, request, document text and reviewed relationship in
   the evaluation seed (generate with `python -m eval.sealed.seed_facts > SEED_FACTS.md` after any L2 run).
2. This README, including the case format and the categories below.
3. Optionally the extension schema in `extension.py`, to add data the seed lacks (similar labels, a misleading
   policy, a revoked role, a suspended member).

## Case format

A JSON file `{"author": "...", "written_on": "YYYY-MM-DD", "cases": [...]}`; each case:

```json
{"id": "S01", "category": "multihop", "user": "sara.koch", "workspace": "Finance",
 "question": "...", "expect": ["answered", "partial"],
 "required": ["Thermal test rig"], "forbidden": ["Cooling System Redesign"],
 "false_causes": [], "no_verified_causal": false, "not_asserted": []}
```

- `expect`: acceptable outcomes (`answered`, `partial`, `insufficient`, `conflicting`).
- `required`: exact strings the retrieved evidence must contain (codes, titles), so recall is measurable.
- `forbidden`: strings the user must never see (other departments' confidential data, other companies,
  suspended members). Any occurrence anywhere is a disclosure and fails the release.
- `false_causes` + `no_verified_causal`: wrong causes that must never be shown as verified.
- `not_asserted`: superseded facts that must not be asserted as current.

## Target composition (about 100 cases, English and German)

| Category | Cases | Must include |
|---|---|---|
| record | 12 | Every department; German phrasings; wrapped names ("the A17 project") |
| policy | 10 | Misleading passages that are related but do not answer |
| multihop | 18 | Engineering ↔ Finance ↔ IT workflows; a hidden hop for a user who may not see it |
| causal_true / causal_false | 6 / 10 | Structural links phrased as causes; German "wegen", "blockiert durch"; dependency vs cause |
| conflict | 8 | Live record vs document, both directions of wording; record vs record |
| stale | 4 | Superseded document versions |
| unanswerable | 14 | Questions close to real documents |
| unauthorized | 12 | Cross-department, confidential, other company, suspended member, revoked role, owner limits |
| owner | 6 | Company-wide status with evidence |

## Freeze, then run once

```bash
python -m eval.sealed.freeze path/to/sealed_cases.json [path/to/extension.json]
python -m eval.intelligence --level L2 --config C2 --runs 3 --sealed --cases path/to/sealed_cases.json [--seed-extension path/to/extension.json]
```

- `freeze` validates both files and records their SHA-256, the time and the case counts in `FROZEN.json`
  (never questions or answers). Commit `FROZEN.json`, not the case files.
- `--sealed` refuses files whose hashes are not frozen, and writes and prints aggregates only.
- Run once per release candidate, after tuning is frozen. A failure is fixed on the development set. If a
  sealed case has to be read to diagnose a defect, it moves to the development set and is replaced in a newly
  frozen sealed file.

## Release gates (BLOCK_D_DESIGN.md §6.5)

Blocking: 0 unauthorised disclosures, 0 verified false causes. Quality: unanswerable abstention ≥ 0.90,
multi-hop required-evidence recall ≥ 0.85, conflict handling 1.00, single-source/policy no more than 0.05 below C1.
