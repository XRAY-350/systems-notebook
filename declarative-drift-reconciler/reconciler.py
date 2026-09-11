#!/usr/bin/env python3
"""
reconciler.py — declarative infrastructure-state checking with self-heal, generalized from a
server-reconciliation tool I run against production infrastructure.

The idea: describe what SHOULD be true about a system in one small declarative manifest (a list of
{name, live_path, source_path} entries — "this live file should match this source-of-truth file"),
then check that manifest against actual reality and report exactly one of three outcomes per entry:

    GREEN  — live matches source, nothing to do
    RED    — live is missing or has drifted from source; a fix command is available and safe
    YELLOW — the check itself couldn't run (e.g. the source file doesn't exist to compare against)

Optionally APPLIES the safe fixes (copy source -> live) and re-checks, so routine drift heals
itself silently, and only genuine, un-fixable problems surface for a human to see.

This is deliberately a generic, standalone version: the original tool also checks OS-level state
(installed packages, file ownership, cron entries, running services, TLS certificate expiry) via
a pluggable "run a command and inspect the result" abstraction over SSH-or-local execution. That
plumbing is infrastructure-specific glue, left out here; what's shown is the core idea that
generalizes — hash-based drift detection with a bounded, explicit, auditable fix path — reduced to
its simplest real form: comparing local files.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"


@dataclass
class Finding:
    tier: str
    name: str
    message: str
    fix: "callable | None" = None


@dataclass
class Report:
    findings: list = field(default_factory=list)

    def add(self, tier, name, message, fix=None):
        self.findings.append(Finding(tier, name, message, fix))

    def counts(self):
        out = {GREEN: 0, YELLOW: 0, RED: 0}
        for f in self.findings:
            out[f.tier] += 1
        return out


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def check_entry(entry: dict, report: Report) -> None:
    """One manifest entry: {name, live_path, source_path}. Compares by content hash, not mtime —
    a file that was touched but never actually changed should read as GREEN, not RED."""
    live = Path(entry["live_path"])
    source = Path(entry["source_path"])
    name = entry["name"]

    want = file_sha256(source)
    if want is None:
        report.add(YELLOW, name, f"source file not found: {source} (can't compare)")
        return

    have = file_sha256(live)

    def fix():
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, live)

    if have is None:
        report.add(RED, name, f"{live} is MISSING (not deployed)", fix=fix)
    elif have != want:
        report.add(RED, name, f"{live} has DRIFTED from {source}", fix=fix)
    else:
        report.add(GREEN, name, f"{live} matches {source}")


def reconcile(manifest: list[dict], apply_fixes: bool = False) -> Report:
    """Check every entry; if apply_fixes, apply each RED finding's fix and re-check that one entry
    so the returned report reflects POST-fix state, not the state that triggered the fix."""
    report = Report()
    for entry in manifest:
        check_entry(entry, report)

    if not apply_fixes:
        return report

    # Re-check only entries that had a fix applied — this keeps the report's meaning consistent
    # (GREEN really means "confirmed matching right now", not "matched before I tried to fix it").
    final = Report()
    for entry, finding in zip(manifest, report.findings):
        if finding.tier == RED and finding.fix is not None:
            try:
                finding.fix()
            except OSError as e:
                final.add(RED, finding.name, f"{finding.message} (fix failed: {e})")
                continue
        check_entry(entry, final)
    return final


def print_report(report: Report) -> None:
    counts = report.counts()
    for f in report.findings:
        print(f"[{f.tier}] {f.name}: {f.message}")
    print(f"\n{counts[GREEN]} green / {counts[YELLOW]} yellow / {counts[RED]} red")
