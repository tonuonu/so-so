# so-so

**Software on software.** Give a small language model tools, an intent, and a measurable feedback loop.

so-so is a Linux-first Python runner that reads `INTENT.md`, proposes experiments, works in parallel Git worktrees, and keeps changes only when they pass fixed checks and improve a score. It can use local Ollama or OpenAI-compatible servers, Codex CLI, and command-line model adapters. No Python runtime dependencies. Python 3.11+ and Git are required.

The repository is initially private and MIT licensed. Version 0.1 is an experimental foundation with reproducible tests and live examples, not a claim that autonomous programming is solved.

## Quick start

```sh
git clone git@github.com:tonuonu/so-so.git
cd so-so
python3 -m venv .venv
. .venv/bin/activate
pip install .
soso doctor --config examples/slugger/soso.json

# Copies three deliberately imperfect projects into fresh repositories.
# Uses your authenticated Codex CLI. This makes real model calls.
python scripts/run_examples.py --backend codex --output /tmp/soso-examples
```

Use a local model instead:

```sh
ollama list
python scripts/run_examples.py --backend ollama --model YOUR_INSTALLED_MODEL \
  --output /tmp/soso-local-examples
```

Do not literally use `YOUR_INSTALLED_MODEL`; copy the exact name your server exposes. Model size, context window, and quality determine how well a local-only run performs. A failed or flat experiment is a recorded outcome, not proof of success.

## Your own project

Start with a committed Git repository. Add `INTENT.md`, an evaluator, and a config based on an example. Commit those files too. Configure Git author identity before running.

```sh
soso probe --config soso.json
soso run --repo /path/to/project
```

The runner leaves the original checkout and branch unchanged. It prints a result branch and report location. Inspect and merge the result when ready:

```sh
git diff main...soso/RUN_ID
git merge --ff-only soso/RUN_ID
```

Use the actual branch printed by the run. No push or merge into your own branch happens automatically. Internal run records live under the repository's common Git directory in `soso/RUN_ID/`. They include JSONL events, scores, candidate commits, review results and checkpoints. Treat logs as private source material.

`--resume RUN_ID` continues a checkpoint with unchanged configuration, intent and execution permission, within its original budgets. It does not extend a stopped run's round or patience limits. For a new intent or budget, start a new run from the accepted result. Interrupted in-flight candidates may be retried; completed checkpointed rounds are retained.

## The loop

1. Run the fixed baseline evaluator. Invalid baselines stop the run.
2. A planner proposes small, independent alternative experiments against the current best commit.
3. Workers run concurrently, each in an isolated worktree. Model concurrency is limited separately per endpoint.
4. Workers request structured actions: list, read, literal search, write, replace, delete, diff, check, skill, or an explicitly enabled command.
5. The runner rejects protected-file changes, runs fixed checks, and measures the candidate. An optional separate review route can reject a passing candidate.
6. Promote the best approved strict improvement. Feed scores and review feedback into the next round. Stop on a plateau, round limit, or budget exhaustion.

Parallel workers explore alternatives; they do not merge unrelated changes concurrently. A larger goal advances through successive, independently measurable increments. This intentionally avoids conflict resolution between agents. If a feature needs several changes before it can score, make that a single bounded experiment or define useful intermediate milestones in the evaluator.

## Small context and routing

Each worker call gets a fresh packet: the task, root instructions, a short durable note, and the last tool result. `read` pages source and supplies applicable ancestor `AGENTS.md` files. A worker must encounter directory instructions before writing there. There is no ever-growing chat transcript or mandatory vector database.

Model `context_bytes` is a conservative **input byte budget**, not an exact tokenizer count. Size it below the model's usable window and reserve output space. Oversized packets fail or route to a larger model; instructions are not silently dropped. API `max_tokens` bounds generated output. Agent CLIs may add their own context and perform internal work outside this packet accounting.

Routes are ordered lists, for example:

```json
"routes": {
  "planner": ["strong"],
  "worker": ["local", "strong"],
  "reviewer": ["strong"]
}
```

Transport/JSON failures fall through to the next model. Consecutive tool failures escalate worker requests. This is transparent policy, not a learned router. Different models, executables and endpoints can be configured independently. See [configuration](docs/configuration.md).

## Measured improvement and self-improvement

An evaluator is a trusted command whose final stdout line is JSON containing a finite numeric `score`. It must exit zero. Checks are trusted commands that must all exit zero. The policy is fixed for a run; workers cannot choose a different evaluator or change protected files.

```json
{"score": 11.0}
```

Maximize correctness/accuracy, minimize runtime or code complexity, or use a domain-specific quality measure. For noisy measurements, use `repeats` and a meaningful `min_delta`; the runner compares median scores. A plateau means the attempted experiments found no qualifying improvement, **not** that no better solution exists.

This repository includes its own `INTENT.md`, checks and AST-size evaluator. You can run `soso run` here to propose simplifications of the runner itself. The running controller stays fixed; a changed controller takes effect only in a subsequent invocation after you inspect/install the result. Tests and evaluator policy remain protected.

A metric is only as good as its coverage. Source hashes prevent casual evaluator edits, not clever metric gaming. Reducing AST nodes is a limited proxy for bloat, never sufficient justification to remove functionality. Review and stronger hidden checks remain valuable.

## Superpowers

Selected [obra/superpowers](https://github.com/obra/superpowers) skills are pinned and bundled with their MIT license. Planning, TDD, debugging, code review and verification are available through bounded skill reads; small role-specific excerpts are supplied automatically. This is an explicit adaptation for an autonomous runner, not an implementation of every upstream workflow. See [the integration and provenance](docs/superpowers.md).

## Execution and resources

File tools constrain paths and protected files. Fixed evaluator/check commands run because you configured them. Arbitrary model-requested subprocesses require `--allow-exec`. Command-model adapters and Codex are also executables you explicitly configure.

**Git worktrees are not an operating-system sandbox.** Commands, evaluators, imported project code and external CLIs run with their process permissions. For untrusted repositories or unattended command access, run in a disposable container/VM with only the project mounted, limited credentials, and restricted network access. Do not mount your home directory or Docker socket. See [operations](docs/operations.md).

The scheduler caps experiment concurrency by available CPUs (including Linux affinity/cgroup quota) and endpoint semaphores. `doctor` reports NVIDIA GPU inventory when available. GPU placement, VRAM limits, tensor parallelism and model loading belong to the inference server; so-so does not pretend to manage them. Configure separate server endpoints for different GPUs and set `parallel` to a capacity each can sustain.

## Verification

```sh
python -m unittest discover -s tests -v
python scripts/check_examples.py
```

Deterministic tests exercise orchestration without paid inference. The live example script is separate and records real model outcomes. [Validation evidence](docs/validation.md) distinguishes these from one another.

MIT license. Vendored skill content retains its own copyright notice under the same license.
