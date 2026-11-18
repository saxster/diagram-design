#!/usr/bin/env python3
"""Adversarial and contract tests for repair_rounds.py (feedback-efficiency & cross-replay harness)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "skills/diagram-design/scripts/repair_rounds.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("repair_rounds", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not HARNESS.exists():
        print(f"FAIL: {HARNESS} does not exist")
        return 1

    module = load_harness()
    failures: list[str] = []

    # 1. Suite integrity check: baseline fixture passes clean, every defect class triggers its exact code.
    integrity = module.check_suite()
    if not integrity.get("ok") or integrity.get("caseCount") != 11:
        failures.append(f"suite integrity failed: {integrity}")
    else:
        print(f"OK: suite integrity verified ({integrity['caseCount']} cases)")

    # 2. Rules-only mechanical repairer converges in 1 round across all 11 cases using only --json receipt fields.
    rules_report = module.run_suite(repair_mode="rules")
    if (
        not rules_report.get("ok")
        or rules_report.get("firstRoundDisclosureRate") != 1.0
        or rules_report.get("lateDiscoveryRate") != 0.0
        or rules_report.get("unactionableCount") != 0
        or rules_report.get("maxRoundsToPass") != 1
        or rules_report.get("totalObservableTokens", 0) <= 0
    ):
        failures.append(f"rules-mode benchmark failed contract: {rules_report}")
    else:
        print("OK: rules-only repairer converges in 1 round across all 11 cases with 0 unactionable receipts")

    # 3. Oracle repairer also converges in 1 round.
    oracle_report = module.run_suite(repair_mode="oracle")
    if not oracle_report.get("ok") or oracle_report.get("maxRoundsToPass") != 1:
        failures.append(f"oracle-mode benchmark failed: {oracle_report}")
    else:
        print("OK: oracle repairer converges in 1 round")

    # 4. Unactionable diagnostic detection: a stripped diagnostic with no evidence/fixes must be counted honestly.
    unact = module.apply_rules_repair(
        "<!DOCTYPE html><html><body></body></html>",
        [{"code": "geometry/connector-micro-jog", "line": 1, "evidence": {}, "supportedFixes": []}],
    )
    if unact.get("repaired") or unact.get("unactionable") != 1:
        failures.append(f"stripped diagnostic should report unactionable=1, got {unact}")
    else:
        print("OK: stripped diagnostic without evidence/supportedFixes is recorded as unactionable")

    # 5. Cross-replay verification between base and candidate HTML files.
    with tempfile.TemporaryDirectory() as tmp:
        base_file = Path(tmp) / "base.html"
        cand_file = Path(tmp) / "candidate.html"
        base_file.write_text(module.inject_defect(module.BASE_DOCUMENT, "connector-micro-jog"), encoding="utf-8")
        cand_file.write_text(module.BASE_DOCUMENT, encoding="utf-8")
        replay = module.cross_replay(base_file, cand_file)
        if replay.get("baseFindingCount") != 1 or replay.get("candidateFindingCount") != 0 or not replay.get("improved"):
            failures.append(f"cross-replay failed to attribute improvement: {replay}")
        else:
            print("OK: cross-replay attributes improvement between frozen base and candidate HTML")

    # 6. CLI subprocess invocation (`run --json`).
    proc = subprocess.run(
        [sys.executable, str(HARNESS), "run", "--repair", "rules", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        failures.append(f"CLI run --json exited {proc.returncode}: {proc.stderr or proc.stdout}")
    else:
        payload = json.loads(proc.stdout)
        if payload.get("unactionableCount") != 0 or payload.get("caseCount") != 11:
            failures.append(f"CLI JSON payload mismatch: {payload}")
        else:
            print("OK: CLI `repair_rounds.py run --repair rules --json` passes")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All repair-rounds benchmark tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
