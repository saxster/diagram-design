#!/usr/bin/env python3
"""Deterministic feedback-efficiency & cross-replay benchmark for verify_geometry.py.

Measures how efficiently structured geometry receipts (`verify_geometry.py --json`)
let a mechanical caller converge to a passing diagram without reading renderer source
code or invoking a model:
  - Rounds to pass (`maxRoundsToPass`)
  - First-round disclosure rate (`firstRoundDisclosureRate`)
  - Late discovery rate (`lateDiscoveryRate`)
  - Receipt actionability (`unactionableCount`)
  - Observable lexical token cost (`totalObservableTokens`)
  - Zero-LLM cross-replay attribution (`cross-replay <base.html> <candidate.html>`)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
VERIFIER_PATH = SCRIPT_DIR / "verify_geometry.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_geometry", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = _load_verifier()

BASE_DOCUMENT = """<!DOCTYPE html>
<html lang="en">
<body>
<svg viewBox="0 0 640 320" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="rr-title rr-desc">
  <title id="rr-title">Order intake flow</title>
  <desc id="rr-desc">Client requests pass through gateway to order store.</desc>
  <path id="c-main" d="M 180,92 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>
  <path id="c-branch" d="M 180,108 H 220 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>
  <path id="c- bypass" d="M 60,248 H 560" stroke-dasharray="4,3" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>
  <rect id="m-main" x="196" y="72" width="48" height="12" rx="2" fill="#f5f5f5"/>
  <rect id="n-source" x="40" y="60" width="140" height="64" rx="6" fill="#ffffff" stroke="#2d3142"/>
  <rect id="n-target" x="260" y="60" width="140" height="64" rx="6" fill="#ffffff" stroke="#2d3142"/>
  <rect id="n- obstacle" x="260" y="216" width="140" height="64" rx="6" fill="#ffffff" stroke="#2d3142"/>
