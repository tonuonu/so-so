# Routing

Write model preferences in `INTENT.md` and run `./so-so`. The launcher discovers
adapters, translates preferences into routing policy, validates destinations and
saves the policy with the goal. This reference describes that policy for users who
want to inspect or edit it directly.

## Rules and the default

Rules run in order; the **first matching rule wins**. They match the assignment
text rather than the surrounding tool protocol.

- `role`: optional exact role, such as `architect`, `coordinator`, `planner`, `worker` or `reviewer`.
- `contains`: at least one listed substring must appear, ignoring case.
- `not_contains`: none of the listed substrings may appear, ignoring case.
- `regex`: optional Python regular expression, matched ignoring case.
- `models`: ordered eligible destinations and fallbacks.

Conditions within a rule are combined with AND. Omitted conditions impose no
restriction. Use a regex such as `"\\b(UI|UX|design|branding)\\b"` to match whole
words; substring `"ui"` also matches unrelated words. A rule containing only
`models` matches every job.

When no rule matches, the optional classifier runs next. If it cannot select a
route, `routing.default` applies; without that setting, the role's `routes` list
applies. The router never selects an unlisted model.

## Optional classifier

A selector can classify unmatched jobs into named routes:

```json
"selector": {
  "model": "triage",
  "prompt": "Visual design, UI, UX and branding use design. Routine coding uses code. Everything else uses other.",
  "routes": {
    "design": ["fable", "x"],
    "code": ["qwen", "x"],
    "other": ["x"]
  }
}
```

Each name must identify a model or command adapter in the pool. `triage` returns
an object such as `{"route":"design"}`. It can be a small local model or a command
implementing your own classification algorithm.

The selector is called directly, without recursive routing. Its calls consume the
shared call and time budget and respect context and concurrency limits. Invalid
output or an unavailable classifier falls through to the configured default.
Logs record the selected label and destinations, not classifier reasoning.

Ollama classifiers receive a JSON schema restricting output to configured labels
and a short output limit. Ollama calls default to `think: false`; a model's
configuration can enable thinking.

## Availability, limits and response repair

After selecting a route, the router skips models whose input limit is too small
or whose cooldown has not expired. Provider failures can use the next eligible
destination. Repeated worker tool errors can escalate within the same route.

Malformed JSON and invalid role responses receive up to two repair attempts on
the same model. Every attempt consumes budget and must fit the model's context.
Exhausted repairs produce a protocol failure; they do not trigger cloud overflow
or count as an optimization plateau. Raw malformed output is not logged.

Provider cooldowns persist across calls and restarts. When no eligible model is
available, the goal waits until the earliest known reset. Unknown reset times use
increasing delays.

If `routes` is omitted, the loader derives role lists from models' `roles`, sorting
by ascending `priority`, then `cost`, then declaration order. These are relative
policy values, not measured provider prices. `roles: []` excludes a classifier
from job routes. Explicit routes and rules override this ordering. Each model has
its own `parallel`, `context_bytes` and `timeout` limits.

## Changing policy

The initial pool is saved with the goal. Editing its source does not change an
already running goal; start a new goal to apply a different pool.

New Markdown policies receive a semantic review against the original intent before
being cached. Change `INTENT.md` to compile a policy for a new goal. Diagnostic
`--backend` and `--model` overrides test a single provider; normal runs use the pool.

## Included deployment policy

The repository's `soso.json` enables `use_project_model_policy`. It takes precedence
over discovery and Markdown model preferences for both `INTENT.md` and command-line
assignments. Adapt its endpoints, model names and dashboard address to your machines.
Set `use_project_model_policy` to `false` to use discovery and Markdown preferences.

The included routes divide work as follows:

| Role | Model | Work |
| --- | --- | --- |
| Architect | Large Qwen on the CPU inference server | Compact milestones, constraints and acceptance guidance |
| Coordinator | Local Qwen 27B | Choose the next implementation step from the strategy and results |
| Worker and reviewer | Local Qwen 27B | Generate code and review changes |
| Overflow guard | Llama Guard 3:1b | Classify requests before Kimi overflow |
| Overflow worker | Kimi CLI | Handle eligible work when the local worker is busy |

The architect's strategy is saved for the intent and model policy, then reused
across restarts and optimization epochs. After two evaluated unsuccessful rounds,
the coordinator can request one strategic revision with a specific reason.
Provider failures and malformed responses do not qualify. Architecture guidance
cannot replace frozen acceptance checks.

### Guarded overflow

`routing.busy_overflow` permits Kimi only when the local worker slot is occupied
and Llama Guard returns exactly `safe` for the complete assignment and outgoing
request. Unsafe, malformed, unavailable or oversized classification keeps the
request local. A failed local generation or retry alone never authorizes overflow.
If Kimi fails or has no credits, the request falls back to local Qwen.

The guard uses its native safe/unsafe protocol with an 8,000-byte input limit.
Larger requests stay local. A guard verdict is a model classification, not a
proof of safety.

Busy detection covers calls sharing one router instance, including reviews; it
does not detect other processes using Ollama. Two parallel candidate workers are
configured, while delivery schedules one increment at a time. Overflow therefore
requires actual overlapping calls. Kimi must be installed and authenticated in the
launcher's `PATH` to receive work.
