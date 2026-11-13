# Strategic decisions and routine coordination

An optional `architect` route gives a slower, larger model a small strategic job:
read the intent and return milestones, constraints and acceptance suggestions.
An explicit `coordinator` route then handles tactical planning, contract generation
and subsequent experiments. Workers write code and reviewers inspect results.
Existing pools without an architect route keep their original planner behavior.

The checked-in pool uses the large Xeon model as architect, with at most 2,048
output tokens. Local Qwen 27B handles coordination, planning, reviewing and code.
The worker-only Kimi overflow and its Llama Guard gate are unchanged. Model weight
size (~180 GB) is a storage description, not a verified parameter count.

Strategic guidance is saved under Git common-dir `soso/architecture/`, outside
candidate source worktrees. Its key binds the exact intent and model-routing
policy. It survives optimization epochs and ordinary restarts, so each tactical
planning step does not call the large model again. Changing the intent or model
policy starts a new strategy. The artifact records at most one revision and is
size/schema checked on reuse. The coordinator is checked for artifact changes.
These checks detect accidental corruption; models with unrestricted shell access
are not an operating-system security boundary.

A coordinator may include `"escalation":{"reason":"specific contradiction"}`
with its normal planning JSON. One strategic revision is permitted only when the
provided history includes at least two unsuccessful rounds with valid evaluations.
Malformed model replies, timeouts and worker crashes do not qualify. A valid
request causes one architect revision and a new tactical decision. Later requests
use the existing strategy; they do not create an unlimited escalation loop.
Escalation is considered only when the configured budgets and patience permit
another planning cycle; it does not silently override stopping rules. The supplied
pool uses patience three so two evaluated failures can inform the third plan.

Guidance cannot replace frozen evaluators, relax acceptance requirements, approve
changes, execute commands or assert that milestones are complete. Existing budget,
review and evaluation rules still apply. The architect's response is intentionally
short, but CPU inference can still take substantial time: output limits do not
eliminate prompt processing or internal reasoning. Delivery contract setup uses
the configured time budget, including the initial architect call.

## Where invariants are enforced

One line per invariant from `AGENTS.md`; `scripts/check_guards.py` lists the guard lines and the tests that prove them.

- **Source-checkout isolation:** `engine.py` `Run._run` (clean source, separate result worktree) and `Run.experiment` (fresh detached worktree per candidate, "worker changed Git history"); `workspace.py` `Workspace.path` (no absolute, `..`, any-case `.git` or escaping symlink); `launch.py` `_snapshot` (managed copy of the checkout).
- **Protected evaluation policy:** `workspace.py` `Workspace.is_protected` and `Workspace.tool` (write-time refusal, case-insensitive; nothing written inside a submodule); `engine.py` `fingerprint`, checked around the worker and evaluation in `Run.experiment`; evaluator and check programs (`engine.py` `local_programs`: in-repo paths as written and resolved, and every file name that could supply a `python3 -m` module) are protected in `cli.py` `main`, `delivery.py` `_contract` and `optimize.py` `optimize_project`; frozen goal policy in `delivery.py` `start_goal`.
- **Bounded I/O:** `process.py` `execute` (deadline, output tail, process-group kill); `workspace.py` `Workspace.tool` (read and search caps); `models.py` `Router.ask` (`context_bytes`); `dashboard.py` `Monitor` (tail, size and directory limits).
- **Explicit model routes:** `models.py` `Router._validate_policy`, `Router._select` and `Router.ask`; pool choice in `intent.py` `resolve_pool`.
- **Strict improvement acceptance:** `engine.py` `evaluate` (score parsing), `Run.experiment` (watched staging settings, included config files (`config_bytes`, `replace_file`), `info/attributes`, `info/exclude` and replace refs restored after the worker and before the checkout (`Run.restore_watched`, recorded by `Run.shared` and `included`); the worker's files compared with the commit through a fresh index (`differs_from_commit`); embedded repositories, worker changes to watched settings, worker-set index flags, nested `.git` entries, files in submodule directories and new symlinks under `core.symlinks=false` rejected; then the scored files are a fresh checkout of the commit (`checkout`: index rebuilt from the commit, everything deleted, everything written again), rejected if anything survives the clean, a watched setting moved meanwhile (`Run.unsettled`), a symlink leads out of the worktree (`outside`), protected files changed or the files differ from the commit; a failed restore keeps new runs from starting in `Run.__init__` until `Run.repaired`; runner Git runs without fsmonitor, replace refs or advice and lists untracked files in `process.py` `git`), `Run.accept` (`min_delta` comparison) and `Run.review` (`review_diff`: no external diff or textconv, and attributes from the base commit (`supports_attr_source`); each binary-shown file gets its kinds and sizes under a quoted name (`quoted`) and the text that is evaluated (both sides when both are text, else only a new text side), never a binary side (`listed_changes`, `listed_binaries`, `blob_kinds` with `READERS` reads at a time, `process.py` `blob_head`); text over `process.py` `OutputLimit` or 2 MB of UTF-8 is withheld with its reason; as in `delivery.py` `DeliveryEpoch.review`); `delivery.py` `_bound` (ignored files removed from the goal worktree before scoring), `DeliveryEpoch.run_epoch` (goal worktree checked out again before the baseline, score must not drop, reviewer approval) and `DeliveryEpoch.completion`.
- **Durable run evidence:** `engine.py` `atomic_json`, `Run.checkpoint`, `Run.log` and the resume checks in `Run._run` (`config_sha256`, result HEAD; pinned by `tests/test_resume_digest.py`); the owner-only watched-settings record `watched-config.json` (`dump_watched`, `load_watched`, which reads older records without the newer parts) and `soso/unrestored.json` (`Run.restore_watched`, on every restore failure), with what a restore replaced kept under `replaced/` (`Run.restore_changes`); `models.py` `redact_value`; `delivery.py` `_save` and `DeliveryEpoch.draft_binding`, which `pending_candidate` checks before a retained draft resumes (pinned by `DraftBindingDigestTests` in the same test file).
