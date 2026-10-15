# Validation record

These are observed results, not a guarantee that arbitrary goals will complete.
The runner was exercised with real inference as well as deterministic fixtures.

## Automated checks

The delivery implementation passed 152 deterministic tests on macOS Python 3.11.
The earlier 117-test snapshot also passed in a Linux container; GitHub Actions
checks the current commit on Linux. These cover tool paths, protected policy, subprocess and
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
not a successful local-model demonstration. Later tests use the user's existing
Qwen installation on RTX through its Ollama API, rather than installing models on
the controller host. Each model still needs task-level evaluation.

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

## Small RTX classifier

The existing `gemma4:e2b` model, run on CPU, correctly classified three live jobs:
ordinary Python implementation and CSV parsing went to Qwen; security architecture
review went to Kimi. All three decisions came from the classifier, not the default.
See [recorded routing evidence](evidence/rtx-small-router.json). These are smoke
checks, not an accuracy benchmark. Ollama classification uses an enum-constrained
JSON schema; malformed or unavailable classification falls back to the user's
explicit default. No model weights were downloaded for this test.

## Failures found during mixed-model application trials

Independent review found an impossible application path in the first todo oracle
and demonstrated that the first Minesweeper oracle accepted HTML comments plus a
printed success marker. These trials are not completion evidence. The
[oracle audit](evidence/generated-oracle-audit.json) records the defects and hashes.
Generated contracts now require independent behavioral review before freezing;
rejections feed later bounded repair attempts. Existing frozen trial records are
preserved rather than silently rewritten.

A separate snapshot of the generated ledger passed 18 of 19 external checks:
persistence, decimal totals and month filtering, CSV contents, malformed data,
and invalid/non-finite amounts. Its automated tests were missing. The snapshot
was an unaccepted intermediate candidate, not a completed delivery. See the
[external check record](evidence/ledger-intermediate-checks.json).

## Clean Linux bootstrap

The no-arguments launcher was exercised with Python outside PATH and no curl or
wget. It installed the downloader and a managed Python runtime automatically,
kept Python executables under `.so-so/tools`, and reused that runtime on the second
invocation. It reached the expected missing-INTENT diagnostic. This proves runtime
setup, not application completion. No model weights were downloaded. See the
[bootstrap record](evidence/linux-bootstrap.json).

A bounded RTX Qwen code-generation probe with thinking disabled produced an
executable decimal parser in 309 output tokens. External checks accepted `12.34`
and rejected NaN, infinities, invalid text and excess decimal places. This is
[function-level evidence](evidence/qwen-action-response.json), not project completion.

The live todo trial also exercised semantic policy repair: the first generated
policy omitted the requested Kimi fallback, the separate reviewer rejected it,
and the next proposal restored the ordered Qwen/Kimi route and was approved.
The [policy review record](evidence/routing-policy-repair.json) preserves both
decisions. This validates policy repair, not task completion.

## Unfinished delivery recovery

Live trials exposed loss of generated files when an epoch exhausted its budget.
Delivery now retains a separate guarded draft before worktree cleanup and resumes
that draft in the next epoch. Regression checks cover worker, evaluator and reviewer
interruptions, quota waits, changed policy/history/ref rejection, and interruptions
on both sides of the acceptance merge. Fresh evaluation and review remain required.
These recovery checks pass; live application trials continue.

## Worker response compatibility

A live Kimi worker returned four requested tools inside an `actions` array. The
runner rejected the wrapper before writing any files. It now accepts only the
first action and explicitly tells the next step that all remaining actions were
not executed. Ambiguous direct-plus-batch or nested batches are rejected.
Replaying that recorded first action through the repaired adapter produced a todo
server that passed 19 of 20 external checks; the deferred test file was absent.
See the [replay record](evidence/todo-protocol-replay.json). This is isolated replay
evidence, not autonomous application completion.
