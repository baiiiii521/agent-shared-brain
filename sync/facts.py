#!/usr/bin/env python3
"""Fact store for the shared brain — event-sourced, bi-temporal.

Model: every change is an explicit operation appended to the agent's OWN log
(facts/<agent>.jsonl). Two machines never write the same file, so there is no
merge conflict, and the fold is deterministic regardless of who syncs first.
Facts are invalidated (valid_until), never deleted, so history stays queryable.

Ops (one JSON object per line):
    {"op":"add","id":"f_x","scope":"memory","text":"...","ts":"...","by":"alpha","seq":12}
    {"op":"update","id":"f_x","text":"..."}
    {"op":"forget","ids":["f_x"]}       # invalidate; history kept
    {"op":"restore","id":"f_x"}         # bring a forgotten fact back
    {"op":"pin","id":"f_x"}             # identity row: reserved budget, not auto-deleted
    {"op":"use","id":"f_x"}             # touch: ranking boost

Sync is a THREE-WAY merge:
    base  = {id: text-hash} of what WE rendered last time (.state/render-*.json)
    live  = what Hermes has on disk now
    facts = fold of every op
  * in base, missing from live -> deleted (or rewritten) by the user -> forget/update
  * in live, in neither base nor facts -> new -> add
  * dropped by the BUDGET -> not in base -> never mistaken for a deletion
  * no base at all -> back up live, render only, never absorb

Hard-won rules baked in here:
  * the whole read -> absorb -> render -> write -> marker sequence runs while
    holding Hermes' own <file>.lock, otherwise a write landing in between is lost
  * ops carry a per-agent monotonic `seq`, so two ops in the same second still
    fold in the order they were written instead of by dictionary luck
  * logs are validated (shape, op, scope, by == filename); a bad line blocks the
    render instead of silently shrinking memory
  * git stages only the data paths it owns — `git add -A` used to sweep unrelated
    working-tree edits into a "facts sync" commit
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
BRAIN = HERMES / "brain"
MEM_DIR = HERMES / "memories"
FACT_DIR = BRAIN / "facts"
SEP = "§"
SCOPES = {"memory": "MEMORY.md", "user": "USER.md"}
LIMIT_KEY = {"memory": "memory_char_limit", "user": "user_char_limit"}
DEFAULT_LIMIT = {"memory": 6000, "user": 3000}
PIN_PATTERNS = [
    r"^\*\*\s*(Name|What to call|Pronouns|Timezone|Birth|Passport|Work)",
    r"^Home Address:",
    r"红线",
    r"^\*\*Name:\*\*",
]
VALID_OPS = {"add", "update", "forget", "restore", "pin", "use"}
# only these paths belong to a facts commit
OWNED_PATHS = ["facts", "overflow", "policy.json"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def epoch(ts) -> float:
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    if d.tzinfo is None:               # never let the local zone decide ordering
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def agent() -> str:
    return (BRAIN / ".agent-id").read_text().strip()


def git(*a) -> int:
    return subprocess.run(("git",) + a, cwd=str(BRAIN), capture_output=True, text=True).returncode


def git_out(*a) -> str:
    p = subprocess.run(("git",) + a, cwd=str(BRAIN), capture_output=True, text=True)
    return (p.stdout + p.stderr).strip()


def norm(t) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def h16(t) -> str:
    return hashlib.sha256(norm(t).encode()).hexdigest()[:16]


def fid_for(scope: str, text: str) -> str:
    """Content-addressed id: two machines adding the same sentence converge."""
    return "f_" + hashlib.sha256(("%s\0%s" % (scope, norm(text))).encode()).hexdigest()[:10]


def split_entries(text: str) -> list[str]:
    parts = re.split(r"^\s*%s\s*$" % re.escape(SEP), text or "", flags=re.M)
    return [p.strip() for p in parts if p.strip()]


def is_pinned(text: str) -> bool:
    return any(re.search(p, norm(text)) for p in PIN_PATTERNS)


def limits() -> dict:
    policy = BRAIN / "policy.json"
    if policy.exists():
        try:
            d = json.loads(policy.read_text())
            lim = {k: int(d["limits"][k]) for k in SCOPES}
            if all(200 <= v <= 50000 for v in lim.values()):
                return {k: int(v * 0.9) for k, v in lim.items()}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    cfg = HERMES / "config.yaml"
    out = dict(DEFAULT_LIMIT)
    try:
        text = cfg.read_text()
    except OSError:
        text = ""
    for k in out:
        m = re.search(r"^\s*%s:\s*(\d+)\s*$" % LIMIT_KEY[k], text, re.M)
        if m:
            out[k] = int(m.group(1))
    return {k: int(v * 0.9) for k, v in out.items()}


# ---------------------------------------------------------------- log i/o

def next_seq() -> int:
    p = BRAIN / ".state" / ("seq-%s" % agent())
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        cur = 0
    cur += 1
    p.write_text(str(cur))
    return cur


def append_ops(ops: list[dict]) -> None:
    if not ops:
        return
    FACT_DIR.mkdir(parents=True, exist_ok=True)
    me = agent()
    p = FACT_DIR / ("%s.jsonl" % me)
    with p.open("a") as fh:
        for op in ops:
            op.setdefault("ts", now())
            op["by"] = me                 # never trust a caller-supplied origin
            op["seq"] = next_seq()
            fh.write(json.dumps(op, ensure_ascii=False) + "\n")


def validate(op, owner: str) -> str | None:
    """Return an error string, or None when the line is acceptable."""
    if not isinstance(op, dict):
        return "not an object"
    kind = op.get("op")
    if kind not in VALID_OPS:
        return "unknown op %r" % (kind,)
    by = op.get("by")
    if by != owner:
        return "forged origin %r (file is %s)" % (by, owner)
    fid = op.get("id")
    ids = op.get("ids")
    if kind in ("add", "update", "restore", "pin", "use"):
        if not isinstance(fid, str) or not fid.startswith("f_"):
            return "bad id"
    if kind == "forget" and not (fid or (isinstance(ids, list) and ids)):
        return "forget without id/ids"
    if kind in ("add", "update"):
        if not isinstance(op.get("text"), str) or not norm(op.get("text")):
            return "empty text"
        if op.get("scope") not in SCOPES:
            return "bad scope %r" % (op.get("scope"),)
    if kind == "add" and op.get("scope") not in SCOPES:
        return "bad scope"
    return None


def read_all() -> tuple[list[dict], list[str]]:
    """Return (ops, problems). A non-empty problems list blocks the render:
    silently dropping ops would make the two machines disagree."""
    ops: list[dict] = []
    problems: list[str] = []
    if not FACT_DIR.exists():
        return ops, problems
    for f in sorted(FACT_DIR.glob("*.jsonl")):
        owner = f.stem
        for lineno, line in enumerate(f.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                op = json.loads(line)
            except ValueError:
                problems.append("%s:%d not JSON" % (f.name, lineno))
                continue
            err = validate(op, owner)
            if err:
                problems.append("%s:%d %s" % (f.name, lineno, err))
                continue
            ops.append(op)
    rank = {"add": 0, "update": 0, "restore": 1, "use": 2, "pin": 2, "forget": 3}
    ops.sort(key=lambda o: (epoch(o.get("ts")), int(o.get("seq") or 0),
                            rank.get(o.get("op"), 2), o.get("by", ""), o.get("id", "")))
    return ops, problems


class Fact:
    __slots__ = ("id", "scope", "text", "born", "touched", "uses", "pinned", "valid_until")

    def __init__(self, fid, scope, text, born):
        self.id, self.scope, self.text, self.born = fid, scope, text, born
        self.touched, self.uses, self.valid_until = born, 0, 0.0
        self.pinned = False


def fold() -> tuple[dict[str, Fact], dict[str, list], list[str]]:
    ops, problems = read_all()
    facts: dict[str, Fact] = {}
    history: dict[str, list] = {}
    for op in ops:
        kind = op.get("op")
        fid = op.get("id") or ""
        history.setdefault(fid, []).append(op)
        if kind == "add":
            f = facts.get(fid)
            if f is None:
                facts[fid] = Fact(fid, op.get("scope", "memory"), op.get("text", ""), op.get("ts", ""))
            elif f.valid_until:
                f.text = op.get("text", f.text)
                f.valid_until = 0.0
                f.touched = op.get("ts", f.touched)
        elif kind == "update":
            f = facts.get(fid)
            if f:
                f.text = op.get("text", f.text)
                f.touched = op.get("ts", f.touched)
                f.uses += 1
                if f.valid_until and epoch(op.get("ts")) > f.valid_until:
                    f.valid_until = 0.0      # an update newer than the forget revives it
        elif kind == "restore":
            f = facts.get(fid)
            if f:
                f.valid_until = 0.0
                f.touched = op.get("ts", f.touched)
        elif kind == "forget":
            for victim in [fid] + list(op.get("ids") or []):
                f = facts.get(victim)
                if f and epoch(op.get("ts")) >= epoch(f.born):
                    f.valid_until = epoch(op.get("ts"))
        elif kind == "pin":
            f = facts.get(fid)
            if f:
                f.pinned = True
        elif kind == "use":
            f = facts.get(fid)
            if f:
                f.touched = op.get("ts", f.touched)
                f.uses += 1
    return facts, history, problems


def render(scope: str, facts: dict[str, Fact]) -> tuple[str, list[str], list[Fact]]:
    cand = [f for f in facts.values() if f.scope == scope and not f.valid_until and norm(f.text)]
    live: list[Fact] = []
    seen: set[str] = set()
    for f in sorted(cand, key=lambda x: (x.touched, x.uses), reverse=True):
        key = norm(f.text)
        if key in seen:
            continue
        seen.add(key)
        live.append(f)
    live.sort(key=lambda f: (f.pinned, f.touched, f.uses), reverse=True)
    limit = limits()[scope]
    pinned_cost = sum(len(norm(f.text)) + len(SEP) + 2 for f in live if f.pinned)
    kept, dropped, total = [], [], 0
    for f in live:
        cost = len(norm(f.text)) + len(SEP) + 2
        if f.pinned:
            kept.append(f)                      # reserved first, never squeezed
            total += cost
            continue
        if pinned_cost > limit or total + cost > limit:
            dropped.append(f.text)
            continue
        kept.append(f)
        total += cost
    ordered = sorted(kept, key=lambda f: (not f.pinned, f.born))
    body = ("\n%s\n" % SEP).join(norm(f.text) for f in ordered).rstrip() + "\n"
    return body, dropped, ordered


# --------------------------------------------------- absorb Hermes' edits

def near_score(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a[:12] == b[:12]:
        return 1.0
    if len(a) < 24 or len(b) < 24:
        return 0.0
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.6:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def load_base(scope: str) -> dict[str, str] | None:
    marker = BRAIN / ".state" / ("render-%s.json" % scope)
    try:
        d = json.loads(marker.read_text())
        base = d.get("base")
        if isinstance(base, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in base.items()):
            return base
    except (OSError, ValueError):
        pass
    return None


def absorb(scope: str, facts: dict[str, Fact], base: dict[str, str] | None) -> list[dict]:
    """Diff live against what WE rendered last time (three-way)."""
    path = MEM_DIR / SCOPES[scope]
    if not path.exists():
        return []
    if base is None:
        # First run, or .state lost. Never silently overwrite what is on disk.
        try:
            live_text = path.read_text()
            if live_text.strip():
                bak = BRAIN / "overflow" / ("lost-base-%s-%s.md"
                                            % (scope, datetime.now().strftime("%Y%m%dT%H%M%S")))
                bak.parent.mkdir(parents=True, exist_ok=True)
                bak.write_text(live_text)
        except OSError:
            pass
        return []                       # render only, never absorb

    live = split_entries(path.read_text())
    live_by_hash = {h16(t): t for t in live}
    active = {f.id: f for f in facts.values() if f.scope == scope and not f.valid_until}
    ops: list[dict] = []

    gone = [fid for fid, h in base.items() if h not in live_by_hash and fid in active]
    fresh = [t for h, t in live_by_hash.items()
             if h not in set(base.values()) and h not in {h16(f.text) for f in active.values()}]

    # pair a deletion with a rewrite only when the text really is the same fact
    pairs: list[tuple[float, str, str]] = []
    for fid in gone:
        for t in fresh:
            s = near_score(active[fid].text, t)
            if s >= 0.72:
                pairs.append((s, fid, t))
    pairs.sort(reverse=True)
    used_ids, used_texts = set(), set()
    for score, fid, t in pairs:
        if fid in used_ids or t in used_texts:
            continue
        used_ids.add(fid)
        used_texts.add(t)
        ops.append({"op": "update", "id": fid, "scope": scope, "text": norm(t)})

    for fid in gone:
        if fid in used_ids:
            continue
        if active[fid].pinned:
            continue                    # pinned rows need an explicit forget --force
        ops.append({"op": "forget", "ids": [fid]})
    for t in fresh:
        if t not in used_texts:
            ops.append({"op": "add", "id": fid_for(scope, t), "scope": scope, "text": norm(t)})
    return ops


def with_lock(fh):
    fcntl.flock(fh, fcntl.LOCK_EX)


def stage_owned_paths() -> None:
    """Stage only the data this program writes — never `git add -A`."""
    existing = [p for p in OWNED_PATHS if (BRAIN / p).exists()]
    if existing:
        git("add", "--", *existing)


def sync(args) -> int:
    if not FACT_DIR.exists():
        print("facts: not initialised — run `facts.py migrate` first")
        return 1
    lk = open(BRAIN / ".sync.lock", "w")
    with_lock(lk)
    try:
        if git("pull", "--rebase", "--autostash", "-q") != 0:
            git("rebase", "--abort")
            print("facts: PULL FAILED, nothing written (%s)" % git_out("status", "--short")[:200])
            return 2

        facts, _, problems = fold()
        if problems:
            print("facts: %d bad log line(s) — refusing to render:" % len(problems))
            for p in problems[:5]:
                print("  " + p)
            return 4

        changed = False
        for scope in SCOPES:
            path = MEM_DIR / SCOPES[scope]
            path.parent.mkdir(parents=True, exist_ok=True)
            mlock = open(str(path) + ".lock", "a+")      # Hermes uses the same name
            with_lock(mlock)                             # held across read → absorb → write
            try:
                base = load_base(scope)
                before = len(read_all()[0])
                ops = absorb(scope, facts, base)
                append_ops(ops)
                facts, _, _ = fold()
                body, dropped, kept = render(scope, facts)
                if ops or len(read_all()[0]) != before:
                    changed = True
                if not path.exists() or path.read_text() != body:
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(body)
                    os.replace(tmp, path)
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
                    changed = True
                marker = BRAIN / ".state" / ("render-%s.json" % scope)
                marker.parent.mkdir(parents=True, exist_ok=True)
                mtmp = marker.with_suffix(".tmp")
                mtmp.write_text(json.dumps({"hash": h16(body),
                                            "base": {f.id: h16(f.text) for f in kept},
                                            "at": now()}, ensure_ascii=False))
                os.replace(mtmp, marker)
            finally:
                mlock.close()
            if dropped:
                ov = BRAIN / "overflow" / ("%s-%s.md" % (datetime.now().strftime("%Y-%m-%d"), agent()))
                ov.parent.mkdir(parents=True, exist_ok=True)
                have = set(ov.read_text().splitlines()) if ov.exists() else set()
                with ov.open("a") as fh:
                    for t in dropped:
                        line = "- [%s] %s" % (now(), norm(t))
                        if line not in have:
                            fh.write(line + "\n")
                            have.add(line)

        ahead = git_out("rev-list", "--count", "@{u}..HEAD")
        pushed = True
        if changed or (ahead.isdigit() and int(ahead) > 0):
            stage_owned_paths()
            code = git("commit", "-q", "-m", "facts(%s) %s" % (agent(), now()))
            if code != 0 and "nothing to commit" not in git_out("status", "--short"):
                print("facts: commit failed (%s)" % git_out("status", "--short")[:160])
                return 5
            pushed = False
            for _ in range(3):
                if git("push", "-q") == 0:
                    pushed = True
                    break
                if git("pull", "--rebase", "--autostash", "-q") != 0:
                    git("rebase", "--abort")
                    break
        facts, _, _ = fold()
        act = [f for f in facts.values() if not f.valid_until]
        try:
            idx = build_index()
        except Exception as exc:                # search is a bonus, never a blocker
            idx = "index-failed:%s" % exc
        print("facts: %d active (%d memory / %d user, %d pinned) · %d ops · index=%s · pushed=%s"
              % (len(act), sum(1 for f in act if f.scope == "memory"),
                 sum(1 for f in act if f.scope == "user"),
                 sum(1 for f in act if f.pinned), len(read_all()[0]), idx, pushed))
        return 0 if pushed else 3
    finally:
        lk.close()


def migrate(args) -> int:
    FACT_DIR.mkdir(parents=True, exist_ok=True)
    log = FACT_DIR / ("%s.jsonl" % agent())
    if log.exists() and log.stat().st_size > 0:
        print("facts: already seeded — refusing to duplicate")
        return 0
    ops: list[dict] = []
    me = agent()
    for scope, name in SCOPES.items():
        path = MEM_DIR / name
        if not path.exists():
            continue
        for t in split_entries(path.read_text()):
            t = norm(t)
            fid = fid_for(scope, t)
            ops.append({"op": "add", "id": fid, "scope": scope, "text": t,
                        "ts": now(), "by": me, "seq": len(ops) + 1})
            if scope == "user" and is_pinned(t):
                ops.append({"op": "pin", "id": fid, "ts": now(), "by": me, "seq": len(ops) + 1})
    tmp = log.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in ops))
    os.replace(tmp, log)                       # atomic: no half-seeded log
    (BRAIN / ".state").mkdir(parents=True, exist_ok=True)
    (BRAIN / ".state" / ("seq-%s" % me)).write_text(str(len(ops)))
    for scope in SCOPES:                       # force a fresh base next sync
        (BRAIN / ".state" / ("render-%s.json" % scope)).unlink(missing_ok=True)
    policy = BRAIN / "policy.json"
    if not policy.exists():
        policy.write_text(json.dumps({"limits": dict(DEFAULT_LIMIT),
                                      "note": "render budget = limits * 0.9"}, indent=1))
    print("migrated %d entries into facts/ (agent %s)" % (len(ops), me))
    return 0


def cmd_add(args) -> int:
    text = norm(args.text)
    append_ops([{"op": "add", "id": fid_for(args.scope, text), "scope": args.scope, "text": text}])
    print("added")
    return sync(args)


def cmd_forget(args) -> int:
    facts, _, _ = fold()
    target = args.target.strip()
    active = [f for f in facts.values() if not f.valid_until]
    if target.startswith("f_") and len(target) == 12:
        ids = [target]
    elif len(target) < 6:
        print("refusing: a substring shorter than 6 chars would match too much (use the id)")
        return 1
    else:
        ids = [f.id for f in active if target in norm(f.text)]
    if not ids:
        print("nothing matched")
        return 1
    pinned = [f.id for f in active if f.id in ids and f.pinned]
    if len(ids) > 1 and not args.yes:
        print("%d matches — pass the exact id, or --yes to forget all:" % len(ids))
        for f in active:
            if f.id in ids:
                print("  %s %s" % (f.id, norm(f.text)[:90]))
        return 1
    if pinned and not args.force:
        print("refusing to forget pinned fact(s) %s — pass --force if you really mean it" % pinned)
        return 1
    append_ops([{"op": "forget", "ids": ids}])
    print("forgot %d fact(s); undo with: facts.py restore <id>" % len(ids))
    return sync(args)


def cmd_restore(args) -> int:
    append_ops([{"op": "restore", "id": args.id}])
    print("restored", args.id)
    return sync(args)


def cmd_pin(args) -> int:
    facts, _, _ = fold()
    ids = ([args.target] if args.target.startswith("f_")
           else [f.id for f in facts.values() if not f.valid_until and args.target in norm(f.text)])
    if not ids:
        print("nothing matched")
        return 1
    append_ops([{"op": "pin", "id": i} for i in ids])
    print("pinned %d" % len(ids))
    return sync(args)


def cmd_list(args) -> int:
    facts, _, _ = fold()
    for f in sorted(facts.values(), key=lambda x: (x.pinned, x.touched), reverse=True):
        if not args.all and f.valid_until:
            continue
        if args.scope and f.scope != args.scope:
            continue
        state = "x" if f.valid_until else ("P" if f.pinned else " ")
        print("%s %s [%s] %s" % (state, f.id, f.scope, norm(f.text)[:110]))
    return 0


def cmd_history(args) -> int:
    _, history, _ = fold()
    for op in history.get(args.id, []):
        print(op.get("ts"), op.get("by"), op.get("op"), norm(op.get("text", ""))[:100])
    return 0


def cmd_status(args) -> int:
    facts, _, problems = fold()
    act = [f for f in facts.values() if not f.valid_until]
    out = {
        "agent": agent(),
        "active": len(act),
        "pinned": sum(1 for f in act if f.pinned),
        "voided": sum(1 for f in facts.values() if f.valid_until),
        "bad_lines": len(problems),
        "ahead": git_out("rev-list", "--count", "@{u}..HEAD"),
        "budget": limits(),
        "used": {s: len((MEM_DIR / SCOPES[s]).read_text()) if (MEM_DIR / SCOPES[s]).exists() else 0
                 for s in SCOPES},
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def build_index() -> str:
    """Rebuild the derived search index (never committed, always rebuildable).

    MEMORY.md only carries the hot set that fits the budget; everything else
    (voided facts, budget-dropped ones, history) stays searchable here.
    Chinese needs the trigram tokenizer — FTS5's default unicode61 can't split it.
    """
    db = BRAIN / ".state" / "facts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    facts, _, _ = fold()
    rows = [(f.id, f.scope, "void" if f.valid_until else "active", norm(f.text))
            for f in facts.values() if norm(f.text)]
    con = sqlite3.connect(str(db))
    try:
        con.execute("DROP TABLE IF EXISTS f")
        try:
            con.execute("CREATE VIRTUAL TABLE f USING fts5(id UNINDEXED, scope UNINDEXED,"
                        " state UNINDEXED, text, tokenize='trigram')")
            mode = "trigram"
        except sqlite3.OperationalError:
            con.execute("CREATE VIRTUAL TABLE f USING fts5(id UNINDEXED, scope UNINDEXED,"
                        " state UNINDEXED, text)")
            mode = "unicode61"
        con.executemany("INSERT INTO f VALUES (?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    return mode


def cmd_search(args) -> int:
    db = BRAIN / ".state" / "facts.db"
    if not db.exists():
        print("index missing — run `facts.py sync` first")
        return 1
    q = args.query.strip()
    con = sqlite3.connect(str(db))
    try:
        if len(q) < 3:
            # instr() is reliable for CJK; LIKE silently misses some 2-char
            # sequences even when the codepoints match byte for byte
            sql = "SELECT id, scope, state, text FROM f WHERE instr(text, ?) > 0 LIMIT ?"
            rows = con.execute(sql, (q, args.limit)).fetchall()
        else:
            sql = ("SELECT id, scope, state, text FROM f WHERE f MATCH ?"
                   " ORDER BY bm25(f) LIMIT ?")
            try:
                rows = con.execute(sql, ('"%s"' % q.replace('"', ""), args.limit)).fetchall()
            except sqlite3.OperationalError:
                rows = con.execute("SELECT id, scope, state, text FROM f WHERE instr(text, ?) > 0 LIMIT ?",
                                   (q, args.limit)).fetchall()
    finally:
        con.close()
    if args.all is False:
        rows = [r for r in rows if r[2] == "active"]
    for fid, scope, state, text in rows:
        print("%s %s [%s] %s" % ("x" if state == "void" else " ", fid, scope, text[:160]))
    if not rows:
        print("(no match)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="facts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync").set_defaults(fn=sync)
    sub.add_parser("migrate").set_defaults(fn=migrate)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--all", action="store_true", help="include voided facts")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("add")
    s.add_argument("text")
    s.add_argument("--scope", default="memory", choices=list(SCOPES))
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("forget")
    s.add_argument("target")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_forget)
    s = sub.add_parser("restore")
    s.add_argument("id")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("pin")
    s.add_argument("target")
    s.set_defaults(fn=cmd_pin)
    s = sub.add_parser("list")
    s.add_argument("--all", action="store_true")
    s.add_argument("--scope", default="")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("history")
    s.add_argument("id")
    s.set_defaults(fn=cmd_history)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
