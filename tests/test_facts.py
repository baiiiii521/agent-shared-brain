#!/usr/bin/env python3
"""Tests for the fact store and the chain anchors.

No dependencies: `python3 tests/test_facts.py`. Every test builds its own
throwaway HERMES_HOME in a temp dir, so nothing here can touch a real memory
directory. The point of this file is that the dangerous paths — deletion,
rewrite, budget pressure, tampering — are checked by a machine instead of by
somebody noticing at 2am.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str, home: Path):
    """Import sync/<name>.py against a throwaway HERMES_HOME."""
    os.environ["HERMES_HOME"] = str(home)
    for mod in [m for m in sys.modules if m in ("facts", "chain")]:
        del sys.modules[mod]
    spec = importlib.util.spec_from_file_location(name, ROOT / "sync" / ("%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Env:
    def __init__(self, agent="alpha", memory=("fact one about dogs", "fact two about cats"),
                 user=("Name: Test",)):
        self.home = Path(tempfile.mkdtemp(prefix="brain-test-"))
        self.mem = self.home / "memories"
        self.mem.mkdir(parents=True)
        self.brain = self.home / "brain"
        for d in ("facts", ".state", "mailbox", "journal", "pending"):
            (self.brain / d).mkdir(parents=True, exist_ok=True)
        (self.mem / "MEMORY.md").write_text("\n§\n".join(memory) + "\n")
        (self.mem / "USER.md").write_text("\n§\n".join(user) + "\n")
        (self.brain / ".agent-id").write_text(agent)
        (self.brain / "policy.json").write_text(
            json.dumps({"limits": {"memory": 6000, "user": 3000}}))
        self.facts = load("facts", self.home)
        self.chain = load("chain", self.home)

    def seed(self):
        self.facts.migrate(None)
        return self.facts

    def render(self, scope="memory", publish=True):
        f, _, problems = self.facts.fold()
        assert not problems, problems
        body, dropped, kept = self.facts.render(scope, f)
        if publish:                     # same steps sync() takes, so absorb has a base
            name = self.facts.SCOPES[scope]
            (self.mem / name).write_text(body)
            marker = self.brain / ".state" / ("render-%s.json" % scope)
            marker.write_text(json.dumps({"hash": self.facts.h16(body),
                                          "base": {x.id: self.facts.h16(x.text) for x in kept},
                                          "at": "test"}))
        return [x for x in body.split("\n§\n") if x.strip()], dropped, kept

    def live(self, text):
        (self.mem / "MEMORY.md").write_text(text)

    def cleanup(self):
        shutil.rmtree(self.home, ignore_errors=True)


# ---------------------------------------------------------------- tests

def test_migrate_seeds_every_entry():
    e = Env()
    e.seed()
    body, _, _ = e.render()
    assert len(body) == 2, body
    assert "dogs" in body[0]
    e.cleanup()


def test_deletion_is_absorbed_in_one_pass():
    """A memory the human deletes must disappear, not come back."""
    e = Env()
    e.seed()
    e.render()
    base = {f.id: f for f in e.facts.fold()[0].values()}
    keep = [t for t in base.values() if "cats" in t.text]
    e.live("\n§\n".join(t.text for t in keep) + "\n")
    facts, _ = e.facts.fold()[0], None
    ops = e.facts.absorb("memory", e.facts.fold()[0], e.facts.load_base("memory"))
    e.facts.append_ops(ops)
    body, _, _ = e.render()
    assert len(body) == 1 and "cats" in body[0], body
    e.cleanup()


def test_rewrite_becomes_update_not_duplicate():
    e = Env()
    e.seed()
    e.render()
    rewritten = "fact one about dogs and also birds"
    e.live("\n§\n".join([rewritten] + [t.text for t in e.facts.fold()[0].values()
                                       if "cats" in t.text]) + "\n")
    ops = e.facts.absorb("memory", e.facts.fold()[0], e.facts.load_base("memory"))
    e.facts.append_ops(ops)
    body, _, _ = e.render()
    assert len(body) == 2, body
    assert any("birds" in b for b in body)
    assert not any(b.startswith("fact one about dogs") and "birds" not in b for b in body)
    e.cleanup()


def test_budget_drop_never_forgets():
    """Dropping for budget must not be mistaken for a deletion on the next pass."""
    e = Env(memory=tuple("filler %02d %s" % (i, "x" * 40) for i in range(6)))
    e.seed()
    e.facts.limits = lambda: {"memory": 100, "user": 3000}
    body, dropped, _ = e.render()
    assert dropped, "budget should have dropped something"
    e.live("\n§\n".join(body) + "\n")
    ops = e.facts.absorb("memory", e.facts.fold()[0], e.facts.load_base("memory"))
    forgets = [o for o in ops if o["op"] == "forget"]
    assert not forgets, "budget drops leaked into forgets: %s" % forgets
    e.cleanup()


def test_pinned_survives_budget_and_deletion():
    e = Env(user=("Name: Test", "Timezone: UTC", "hobby: chess"))
    e.seed()
    e.facts.append_ops([{"op": "pin", "id": e.facts.fid_for("user", "Name: Test")}])
    e.facts.limits = lambda: {"memory": 6000, "user": 1}
    body, dropped, _ = e.render("user")
    assert len(body) == 1 and "Name" in body[0], body
    e.cleanup()


def test_forged_origin_is_rejected():
    e = Env()
    e.seed()
    p = e.brain / "facts" / "alpha.jsonl"
    with p.open("a") as fh:
        fh.write(json.dumps({"op": "add", "id": "f_deadbeef01", "scope": "memory",
                             "text": "forged", "ts": "2026-01-01T00:00:00+00:00",
                             "by": "beta", "seq": 99}) + "\n")
    _, _, problems = e.facts.fold()
    assert problems and "forged" in problems[0], problems
    e.cleanup()


def test_chain_detects_rewrite_and_truncation():
    e = Env()
    e.seed()
    e.chain.update()
    assert not e.chain.verify(verbose=False)
    p = e.brain / "facts" / "alpha.jsonl"
    original = p.read_text()
    p.write_text(original.replace("dogs", "dogz", 1))
    assert e.chain.verify(verbose=False), "rewrite not detected"
    p.write_text(original)
    assert not e.chain.verify(verbose=False)
    p.write_text("\n".join(original.splitlines()[:-1]) + "\n")
    assert e.chain.verify(verbose=False), "truncation not detected"
    p.write_text(original)
    e.chain.update()
    e.cleanup()


def test_lost_base_backs_up_instead_of_overwriting():
    e = Env()
    e.seed()
    e.render()
    (e.brain / ".state" / "render-memory.json").unlink()
    e.live("hand written line that must not vanish")
    ops = e.facts.absorb("memory", e.facts.fold()[0], e.facts.load_base("memory"))
    assert ops == [], "must not absorb without a base"
    backups = list((e.brain / "overflow").glob("lost-base-*.md"))
    assert backups, "no backup written"
    assert "hand written" in backups[0].read_text()
    e.cleanup()


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok   %s" % t.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (t.__name__, exc))
        except Exception as exc:                     # noqa: BLE001
            failed += 1
            print("ERR  %s: %r" % (t.__name__, exc))
    print("\n%d passed, %d failed" % (len(tests) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
