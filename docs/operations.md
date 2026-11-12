# Operations and limits

This is a developer and deployment reference. For ordinary use, write `INTENT.md`
and run `./so-so`; the container commands below are optional deployment examples.

## Linux container

Build the supplied image (contains Python, Git, C compiler and Node for the examples):

```sh
docker build -t soso .
```

For model servers exposed on your host, choose a reachable endpoint for your
container network. On Linux, a trusted local deployment can use host networking;
that is a connectivity choice, not network isolation. For untrusted source,
prefer a separate VM or a container network with only the model endpoint exposed.

Example with a disposable repository copy whose files are owned by your UID:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 4g --cpus 2 \
  -v /path/to/disposable-repo:/workspace \
  -e GIT_AUTHOR_NAME='Your Name' -e GIT_AUTHOR_EMAIL='you@example.com' \
  -e GIT_COMMITTER_NAME='Your Name' -e GIT_COMMITTER_EMAIL='you@example.com' \
  soso run --repo /workspace --allow-exec
```

Configure the endpoint before committing the source. GPU compute usually lives in
a separately managed Ollama/vLLM server. Container memory above covers the runner
and project commands, not an external inference server.

This example does not mount CLI authentication. Use API/local-server adapters in
a container, or deliberately provision narrowly scoped credentials. Never mount
a Docker socket or your whole home. In-process source tools are path checked;
commands and model CLIs still have their OS permissions. A configured executable
can read environment variables, use the network or alter shared Git metadata.

## Failure handling

- Invalid model JSON: try the next configured route; if none succeeds, record a failed candidate.
- Tool error: return a bounded error and escalate to a later route on the next step.
- Failed check or invalid/nonfinite score: reject the candidate.
- Changed protected source: reject the candidate.
- Rejected review: try the next qualifying candidate, and carry the feedback into planning.
- No strict improvement: increment plateau count.
- Call/time/step limits: bounded stopping; step-limited partial work may still be evaluated against the same gates.
- Interrupt: save the checkpoint when possible. `SIGKILL`, power loss and filesystem failure can leave an unfinished worktree. No program can promise a final checkpoint after an unconditional kill.

Subprocess groups are killed at timeout/exit. Deliberately detached descendants
can escape process-group cleanup; pipe handling still returns within its budget.
Use a container/cgroup for a stronger lifetime boundary. SIGINT during parallel
work may wait for already-running workers to reach their bounded timeouts.

## Recovery and storage

An optimization run has a persisted absolute wall deadline that includes downtime.
Resuming that run does not replenish time or already-journaled model calls.
Persistent delivery instead opens bounded new epochs while preserving its goal.

Only one run owns a repository at a time (POSIX file lock in the common Git directory).
Source must be clean. Worktrees and configuration are pinned to committed source;
ignored caches are not copied into candidate worktrees. Submodule and large-data
setup are the caller's responsibility.

A candidate is scored on exactly the commit that would be accepted. After the
commit, every file in the candidate worktree is deleted (`git clean` against an
empty index, which also removes ignored files and empty directories) and the
commit is checked out again from an index rebuilt from it. Filters, `ident`,
`eol`, `working-tree-encoding`, file modes and symlinks therefore apply as on a
fresh clone with your configuration, paths outside a sparse checkout stay absent
as in the baseline, and nothing the worker left on disk is scored. This costs
about one checkout per candidate: on a synthetic 35,000-file repository on a
local SSD it took 12-13 s, next to 6-15 s for `git worktree add` and 2-10 s
for the comparison below. A candidate is rejected when anything survives the
clean (for example a FIFO or a read-only folder), when a watched setting changes
while the files are checked out, when the commit's attributes change how a
protected file is written, when the checked-out files do not match the commit
(for example paths that collide on a case-insensitive file system), when it
adds or changes a symlink while `core.symlinks` is false (the checkout would
write it as a plain file), and when it adds or changes a symlink whose target is
absolute or resolves outside the worktree. In persistent delivery the goal
worktree is checked out again the same way before each epoch's baseline, because
`merge --ff-only` rewrites only the files that changed; when an interrupt cuts that
checkout short, resuming the goal finishes it.

With command execution, settings that change how content is staged or checked
out (`filter.*`, `core.attributesFile`, `core.autocrlf`, `core.eol`,
`core.safecrlf`, `core.fsmonitor`, `core.worktree`, `core.sparseCheckout*`,
`core.symlinks`, `core.fileMode`, `sparse.*`, `attr.*`, `index.*`, `diff.*`
(which shapes the reviewer's diff), and the `include.*`, `includeIf.*` and
`extensions.worktreeConfig` settings that can bring such settings in), read with
includes expanded from the repository config and each worktree's
`config.worktree`, `info/attributes`, `info/exclude`, the `refs/replace/` refs,
and the bytes of every file the repository config names with `include.path` or
`includeIf.*.path` (followed transitively, whatever the condition, at most 64
files of 1 MiB, a symlinked one at its target) are recorded when a run's first
candidate worktree is created. `info/` files are also recorded at their target
when they are symlinks. A device such as `/dev/null`, an include target too large
or too numerous to record, and one Git cannot parse (for example behind an
`includeIf` that never holds) are not recorded and never stop a run; a watched
change they bring in cannot be restored and fails closed, as below. The record is
kept, owner-readable only, in the run folder for resume.

After each worker they are restored, under the run lock that also covers staging
and the commit, and again before the checkout. Each restore is logged as
`repository_config_restored` with the key, file or ref names, and the old and
new values are kept in the candidate's record. An included file gets its
recorded bytes back, through a new file renamed into place with the old file's
mode, only when the watched values that included files supply changed; the
evidence lists those values, never the file's other content. Every recorded
include whose bytes differ is then put back, including unrelated edits in it,
while an include edit that changes no watched value is kept. A FIFO, symlink,
folder or other non-file put in place of a recorded include is replaced at once.
Whatever a restore replaces is first kept, owner-only, under `replaced/` in the
run folder (a folder is moved there whole, a file's first MiB is copied), and the candidate's record names it as
`kept`, so your unrelated edits can be recovered. A value that comes from an
included file is never overridden in `.git/config`. Replace refs the worker added or changed are
removed or put back. This includes watched changes you make yourself during a
run with command execution enabled; without it a worker cannot change config,
so nothing is restored. Other config, such as remotes and branch tracking, is
never touched. When a difference cannot be restored (for example an
`includeIf "hasconfig:remote.*.url:..."` that a new remote turned on), or any
error stops a restore, the candidate ends with an error naming the file, and
`soso/unrestored.json` in the
common Git directory makes every new run, including the next delivery epoch,
refuse to start until the settings again match that run's record. They are
compared in a temporary detached worktree, as candidates read them, so
`includeIf "onbranch:..."` and `"gitdir:..."` conditions hold as they did when
recorded; a run killed during that check can leave a `soso/probe-*` worktree,
which `git worktree remove --force` removes. Delete that file to keep the current
settings instead.

Before the checkout, the worker's files are also compared with the commit through
a fresh index, so a stat cache left by a worker's own `git add` cannot hide a
difference. A candidate is rejected when it changed its own worktree-scope
settings, changed shared ones while it was the only candidate running, was
committed while a watched setting changed, has files that differ from its
commit, adds or changes a gitlink (nested repository or submodule), sets
skip-worktree or assume-unchanged index flags its fresh worktree did not have,
leaves files inside a submodule directory, or creates an entry named `.git`
below the worktree root. Worker tools cannot write inside a submodule directory.
Runner-managed Git commands run without hooks or an fsmonitor hook
(`core.hooksPath=/dev/null`, `core.fsmonitor=false`), list untracked files
whatever `status.showUntrackedFiles` says (so a repository with untracked files
is not clean enough to start a run, even if you set it to `no`), and run without replace refs
(`GIT_NO_REPLACE_OBJECTS=1`) and without advice messages (`GIT_ADVICE=0`, Git
2.46 or newer). The reviewer's diff is built with `--no-ext-diff --no-textconv`
and with attributes from an empty tree (`--attr-source`, where a probe shows Git accepts it), so
no `-diff`, `binary` or diff driver in the repository's `.gitattributes` hides a
change. Each file Git still shows only as binary gets a line with both sides' kinds
and sizes (for example `pointer.dat: text 44 B → binary 3.1 MB`), read by blob id,
never through the file name, and sized with one `cat-file --batch-check`; a
gitlink side counts as absent. A side is binary as Git judges it: a NUL byte in
its first 8000 bytes, of which at most 8000 bytes are read, within 60 seconds,
and a failed or stalled read is withheld. The change is also shown as text when a
side is text and any binary side is at most 32 KiB: a `-diff` from an older Git or
your own `info/attributes` or `core.attributesFile`, a renamed file, or a NUL byte
added or removed cannot hide a text change, and a change that adds NUL bytes is
flagged, since later edits to that file will show only as binary. Larger binaries
are never shown as text and never refused, whatever their size. A file binary on
both sides still shows only "Binary files differ", as before, including a file an
earlier accepted change made binary with a NUL byte. Text the reviewer would
receive beyond the 2 MB the runner reads is never shown cut short: the candidate is
not sent to the reviewer and not approved, with the reason "the diff exceeds 2 MB;
split the change into smaller ones"; a side that cannot be read, or binary-shown
files that cannot all be read within the review's time (at most 300 seconds, less
when the run's budget is nearly spent), are withheld the same way, each with its
own reason. In persistent delivery such a reason goes back to the worker as repair
feedback. Do not rely on hooks as your evaluator, and
expect no fsmonitor speed-up in them.

Not covered: global, system and XDG Git configuration, the attributes file that
`core.attributesFile` (or the XDG default) names, and `GIT_CONFIG_*` variables
are neither watched nor restored, so a worker with command execution
that edits your global config (for example a smudge filter) can still change
what is checked out, here and in your other repositories. Likewise, a process a
worker leaves running detached from its process group can still write into the
candidate worktree between the checkout and the evaluation. Use a container or
VM to bound both. Two parallel workers can still race: a shared setting one
changes and changes back while another candidate is checked out is not seen;
run with `parallel` 1 when that matters.

A run retains its result branch/worktree and candidate commit refs. Successful
candidate commits are kept under `refs/soso/RUN_ID/`, including measured rejected
candidates. In persistent delivery, budget/quota exhaustion and graceful interruption
save valid unfinished changes as a separate draft ref before cleanup. The next
epoch resumes the draft against its original accepted base and frozen policy.
Evaluation and independent review still precede acceptance; a saved draft is not
a completed or accepted result. When review explicitly marks a nonregressing
candidate useful but rejects it with concrete findings, delivery keeps it for up
to three repair attempts. Each attempt receives the findings and still requires
fresh evaluation and review. Useless, invalid or exhausted repair candidates are
not resumed automatically. Optimization workers retain their existing cleanup
behavior for uncommitted failed experiments; tool events remain. The JSON report lists base/head commit hashes,
scores, stop reason, timing, model calls, candidate summaries and review verdicts.

Review before removing a result worktree or candidate refs. Never blindly delete
a shared Git directory. Git garbage collection can reclaim objects only after
all references to them are removed. Logs contain source excerpts and command
output, so do not publish entire journals without inspecting them.

## Limits worth knowing

Small deterministic examples test plumbing and useful edits, not competence on a
large production system. Static scores can be gamed; visible tests can be overfit;
model reviewers can be wrong. Keep stronger tests, human review, and domain checks.
There is no automatic online training, learned routing, distributed queue,
merge-conflict solver, privileged sandbox, provider spend estimator, or GPU memory
scheduler. The loop improves a Git project; it does not change model weights.

## Persistent delivery

The normal goal entry point uses an outer controller around bounded experiment
epochs. Local epoch budgets remain finite; the goal can open a new epoch from the
accepted commit. Provider rate/quota failures carry safe retry metadata. HTTP
`Retry-After` is honored, including long waits; unknown reset times use backoff.
Waiting is persisted before sleeping and is distinct from completion. `--once`
returns after a bounded epoch or wait decision so an external service can manage
lifetime. A process must be running, or restarted by your service manager, to wake
and continue; writing a checkpoint is not an operating-system scheduler.

The goal controller does not infer that credentials grant unlimited scope. Put the
allowed accounts, regions, resource limits and release destination in INTENT.md.
Keep secret values in environment/keychain/provider profiles, not in intent prose.
Capability discovery reports tool availability without printing secrets.

External side effects are not transactional with Git. Rejecting a candidate or
removing its worktree does not remove a server, undo a database migration or revoke
a release. Use stable goal-derived names, inspect existing resources, and prefer
idempotent/declarative deployment tools. Avoid parallel competing deployments.
The runner does not provide an exactly-once distributed transaction system, and
its mobile/cloud workflows have not been production-validated in this release.
