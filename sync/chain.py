#!/usr/bin/env python3
"""Append-only enforcement for the fact logs.

GitHub branch protection needs a paid plan on a private repo, so the guarantee is
built here instead: every machine keeps a local anchor = (number of lines,
sha256 of those bytes) for EVERY agent's log. Verification is a prefix check, so

  * truncation (lines < anchored)            -> tamper
  * rewriting of already-seen history        -> tamper
  * normal growth (lines > anchored)         -> fine, anchor advances

Anchors live OUTSIDE the repo (~/.hermes/brain-anchors/) on purpose: whoever can
push to the repo must not be able to move the anchor that judges them.

Commands
    chain.py verify     # exit 0 clean, 3 tampered, 4 bad state
    chain.py update     # advance anchors to what is on disk (after a good pull)
    chain.py accept     # re-anchor after a human has looked (deliberate override)
    chain.py status
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
BRAIN = HERMES / "brain"
FACT_DIR = BRAIN / "facts"
ANCHOR_DIR = HERMES / "brain-anchors"


def anchor_path(name: str) -> Path:
    return ANCHOR_DIR / ("%s.json" % name)


def digest_lines(raw: bytes, upto: int | None = None) -> tuple[int, str]:
    """(line_count, sha256 over the first `upto` lines' raw bytes)."""
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    n = len(lines)
    if upto is not None:
        lines = lines[:upto]
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln)
        h.update(b"\n")
    return n, h.hexdigest()


def load_anchor(name: str) -> dict | None:
    p = anchor_path(name)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return {"lines": int(d["lines"]), "sha256": str(d["sha256"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_anchor(name: str, lines: int, sha: str) -> None:
    ANCHOR_DIR.mkdir(parents=True, exist_ok=True)
    p = anchor_path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"lines": lines, "sha256": sha}, indent=0))
    os.replace(tmp, p)


def logs() -> list[Path]:
    return sorted(FACT_DIR.glob("*.jsonl")) if FACT_DIR.exists() else []


def verify(verbose: bool = True) -> list[str]:
    problems: list[str] = []
    for f in logs():
        name = f.name
        raw = f.read_bytes()
        total, _ = digest_lines(raw)
        a = load_anchor(name)
        if a is None:
            if verbose:
                print("chain: %s has no anchor yet (will be created on first update)" % name)
            continue
        if total < a["lines"]:
            problems.append("%s: truncated %d -> %d lines" % (name, a["lines"], total))
            continue
        _, prefix = digest_lines(raw, a["lines"])
        if prefix != a["sha256"]:
            problems.append("%s: history rewritten (first %d lines no longer match)"
                            % (name, a["lines"]))
    if verbose:
        if problems:
            print("chain: TAMPER DETECTED")
            for p in problems:
                print("  " + p)
        else:
            print("chain: ok (%d log(s) verified)" % len(logs()))
    return problems


def update() -> int:
    changed = 0
    for f in logs():
        name = f.name
        raw = f.read_bytes()
        total, sha = digest_lines(raw)
        a = load_anchor(name)
        if a and total < a["lines"]:
            print("chain: refusing to shrink anchor for %s (%d -> %d)" % (name, a["lines"], total))
            continue
        if a and a["lines"] == total and a["sha256"] == sha:
            continue
        save_anchor(name, total, sha)
        changed += 1
    print("chain: anchors updated for %d log(s)" % changed)
    return 0


def accept() -> int:
    n = 0
    for f in logs():
        raw = f.read_bytes()
        total, sha = digest_lines(raw)
        save_anchor(f.name, total, sha)
        n += 1
    print("chain: accepted current state as truth for %d log(s)" % n)
    return 0


def status() -> int:
    for f in logs():
        raw = f.read_bytes()
        total, sha = digest_lines(raw)
        a = load_anchor(f.name)
        if not a:
            state = "no-anchor"
        elif total < a["lines"]:
            state = "TRUNCATED"
        elif digest_lines(raw, a["lines"])[1] != a["sha256"]:
            state = "REWRITTEN"
        elif total > a["lines"]:
            state = "grown +%d" % (total - a["lines"])
        else:
            state = "ok"
        print("%-16s lines=%-4d anchor=%-4s %s" % (f.name, total, a["lines"] if a else "-", state))
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "verify":
        return 3 if verify() else 0
    if cmd == "update":
        return update()
    if cmd == "accept":
        return accept()
    if cmd == "status":
        return status()
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main())
