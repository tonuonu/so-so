# Run dashboard

Normal `./so-so` runs print a local dashboard URL before work starts. Open it to
follow activity without extra model calls, packages or external services. The
server runs only while that launcher is alive. Set `SOSO_DASHBOARD=0` to disable
it; a dashboard port failure does not stop the work.

The layout is independent of the assignment:

- Current phase, actual active model names, routing aliases, roles, elapsed call
  time and last recorded activity.
- Planner output, contract milestones, reviewer-reported status and evidence.
- Per-model input/output tokens, input bytes, context capacity, duration and errors.
- Evaluation results, accepted/rejected experiments and a recent event timeline.
- Raster images already present in the result worktree, including screenshots
  when the worker creates them. The monitor does not capture screenshots itself.

## Goal insights are defined by the planner

There are no hardcoded refactoring, game, application or other intent-specific
cards. Planner responses can include an optional `monitoring` list:

```json
{"monitoring": [{"label": "Pages indexed", "source": "metric:pages", "unit": "pages"}]}
```

An evaluator can return `{"score": 80, "metrics": {"pages": 12}}` (delivery also
requires `complete`). Only finite numeric metrics are retained; with repeated
evaluation, supplemental metrics describe the final sample. The dashboard binds
labels to observed values, never to numbers invented in the planner response.
Other sources are `best_score`, `baseline_score`, `accepted_count` and
`plateau_count`. At most six cards are displayed. Unknown measurements remain
unavailable. This is data, not model-generated HTML, JavaScript or executable UI.

Milestone progress comes from `milestone_statuses` in approved reviews of the
accepted candidate, with zero-based `index`, `status` and `evidence`. A complete
milestone requires evidence text. These are model-reported judgments, not an
independent guarantee. Final acceptance marks all contract milestones complete.
Until all milestones have a reported status, no percentage is shown. There is
no general completion ETA: task sizes and revisions are too variable. A request's
timeout is not an ETA. Open-ended optimization does not get a fake percentage.

## Observe an existing or completed run

From an updated checkout:

```sh
python3 -m soso.dashboard --project /path/to/project --port 8765
```

This only reads `.so-so` records. It can observe older running versions without
restarting them; missing historical telemetry stays unavailable. The monitor
checks the launcher lock separately from saved status, so a stale `running`
record does not assert that a process is alive. Legacy in-flight calls are
best-effort because old logs lack unique call IDs.

By default the server binds to loopback and uses an unguessable URL path.
A project can set `dashboard.host` in its existing `soso.json` to a reachable LAN
address. The launcher binds that address and prints it automatically, including
when invoked as plain `./so-so`. `dashboard.public_host` optionally separates the
advertised hostname from the bind address; it is required for wildcard binding.
The environment equivalents are `SOSO_DASHBOARD_HOST` and
`SOSO_DASHBOARD_PUBLIC_HOST`. These settings expose read-only run evidence on the
selected interface; the random URL path remains required. For a remote Linux
host, run the monitor there and forward its port from your computer:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@host
```

Open the printed URL on your computer. No firewall port needs to be opened.
The monitor has no run/stop/edit endpoints. It serves only bundled UI assets,
selected journal data, and bounded raster images within managed run storage.

## Measurement limits

Call IDs correlate overlapping requests. Timings cover the provider invocation,
including transport and model loading, but exclude waiting for a worker slot.
Tokens are provider-reported after a response; command-line providers may not
report them. No tokenizer estimates or invented costs are shown. A `returned`
call means transport returned text, not that its protocol, code or tests passed.

Reads are bounded to recent epochs and log tails. The UI labels this window;
statistics are not lifetime billing totals. Prompt text and raw model responses
are not added to usage telemetry. Existing task descriptions and evaluator
results remain in the normal run journal. Rendering treats those strings as
text. Older runs cannot retrospectively gain metrics they did not record.

Temporary inference failures show their reason and automatic retry time. A stopped
launcher shows a saved retry time rather than promising an automatic retry.

Model entries may set `display_name` and `host_label` for readable identification.
These display-only fields are read live from the source `soso.json`; they do not
change the frozen execution policy. The endpoint hostname and port come from the
run's frozen model pool, with credentials and URL paths omitted. Model weight file
size is not a parameter count. Output tokens per second uses completed requests
with token telemetry and includes total request latency, not just decoding time.

## Workflow roadmap and performance cards

The roadmap distinguishes starting baseline checks from candidate validation.
It shows observed stages in the current epoch, not a guessed completion percentage.
Planning, implementation and review can repeat. Goal milestones remain separate
and come from the planner and accepted review evidence. A stopped run shows its
saved position; a waiting run shows the retry reason and time.

Model cards group display name, exact model identifier, host, route alias, active
request context, timeout, accumulated request time, live elapsed time, tokens,
median response latency and end-to-end throughput. Accumulated request time sums
parallel requests; it is not run wall-clock duration. Unfinished historical calls
cannot become live when a new epoch starts. Unmatched stopped calls have unknown
duration rather than an increasing timer. Token totals require provider telemetry;
GPU utilization, RAM use, live token streaming and billing are not measured here.

Temporary HTTP failures and model timeouts use the existing cooldown/retry loop.
Global epoch budgets still apply. CPU-hosted models can have a longer model
`timeout`, with a larger `max_seconds` allowance for the epoch. These are frozen
when a run starts; changing source configuration applies on the next launch.
