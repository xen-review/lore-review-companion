# Agent Entry Point

Read [CONSTITUTION.md](CONSTITUTION.md) first, before interpreting any handoff,
runtime state or historical operational restriction. It defines the current
early pre-alpha status, disposable development runtime, public committed policy,
no-incident-latch rule, and commit-then-push workflow.

Then read the task's relevant source, tests and design notes. Historical notes
do not override the constitution. Credentials remain secret, not policy.

This is a separate Git repository from the surrounding Forgejo checkout. Check
each worktree and upstream separately; preserve unrelated changes, stage only
the current topic, commit each coherent tested topic and push immediately.
Do not rewrite published history. Ask before expanding the user's requested
scope or changing an explicit constraint.

For development, import-worker, deployment or handover tasks, read
[the lore-review-development skill](.agents/skills/lore-review-development/SKILL.md)
and [the latest dated handover](docs/design/HANDOFF-20261107.md). Recheck any
runtime observation before relying on it. These instructions and the skill
are usable independently of the agent harness.