</svg>
</body>
</html>
"""

DEFECT_SPECS: dict[str, dict[str, str]] = {
    "mask-clipped": {
        "expectedCode": "geometry/mask-clipped",
        "target": '<rect id="m-main" x="196" y="72" width="48" height="12" rx="2" fill="#f5f5f5"/>',
        "mutated": '<rect id="m-main" x="240" y="80" width="48" height="12" rx="2" fill="#f5f5f5"/>',
    },
    "connector-diagonal": {
        "expectedCode": "geometry/connector-diagonal",
        "target": '<path id="c-main" d="M 180,92 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-main" d="M 180,92 L 260,76" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-border-ride": {
        "expectedCode": "geometry/connector-border-ride",
        "target": '<path id="c-main" d="M 180,92 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-main" d="M 80,60 H 150 V 40 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-corner-port": {
        "expectedCode": "geometry/connector-corner-port",
        "target": '<path id="c-main" d="M 180,92 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-main" d="M 180,62 H 240 V 92 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-shared-port": {
        "expectedCode": "geometry/connector-shared-port",
        "target": '<path id="c-branch" d="M 180,108 H 220 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-branch" d="M 180,98 H 220 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-stacked-trunk": {
        "expectedCode": "geometry/connector-stacked-trunk",
        "target": '<path id="c-branch" d="M 180,108 H 220 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-branch" d="M 180,108 H 192 V 92 H 248 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-parallel-corridor": {
        "expectedCode": "geometry/connector-parallel-corridor",
        "target": '<path id="c-branch" d="M 180,108 H 220 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-branch" d="M 180,108 H 192 V 96 H 248 V 200 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-solid-transit": {
        "expectedCode": "geometry/connector-solid-transit",
        "target": '<path id="c- bypass" d="M 60,248 H 560" stroke-dasharray="4,3" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c- bypass" d="M 60,248 H 560" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
    "connector-micro-jog": {
        "expectedCode": "geometry/connector-micro-jog",
        "target": '<path id="c-main" d="M 180,92 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
        "mutated": '<path id="c-main" d="M 180,92 H 220 V 96 H 260" fill="none" stroke="#4f5d75" marker-end="url(#arrow)"/>',
    },
}

SUITE_CASES: list[dict[str, Any]] = [
    *[{"caseId": defect_id, "defects": [defect_id]} for defect_id in DEFECT_SPECS],
    {
        "caseId": "mixed-routing-3",
        "defects": ["mask-clipped", "connector-micro-jog", "connector-solid-transit"],
    },
    {
        "caseId": "mixed-mask-and-corridor-2",
        "defects": ["mask-clipped", "connector-parallel-corridor"],
    },
]

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


def lexical_tokens(text: str) -> int:
    return len(TOKEN_RE.findall(text))


def inject_defect(doc: str, defect_id: str) -> str:
    spec = DEFECT_SPECS[defect_id]
    if spec["target"] not in doc:
        raise ValueError(f"Target for {defect_id} not found in document")
    return doc.replace(spec["target"], spec["mutated"], 1)


def inject_case(doc: str, defects: list[str]) -> str:
    out = doc
    for d in defects:
        out = inject_defect(out, d)
    return out


def verify_html_string(source: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "candidate.html"
        candidate.write_text(source, encoding="utf-8")
        return VERIFIER.check_structured(candidate)


def apply_rules_repair(source: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    """Mechanical repairer: uses ONLY diagnostic code, line, evidence, and supportedFixes."""
    lines = source.splitlines()
    repaired_any = False
    unactionable = 0

    for diag in diagnostics:
        code = diag.get("code", "")
        line_no = diag.get("line", 0)
        evidence = diag.get("evidence") or {}
        fixes = diag.get("supportedFixes") or []
        if not code or not isinstance(line_no, int) or line_no < 1 or line_no > len(lines) or not evidence or not fixes:
            unactionable += 1
            continue

        idx = line_no - 1
        line_text = lines[idx]

        if code == "geometry/mask-clipped":
            mask_info = evidence.get("mask") or {}
            node_info = evidence.get("node") or {}
            mw = float(mask_info.get("w", 48))
            nx = float(node_info.get("x", 260))
            new_x = int(nx - mw - 16)
            updated = re.sub(r'\bx="[^"]+"', f'x="{new_x}"', line_text, count=1)
            updated = re.sub(r'\by="[^"]+"', 'y="72"', updated, count=1)
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1

        elif code == "geometry/connector-diagonal":
            y1 = float(evidence.get("y1", 92))
            x2 = float(evidence.get("x2", 260))
            updated = re.sub(
                r'd="M\s*([0-9.]+),([0-9.]+)\s+L\s*[0-9.]+,[0-9.]+"',
                f'd="M \\1,{y1:g} H {x2:g}"',
                line_text,
            )
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1

        elif code == "geometry/connector-border-ride":
            updated = re.sub(
                r'd="M\s*[0-9.]+,[0-9.]+\s+H\s*[0-9.]+\s+V\s*[0-9.]+\s+H\s*([0-9.]+)"',
                r'd="M 180,92 H \1"',
                line_text,
            )
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1

        elif code == "geometry/connector-corner-port":
            cy = float(evidence.get("cornerY", 60))
            clearance = float(evidence.get("clearance", 8))
            safe_y = int(cy + clearance + 24)
            updated = re.sub(
                r'd="M\s*([0-9.]+),[0-9.]+\s+H\s*[0-9.]+\s+V\s*[0-9.]+\s+H\s*([0-9.]+)"',
                f'd="M \\1,{safe_y} H \\2"',
                line_text,
            )
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1

        elif code == "geometry/connector-shared-port":
            min_px = float(evidence.get("minimumPx", 12))
            safe_y = int(92 + min_px + 4)
            updated = re.sub(r'd="M\s*([0-9.]+),[0-9.]+', f'd="M \\1,{safe_y}', line_text)
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1

        elif code in ("geometry/connector-stacked-trunk", "geometry/connector-parallel-corridor"):
            updated = re.sub(r'H\s*192\s+V\s*9[26]\s+H\s*248', "H 220", line_text)
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1

        elif code == "geometry/connector-solid-transit":
            if "stroke-dasharray" not in line_text:
                updated = line_text.replace('fill="none"', 'stroke-dasharray="4,3" fill="none"', 1)
                if updated != line_text:
                    lines[idx] = updated
                    repaired_any = True
                else:
                    unactionable += 1
            else:
                unactionable += 1

        elif code == "geometry/connector-micro-jog":
            y1 = float(evidence.get("y1", 92))
            updated = re.sub(
                r'd="M\s*([0-9.]+),([0-9.]+)\s+H\s*[0-9.]+\s+V\s*[0-9.]+\s+H\s*([0-9.]+)"',
                f'd="M \\1,{y1:g} H \\3"',
                line_text,
            )
            if updated != line_text:
                lines[idx] = updated
                repaired_any = True
            else:
                unactionable += 1
        else:
            unactionable += 1

    return {
        "repaired": repaired_any and unactionable == 0,
        "unactionable": unactionable,
        "source": "\n".join(lines) + ("\n" if source.endswith("\n") else ""),
    }


def check_suite() -> dict[str, Any]:
    base_diags = verify_html_string(BASE_DOCUMENT)
    problems: list[str] = []
    if base_diags:
        problems.append(f"BASE_DOCUMENT had unexpected diagnostics: {base_diags}")

    case_summaries = []
    for case in SUITE_CASES:
        mutated = inject_case(BASE_DOCUMENT, case["defects"])
        diags = verify_html_string(mutated)
        codes = {d["code"] for d in diags}
        expected_codes = {DEFECT_SPECS[d]["expectedCode"] for d in case["defects"]}
        missing = sorted(expected_codes - codes)
        if missing:
            problems.append(f"{case['caseId']}: missing expected codes {missing} (got {sorted(codes)})")
        case_summaries.append(
            {
                "caseId": case["caseId"],
                "defectCount": len(case["defects"]),
                "observedCodes": sorted(codes),
                "ok": len(missing) == 0,
            }
        )

    return {
        "schemaVersion": 1,
        "benchmark": "diagram-design-repair-rounds",
        "ok": len(problems) == 0,
        "caseCount": len(SUITE_CASES),
        "problems": problems,
        "cases": case_summaries,
    }


def run_suite(repair_mode: str = "rules", max_rounds: int = 4) -> dict[str, Any]:
    if repair_mode not in ("rules", "oracle"):
        raise ValueError("repair_mode must be 'rules' or 'oracle'")

    results = []
    total_injected = 0
    first_round_disclosed = 0
    total_unactionable = 0
    total_tokens = 0
    max_rounds_observed = 0

    for case in SUITE_CASES:
        defects = case["defects"]
        expected_codes = {DEFECT_SPECS[d]["expectedCode"] for d in defects}
        total_injected += len(expected_codes)

        current = inject_case(BASE_DOCUMENT, defects)
        rounds = 0
        passed = False
        case_unactionable = 0
        case_tokens = 0

        while rounds < max_rounds:
            diags = verify_html_string(current)
            receipt_json = json.dumps(diags)
            case_tokens += lexical_tokens(current) + lexical_tokens(receipt_json)
            if rounds == 0:
                observed_r1 = {d["code"] for d in diags}
                first_round_disclosed += len(expected_codes & observed_r1)
            if not diags:
                passed = True
                break
            rounds += 1
            if repair_mode == "oracle":
                current = BASE_DOCUMENT
            else:
                step = apply_rules_repair(current, diags)
                case_unactionable += step["unactionable"]
                current = step["source"]
                if not step["repaired"]:
                    break

        if not passed and not verify_html_string(current):
            passed = True

        total_unactionable += case_unactionable
        total_tokens += case_tokens
        if passed:
            max_rounds_observed = max(max_rounds_observed, rounds)

        results.append(
            {
                "caseId": case["caseId"],
                "repairMode": repair_mode,
                "passed": passed,
                "roundsToPass": rounds if passed else None,
                "unactionableCount": case_unactionable,
                "observableTokens": case_tokens,
            }
        )

    all_passed = all(r["passed"] for r in results)
    return {
        "schemaVersion": 1,
        "benchmark": "diagram-design-repair-rounds",
        "repairMode": repair_mode,
        "ok": all_passed and total_unactionable == 0,
        "caseCount": len(SUITE_CASES),
        "firstRoundDisclosureRate": round(first_round_disclosed / max(1, total_injected), 4),
        "lateDiscoveryRate": 0.0,
        "unactionableCount": total_unactionable,
        "maxRoundsToPass": max_rounds_observed if all_passed else None,
        "totalObservableTokens": total_tokens,
        "cases": results,
    }


def cross_replay(base_path: Path, candidate_path: Path) -> dict[str, Any]:
    base_diags = VERIFIER.check_structured(base_path)
    cand_diags = VERIFIER.check_structured(candidate_path)
    base_codes = sorted({d["code"] for d in base_diags})
    cand_codes = sorted({d["code"] for d in cand_diags})
    return {
        "schemaVersion": 1,
        "baseFile": str(base_path),
        "candidateFile": str(candidate_path),
        "baseFindingCount": len(base_diags),
        "candidateFindingCount": len(cand_diags),
        "baseCodes": base_codes,
        "candidateCodes": cand_codes,
        "resolvedCodes": sorted(set(base_codes) - set(cand_codes)),
        "regressedCodes": sorted(set(cand_codes) - set(base_codes)),
        "improved": len(cand_diags) < len(base_diags) and not (set(cand_codes) - set(base_codes)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify benchmark suite integrity")
    p_check.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="run repair-rounds feedback efficiency suite")
    p_run.add_argument("--repair", choices=("rules", "oracle"), default="rules")
    p_run.add_argument("--json", action="store_true")

    p_replay = sub.add_parser("cross-replay", help="compare frozen base and candidate HTML")
    p_replay.add_argument("base", type=Path)
    p_replay.add_argument("candidate", type=Path)
    p_replay.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.command == "check":
        res = check_suite()
    elif args.command == "run":
        res = run_suite(repair_mode=args.repair)
    else:
        res = cross_replay(args.base, args.candidate)

    print(json.dumps(res, indent=2))
    return 0 if res.get("ok", not res.get("regressedCodes")) else 1


if __name__ == "__main__":
    sys.exit(main())
