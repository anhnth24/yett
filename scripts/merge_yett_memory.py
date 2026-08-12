#!/usr/bin/env python3
"""Merge a new vulnerability finding into the yett flagged-vulnerabilities memory."""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EXISTING_MEMORY_PATH = Path(__file__).resolve().parent / "existing_yett_memory.json"

NEW_FINDING = {
    "severity": "medium",
    "location": "src/yett/tools/remote/ssh_exec.py",
    "title": "log_read lines parameter accepts negative values, bypassing the intended tail line cap",
    "description": "`LogReadTool` casts `lines` with `int(args.get('lines', 200))` and does not reject negative values. It builds `tail -n {lines} {path}` and runs it on the remote host. GNU `tail -n -N` returns all lines except the last N, so a large negative value (e.g. `-999999`) returns nearly the entire log file instead of the default 200-line cap.",
    "impact": "An attacker who can influence agent tool arguments (prompt injection, compromised channel message, malicious task) can exfiltrate full remote log files within allowed `log_paths`, bypassing the volume limit designed to reduce secret/PII leakage from logs.",
    "verified": True,
    "status": "active",
    "background_composer_id": "bc-0c7d3fdc-1d4f-4bd8-b194-f0ac8238393a",
    "commit_hash": "5b99511906015a60fc64cdbe4a02e8c6536087aa",
    "detected_at_pst": "USE_CURRENT_PST_TIMESTAMP",
    "reported_link": "",
    "attack_path": "1. Deployment registers an SSH host with `log_paths` and allows `log_read` in policy.\n2. Attacker causes the agent to call `log_read` with `path` inside `log_paths` and `lines: -999999`.\n3. `BasicGate._gate_log_read()` returns allow without inspecting `lines`.\n4. `LogReadTool` runs `tail -n -999999 <path>` on the remote server via SSH.\n5. Nearly the entire log file is returned in the agent response instead of the intended 200-line maximum.",
    "evidence": [
        "src/yett/tools/remote/ssh_exec.py:99-101",
        "src/yett/security/basic_gate.py:71-83",
        "src/yett/security/cmdguard.py:174-204",
    ],
    "remediation": "Validate `lines` is a positive integer within a configured maximum (e.g. `1 <= lines <= 500`) before building the `tail` command; reject zero and negative values.",
    "poc_video_notes": "Validated in-process: `gate.evaluate('log_read', {..., 'lines': -999999})` returns allow; constructed command is `tail -n -999999 /var/log/app/e.log`. Video not recorded in this headless scan environment.",
}


def current_pst_timestamp() -> str:
    """Return the current time formatted as an ISO 8601 string in US/Pacific."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return now.strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":" + now.strftime("%z")[-2:]


def merge_findings(memory: dict, new_finding: dict) -> dict:
    """Append new_finding to memory['findings'] unless a duplicate title exists."""
    findings = memory.setdefault("findings", [])
    existing_titles = {finding.get("title") for finding in findings}

    if new_finding["title"] in existing_titles:
        return memory

    finding = dict(new_finding)
    if finding.get("detected_at_pst") == "USE_CURRENT_PST_TIMESTAMP":
        finding["detected_at_pst"] = current_pst_timestamp()

    findings.append(finding)
    return memory


def main() -> int:
    with EXISTING_MEMORY_PATH.open(encoding="utf-8") as handle:
        memory = json.load(handle)

    merged = merge_findings(memory, NEW_FINDING)
    json.dump(merged, sys.stdout, indent=4)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
