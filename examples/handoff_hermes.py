#!/usr/bin/env python3
"""Hermes-specific: record what this machine was working on, so the always-on
instance knows what happened when the lid closed.

Reads Hermes' own session store (state.db). `messages.timestamp` is a Unix
FLOAT in that schema — comparing it to a formatted date string silently returns
nothing. That mistake kept this feature dead for a day.

Drop this next to the other scripts and call it from brain_sync.sh:
    python3 "$BRAIN/sync/handoff_hermes.py" handoff
"""
import os, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
DB = HERMES / "state.db"
BRAIN = HERMES / "brain"


def recent(minutes=180, limit=3):
    if not DB.exists():
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=3)
        rows = con.execute(
            "SELECT content FROM messages WHERE role='user' AND timestamp >= ?"
            " ORDER BY timestamp DESC LIMIT 40", (time.time() - minutes * 60,)).fetchall()
    except Exception:
        return []
    out = []
    for (c,) in reversed(rows):
        t = " ".join(str(c or "").split())
        if t and len(t) > 4:
            out.append(t[:110])
    return out[-limit:]


def main():
    turns = recent()
    if not turns:
        return 0
    agent = (BRAIN / ".agent-id").read_text().strip()
    day = datetime.now().strftime("%Y-%m-%d")
    d = BRAIN / "journal"
    d.mkdir(parents=True, exist_ok=True)
    with (d / ("%s-%s.md" % (day, agent))).open("a") as fh:
        fh.write("- %s [handoff] last working on: %s\n"
                 % (datetime.now().strftime("%H:%M"), " | ".join(turns)))
    subprocess.run(("git", "add", "--", "journal"), cwd=str(BRAIN))
    subprocess.run(("git", "commit", "-q", "-m", "handoff"), cwd=str(BRAIN))
    subprocess.run(("git", "push", "-q"), cwd=str(BRAIN))
    return 0


if __name__ == "__main__":
    sys.exit(main())
