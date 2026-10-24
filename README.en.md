# agent-shared-brain

> Two AI agents, one brain. Share memory, messages and hand-offs over git — with tamper detection.

[中文](README.md) | **English**

## The problem

Run the same agent on two machines (a laptop and an always-on box) and three
things go wrong: the laptop sleeps and the other side has no idea what you were
doing; two copies of memory drift apart and an edited fact comes back as a
second contradicting version; and merging two writers into one file either
conflicts or silently overwrites.

This project takes the boring-but-sound route: **each agent only appends to its
own log, never touching the other's file**, and a deterministic fold derives the
memory that the agent actually reads.

- No merge conflicts — structurally, not by retrying
- Edit is an explicit `update` (no stale copy coming back)
- Delete is `forget` (invalidated, still auditable)
- History rewriting is detectable (chained anchors)

Pure Python standard library + git. No dependencies.

## Quick start

```bash
git clone git@github.com:you/brain.git ~/.hermes/brain
printf 'alpha\n' > ~/.hermes/brain/.agent-id     # this machine; the other one is beta
python3 ~/.hermes/brain/sync/facts.py migrate
sh ~/.hermes/brain/sync/brain_sync.sh            # run every 10 minutes
```

The second machine only sets `.agent-id=beta` and runs the same script — it
renders an identical memory file.

## How it works

```
   alpha                              beta
   facts/alpha.jsonl   ──┐      ┌──   facts/beta.jsonl
   (append only)         │      │     (append only)
                         ▼      ▼
                       git repo
                          │
                 deterministic fold
                          │
        MEMORY.md / USER.md + mailbox/ journal/ pending/
```

Four rules carry the whole thing:

| Rule | Why |
|---|---|
| One log per agent | Two machines never write the same file → no conflict exists |
| Ops are add/update/forget/restore/pin/use | Editing and deleting are explicit, not guessed |
| Render is a three-way merge (base / live / facts) | A machine that is merely behind cannot "absorb" its stale copy and undo your edit |
| `chain.py` anchors each log | Rewritten or truncated history is caught immediately — no paid GitHub plan needed |

Details and the bugs that shaped them: [docs/design.md](docs/design.md).

## Commands

```bash
facts.py sync | list | add | forget | restore | pin | history | search
mailbox.py send | inbox | ack | log | pending
chain.py verify | update | accept | status
sh sync/brain_sync.sh      # one full pass, use this from cron/launchd
```

## Wiring it to an agent

`skills/shared-brain/SKILL.md` is a drop-in instruction sheet for the agent
itself. For Hermes Agent, copy it to `~/.hermes/skills/`; for anything else,
paste it into the system prompt.

## Limits

- Heuristic pairing between "rewrite" and "delete + add" (0.72 similarity)
- Budget eviction sorts by last-modified, not last-used
- Eventually consistent (up to 10 minutes), not real-time

## License

MIT.
