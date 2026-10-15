# Superpowers integration

Upstream: https://github.com/obra/superpowers

Pinned revision: `bb92a77741419a4ab5f06e711a283343f1ada0c3`.

Selected original Markdown files and the upstream MIT license live in
`soso/skillpacks/superpowers/`. `manifest.json` records SHA-256 hashes; skill reads
verify the requested SKILL.md against that manifest. Package data is included in
wheel installations. Updating the pack is an explicit repository change, never
a network fetch during a run.

| Runner stage | Skill | Adaptation |
|---|---|---|
| Planner | writing-plans | Propose bounded, independently testable experiments with concrete files and checks. |
| Worker | test-driven-development | Use fixed checks to reproduce defects, make minimal changes, add regression tests where allowed. |
| Worker failure | systematic-debugging | Read on demand with the `skill` tool; investigate causes before speculative edits. |
| Review | requesting-code-review | Fresh model call receives intent, diff and measured results. |
| Acceptance | verification-before-completion | Runner executes checks and evaluates source; prose completion does not authorize acceptance. |

The worker protocol supports paged skill reads:

```json
{"tool":"skill","name":"systematic-debugging","start":1,"lines":60}
```

A small opening excerpt is included automatically for each role. The full skills
remain available on demand. The runner does not claim every model will follow
every procedural instruction; the mechanical acceptance checks are separate.

## Intentional differences from upstream

This is an adapted workflow, not the full Superpowers plugin. An explicit
`INTENT.md` and trusted configuration supply the already-authorized scope. The
runner owns worktrees, parallel dispatch, commits and promotion; models must not
spawn another unbounded agent tree. It supplies JSON plans instead of interactive
planning handoffs and keeps evidence in its journal instead of asking a model to
assert completion. Ambiguous requirements remain a limitation to report, not
permission to expand scope.

Existing protected tests cannot be rewritten, even if a skill discusses changing
tests. Add focused tests outside protected paths and configure their execution
before the run when they should be mandatory gates. The fixed evaluator is never
replaced by a generated test or self-review. Full interactive brainstorming,
subagent tooling and branch-finishing workflows remain available in upstream
Superpowers for humans using supported agent harnesses.

Only the upstream license notice is required attribution. No model/tool credits
are inserted into generated project files or commit messages.
