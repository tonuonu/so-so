# Validation record

These are observed results, not a guarantee that arbitrary goals will complete.
The runner was exercised with real inference as well as deterministic fixtures.

## Automated checks

The delivery implementation passed 98 deterministic tests on macOS Python 3.11
(the earlier 69-test snapshot also passed in a Linux container). These cover tool paths, protected policy, subprocess and
HTTP deadlines, routing, Git isolation, evaluation binding, restart recovery,
quota waiting, intermediate acceptance, and independent completion review.
Further entry-point regression checks are recorded in the final CI run.

GitHub Actions tests Python 3.11, 3.12 and 3.13, installs the package, exercises
`doctor`, and runs all three example evaluators. Check the commit's Actions result
for the exact release status. The package has no Python runtime dependencies.

## Live projects

The checked-in [evidence](evidence/) contains sanitized trial summaries and accepted
patches. Raw prompts/tool logs remain local because they can contain source and host
paths. Scores below refer to the fixed visible example evaluators.

| Project | Language | Initial extended trial | Outcome |
| --- | --- | --- | --- |
| Unicode slugger | Python | 3/12 → 12/12 | Reviewed improvement; subsequent plateau |
| Quoted CSV reader | JavaScript | 4/12 → 12/12 | Reviewed improvement; subsequent plateau |
| Numeric statistics CLI | C | 2/12 → 2/12 accepted | 12/12 candidates rejected for numerical edge cases |

The C failures are retained deliberately. They demonstrated that visible tests
alone were insufficient, and that review feedback needed to reach subsequent
planning. The runner now passes that feedback forward and can try another eligible
candidate when the highest-scoring candidate is rejected.

## Local inference

An installed Ollama `granite4:latest` model returned valid JSON in a live probe.
A full local-worker trial with Codex planning/review made no accepted improvement:
small-model tool protocol failures exhausted the attempt. This is a real limitation,
not a successful local-model demonstration. The user's Qwen model was not installed
on this host and has not been validated. Ordered fallback routes and deterministic
routing tests exist, but each model still needs task-level evaluation.

## Self-improvement

On baseline `7174cd1c9006fbc5eeb92c6d781ef0bea871a308`, so-so proposed and reviewed
its own simplifications. The AST-node metric improved **7902 → 7850**, with the
then-current 42 tests passing. Two accepted patches simplified HTTP request-body
construction and candidate selection. Both changes were incorporated into the
subsequent delivery implementation.

The trial used 53 model calls and ended at its three-round limit, **not** a proven
plateau. AST size is only a proxy for bloat. New delivery functionality subsequently
increased total source size; the figures are for that specific baseline and trial.
See [self-improvement evidence](evidence/self-improvement.json) and
[the generated patch](evidence/self-improvement.patch).

## Boundaries

Model-generated acceptance contracts can miss requirements. Completion also needs
an independent reviewer, but this still does not establish human-level correctness.
No iPhone/App Store/AWS production deployment was performed in these trials.
Commands execute with host permissions; worktrees are isolation for source changes,
not an operating-system sandbox. Cloud operations need idempotent project-level
logic because Git rollback cannot undo external effects.

## Single-command and RTX validation in progress

The no-argument executable now has a real subprocess regression test: only
INTENT.md is supplied, provider responses are deterministic fixtures, and the
launcher performs real snapshot/Git setup. Model-policy Markdown translation and
per-model cooldown races have dedicated regression coverage.

The exact Qwen3.8-27B Q4_K_M model answered a live JSON probe on the existing RTX
Ollama installation. The configured Kimi CLI answered a separate live probe.
A live expense-ledger assignment is now testing the ordinary no-flags executable
with that Qwen worker, a smaller installed classifier and Kimi planning/review.
These probes alone do not prove mixed-model task completion.
