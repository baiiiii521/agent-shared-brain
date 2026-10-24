#!/usr/bin/env python3
"""Agent-to-agent mailbox, work journal and hand-off queue.

Deliberately dumb: one append-only JSONL file per agent, nothing else. Two
machines never write the same file, so there is nothing to merge and nothing to
conflict — the same trick the fact store uses.

    mailbox/<agent>.jsonl      messages SENT by <agent>
    mailbox/<agent>.read.json  ids <agent> has consumed
    journal/<date>-<agent>.md  per-agent work log
    pending/<id>.json          a hand-off addressed to one agent

Commands
    mailbox.py send "text" [--to beta] [--kind note|question|answer|progress]
    mailbox.py inbox [--unread] [--limit N]
    mailbox.py ack <msg-id>...
    mailbox.py log "text"
    mailbox.py pending add "text" --for beta
    mailbox.py pending list
    mailbox.py pending take <id>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
BRAIN = HERMES / "brain"
MAILBOX = BRAIN / "mailbox"
JOURNAL = BRAIN / "journal"
PENDING = BRAIN / "pending"


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def agent() -> str:
    return (BRAIN / ".agent-id").read_text().strip()


def git(*a) -> int:
    return subprocess.run(("git",) + a, cwd=str(BRAIN), capture_output=True, text=True).returncode


def commit_push(message: str) -> bool:
    """Stage only what this program owns, then push with a rebase retry."""
    git("add", "--", "mailbox", "journal", "pending")
    code = git("commit", "-q", "-m", message)
    if code != 0:
        return True                      # nothing to commit is fine
    for _ in range(3):
        if git("push", "-q") == 0:
            return True
        if git("pull", "--rebase", "--autostash", "-q") != 0:
            git("rebase", "--abort")
            return False
    return False


def cmd_send(args) -> int:
    me = agent()
    msg = {"id": uuid.uuid4().hex[:12], "ts": now(), "from": me, "to": args.to,
           "kind": args.kind, "text": args.text.strip()}
    MAILBOX.mkdir(parents=True, exist_ok=True)
    with (MAILBOX / ("%s.jsonl" % me)).open("a") as fh:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    ok = commit_push("mail(%s->%s) %s" % (me, args.to, msg["id"]))
    print(("sent %s -> %s (%s)" if ok else
           "NOT PUSHED %s -> %s (%s) — the other side cannot see it yet")
          % (msg["id"], args.to, args.kind))
    return 0 if ok else 3


def read_acked(me: str) -> set[str]:
    p = MAILBOX / ("%s.read.json" % me)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()).get("acked", []))
    except (OSError, ValueError):
        return set()


def cmd_inbox(args) -> int:
    me = agent()
    acked = read_acked(me)
    msgs = []
    for f in sorted(MAILBOX.glob("*.jsonl")) if MAILBOX.exists() else []:
        if f.stem == me:
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("to") not in (me, "all"):
                continue
            if args.unread and m["id"] in acked:
                continue
            msgs.append(m)
    msgs.sort(key=lambda m: m.get("ts", ""))
    if args.limit:
        msgs = msgs[-args.limit:]
    if not msgs:
        print("(inbox empty)")
        return 0
    for m in msgs:
        flag = " " if m["id"] in acked else "*"
        print("%s [%s] %s %s>%s: %s" % (flag, m["id"], m["ts"], m.get("from"),
                                        m.get("to"), m.get("text")))
    return 0


def cmd_ack(args) -> int:
    me = agent()
    acked = read_acked(me)
    acked.update(args.ids)
    MAILBOX.mkdir(parents=True, exist_ok=True)
    (MAILBOX / ("%s.read.json" % me)).write_text(
        json.dumps({"acked": sorted(acked)}, ensure_ascii=False, indent=0))
    ok = commit_push("ack(%s) %d" % (me, len(args.ids)))
    print("acked %d (total %d)%s" % (len(args.ids), len(acked), "" if ok else " — NOT PUSHED"))
    return 0 if ok else 3


def cmd_log(args) -> int:
    me = agent()
    JOURNAL.mkdir(parents=True, exist_ok=True)
    with (JOURNAL / ("%s-%s.md" % (today(), me))).open("a") as fh:
        fh.write("- %s %s\n" % (datetime.now().strftime("%H:%M"), args.text.strip()))
    ok = commit_push("journal(%s) %s" % (me, today()))
    print("logged%s" % ("" if ok else " — NOT PUSHED"))
    return 0 if ok else 3


def cmd_pending(args) -> int:
    me = agent()
    PENDING.mkdir(parents=True, exist_ok=True)
    if args.op == "add":
        pid = uuid.uuid4().hex[:8]
        (PENDING / ("%s.json" % pid)).write_text(json.dumps({
            "id": pid, "created": now(), "from": me, "for": args.for_agent,
            "text": args.text, "state": "open"}, ensure_ascii=False, indent=1))
        ok = commit_push("pending(%s) %s" % (me, pid))
        print("pending %s -> %s%s" % (pid, args.for_agent, "" if ok else " — NOT PUSHED"))
        return 0 if ok else 3
    if args.op == "take":
        f = PENDING / ("%s.json" % args.id)
        if not f.exists():
            print("no such id")
            return 1
        d = json.loads(f.read_text())
        d.update({"state": "taken", "taken_by": me, "taken_at": now()})
        f.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        commit_push("pending-take(%s) %s" % (me, args.id))
        print("taken", args.id)
        return 0
    for f in sorted(PENDING.glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("state") == "open":
            print("%s [%s] for=%s %s" % (d["id"], d["created"], d.get("for"),
                                         d.get("text", "")[:120]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mailbox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("text")
    s.add_argument("--to", default="beta")
    s.add_argument("--kind", default="note", choices=["note", "question", "answer", "progress"])
    s.set_defaults(fn=cmd_send)
    s = sub.add_parser("inbox")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_inbox)
    s = sub.add_parser("ack")
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_ack)
    s = sub.add_parser("log")
    s.add_argument("text")
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("pending")
    s.add_argument("op", nargs="?", default="list", choices=["add", "list", "take"])
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--for", dest="for_agent", default="beta")
    s.add_argument("--id", default="")
    s.set_defaults(fn=cmd_pending)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
