# Operations and limits

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

A persisted absolute wall deadline survives crashes and includes downtime. Resume
never replenishes time or already-journaled model calls.

Only one run owns a repository at a time (POSIX file lock in the common Git directory).
Source must be clean. Worktrees and configuration are pinned to committed source;
ignored caches are not copied into candidate worktrees. Submodule and large-data
setup are the caller's responsibility. Git hooks are disabled for runner-managed
Git commands; do not rely on hooks as your evaluator.

A run retains its result branch/worktree and candidate commit refs. Successful
candidate commits are kept under `refs/soso/RUN_ID/`, including measured rejected
candidates. Failed, uncommitted worker edits are discarded when that worker is
cleaned up; its tool events remain. The JSON report lists base/head commit hashes,
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
