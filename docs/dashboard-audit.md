# Dashboard audit

The dashboard answers five operational questions: where the run is, which model
and host are working, how long requests take, what progress has evidence, and
whether the user must act.

## Changes from the audit

- A roadmap distinguishes preparation and baseline checks from implementation and
  candidate validation. It shows recorded stages within the current epoch; the
  middle stages repeat, and their position is not a completion percentage.
- Model cards put readable identity, exact model ID, endpoint host, routing alias,
  live request details and performance measurements together. They reflow on
  narrow screens instead of requiring a wide table.
- Total request time includes in-flight requests. End-to-end throughput only uses
  completed requests with measured output tokens. Missing telemetry is explained.
- Historical unfinished requests cannot become active in a new epoch. Stopped
  requests without a recorded end have unknown duration, not a ticking clock.
- Temporary provider errors retain their cause through planner and worker retry
  paths. Waiting shows the automatic retry time; stopped runs show next steps.
- Goal-specific milestones and metrics remain planner-defined and evidence-based.
  No task-specific metric is hardcoded into the dashboard.
- Secondary details, history and visual assets use expandable sections. The page
  has one brand heading, clear focus outlines, a skip link, stable connection
  announcements and text labels alongside colored states.

## Measurement boundaries

The roadmap is runner activity, not an independent assessment of feature quality.
Milestone status is reported by approved reviews. Throughput includes request
latency and is not decoding speed. CPU/GPU utilization, RAM consumption, live
streamed token counts, costs and a reliable completion ETA are not recorded by
this monitor. Unknown values remain unknown.

Long-running CPU models need a model timeout and an epoch time allowance suited
to their throughput. Increasing a timeout does not make the model faster. A
future architectural improvement is a rarely-invoked high-level architect with
a faster coordinator handling implementation cycles; this audit does not claim
that scheduling architecture is implemented.
