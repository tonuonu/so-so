# How to read the validation evidence

These files record development tests of so-so. Start with the
[validation summary](../validation.md) for the test environment, model roles and
results in plain language. The JSON files provide the underlying measurements;
patch files show changes generated during those tests.

The records are historical snapshots, not a dashboard. A status such as
`interrupted`, `plateau` or `round_limit` describes that run at the time it was
recorded. It does not mean an application was completed. Later records can cover
further work on the same example.

## Application trials

| Records | What they demonstrate |
| --- | --- |
| [completed-ledger.json](completed-ledger.json) and [patch](completed-ledger.patch) | Completed expense-ledger CLI, with 19 independent acceptance checks |
| [completed-todos.json](completed-todos.json) and [patch](completed-todos.patch) | Completed persistent todo HTTP service, with 20 independent acceptance checks |
| [minesweeper-progress.json](minesweeper-progress.json) and [patch](minesweeper-progress.patch) | Accepted partial game logic, with nine independent core checks; this snapshot does not establish a finished browser game |
| [ledger-intermediate-checks.json](ledger-intermediate-checks.json) | Checks against an earlier, unaccepted ledger candidate |
| [live-draft-resume.json](live-draft-resume.json) | Todo implementation retained across a pause and resumed for fresh review; completion is recorded separately above |

Files with `live-examples-`, `soso-live-examples-` or `final-examples-` prefixes
are distinct development runs of the Python slugger, JavaScript CSV reader and
C statistics examples under `examples/`. The prefixes distinguish runs; `final`
is a run label, not a guarantee of completion. The `soso-local-slugger-` record
covers a local-model trial. Use each record's scores, review decisions and stop
reason rather than its filename to assess the outcome.

## Runner behavior and focused probes

| Record | Scope |
| --- | --- |
| [local-small-router.json](local-small-router.json) | Three classification jobs handled by a small CPU-based model; not a routing accuracy benchmark |
| [routing-policy-repair.json](routing-policy-repair.json) | Review detects a missing fallback in a generated routing policy, then approves the repair |
| [qwen-action-response.json](qwen-action-response.json) | One generated decimal-parsing function checked externally |
| [qwen-tool-wrapper.json](qwen-tool-wrapper.json) | Parsing a recorded model reply; its requested tool was not executed in this replay |
| [todo-protocol-replay.json](todo-protocol-replay.json) | Replaying a recorded worker response through the adapter; separate from autonomous application completion |
| [generated-oracle-audit.json](generated-oracle-audit.json) | Defects found in generated acceptance evaluators, including checks that could accept invalid implementations |
| [linux-bootstrap.json](linux-bootstrap.json) | Automatic runtime setup in a minimal Linux environment |
| [self-improvement.json](self-improvement.json) and [patch](self-improvement.patch) | Measured simplification of a specific earlier so-so revision; not a claim that the current source has reached an optimum |

## Terms and identifiers

- **Controller:** the so-so process that manages files, commands, model calls and Git.
- **Worker:** a model that requests implementation actions; the controller executes them.
- **Classifier/router:** a small model or decision program that selects a job's route.
- **Evaluator (also called an oracle):** the program that runs acceptance checks and returns a score.
- **Epoch:** a bounded work attempt within a persistent goal. A later epoch can resume saved work.
- **Accepted increment:** a reviewed change retained as progress, possibly short of completing the goal.
- **Replay:** a recorded response submitted to an adapter or parser to test a specific behavior.

In mixed-model records, `qwen` means the GPU-backed local implementation model,
`triage` means the CPU-based Gemma classifier, and `kimi` means the authenticated
Kimi command-line adapter. Exact models and hardware are described in the
[environment section](../validation.md#test-environment-and-model-roles).
These choices describe the tests; users can configure different supported models.

Commit hashes in application records belong to generated project repositories.
They are not necessarily commits in this repository. Source SHA-256 values bind
checks to the recorded files. Patches preserve generated changes, including flaws
in rejected or partial candidates. Reviewer `reason` fields and worker summaries
are recorded model output, not independently verified assertions; compare them
with the explicit check results and stated limitations.

Loopback URLs in recorded output refer to local test connections. They are not
public services or required user settings. Trial names such as `battle-todos`
identify a test run, not a computer or a deployment environment.
