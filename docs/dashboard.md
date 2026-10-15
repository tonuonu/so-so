# Run dashboard

Normal `./so-so` runs print a local dashboard URL before work starts. Open it to
follow activity without extra model calls, packages or external services. The
server runs only while that launcher is alive. Set `SOSO_DASHBOARD=0` to disable
it; a dashboard port failure does not stop the work.

The layout is independent of the assignment:

- Current phase, active model calls, elapsed call time and last recorded activity.
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

The server binds to loopback and uses an unguessable URL path. For a remote Linux
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
