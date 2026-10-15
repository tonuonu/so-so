# Routing internals

This is a developer reference for the internal policy generated from user Markdown.
The user writes preferences in INTENT.md and runs `./so-so`; no JSON file or routing
flags are required. `soso.intent` discovers available adapters, translates prose
into aliases/rules, validates destinations, and stores the resulting policy.

The examples below describe that internal representation for development and
troubleshooting. They are not normal setup instructions. Model aliases and rankings
are chosen by the user's preferences, not hard-coded provider claims.

## Rules and the default

Rules are evaluated in order; the **first matching rule wins**. They match the job
text, not the tool protocol or incidental documentation in the model packet.

- `role`: optional exact `planner`, `worker`, or `reviewer` match.
- `contains`: any of these case-insensitive substrings must be present.
- `not_contains`: none of these case-insensitive substrings may be present.
- `regex`: optional Python regular expression, matched case-insensitively.
- `models`: ordered eligible destinations and fallbacks for this rule.

Conditions within a rule are combined with AND. An omitted condition is unrestricted.
For word boundaries, prefer a regex such as `"\\b(UI|UX|design|branding)\\b"` over
substring `"ui"`. A rule containing only `models` is a catch-all.

`routing.default: ["x"]` means exactly **no matching rule → X**. Without that key,
the role's `routes` list is the default. Explicit rules and defaults are authoritative;
the router does not silently choose an unlisted model.

## Optional cheap classifier or your own decision program

To classify unmatched jobs semantically, add a selector inside `routing`:

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

Here `fable`, `qwen`, `x` and `triage` are example aliases for models or command
adapters in the model pool. `x` denotes the chosen fallback; these names do not
install a provider or assert which model is best for a task.

The selector returns `{"route":"design"}` (or another configured label). `triage`
can be a cheap local model **or a command adapter implementing your own algorithm**.
It is called directly once, never recursively through itself. Ollama classifiers receive a JSON schema restricting output to the configured labels. Ollama calls default to `think: false` to reserve their output budget for actionable responses; explicit model preferences can enable thinking. Classifier calls also default to a short output limit. Rules run first;
the optional selector handles unmatched jobs; a failed/invalid/unavailable selector
falls through to the explicit default. Selector calls consume the same call/time
budget and respect their own context/concurrency limits. Logs record the selected
policy, label and destinations without storing classifier reasoning.

## Availability, context and cost

After policy chooses destinations, the router skips models whose input limit is
too small or whose quota cooldown has not expired. Errors try the next destination.
Repeated worker tool errors escalate within that same route. Delivery persists
per-model reset times across calls and restarts, so an exhausted first choice is
not repeatedly polled while a fallback works. If every eligible destination is
unavailable, the goal waits until the earliest known reset; unknown resets use
increasing delays. A model outside the chosen route is never used just because it
is available.

If `routes` is omitted from the pool, the loader derives each role's list from the
models' `roles` (default: all roles), sorting by ascending `priority`, then `cost`,
then declaration order. These are **your relative policy numbers**, not inferred
provider prices or a dollar-spend meter. `roles: []` keeps a classifier out of job
routes. Explicit routes/rules override this ordering. Each model has its own
`parallel`, `context_bytes` and `timeout` limits.

The initial pool is copied into durable goal state; edits to its original file do
not silently change a running goal's policy. Start a new goal to apply a changed
pool. `--backend` and `--model` are diagnostic overrides for deliberately testing a
single provider/model, not the normal pool-based interface.

New Markdown policies receive a separate semantic review before caching. The review
checks ordered fallbacks and model/resource preferences against the original intent.
Existing cached policies remain unchanged so an ongoing goal keeps its chosen
routing. Revise `INTENT.md` to compile and review a new policy for a new goal.


## Included deployment policy

The tracked `soso.json` contains the maintainer's working deployment, including
its LAN endpoint and installed model names. Other installations must adapt those
values, or set `use_project_model_policy` to `false` to restore discovery and
Markdown-based model preferences. The LAN endpoint is not a public service.

The tracked `soso.json` selects an explicit project model policy with
`use_project_model_policy: true`. The normal `./so-so` launcher uses it for both
`INTENT.md` and command-line assignments, before model discovery or intent-policy
cache lookup. No cache needs to be prepared by hand. Disabling that setting
restores Markdown-based policy compilation. An explicit project policy takes
precedence over model preferences in the assignment; edit the policy to change
its destinations. Existing running goals retain their frozen policy.

The Xeon-hosted large Qwen is the high-level planner. The local Qwen 27B
uncensored model performs code generation and routine reviews. Planning frequency
is determined by milestones, not a guaranteed percentage of model calls.

`routing.busy_overflow` permits Kimi only when the primary worker slot is occupied
and local Llama Guard 3:1b returns exactly `safe` for the complete assignment plus
outgoing request. Unsafe, malformed, unavailable, or oversized classification
stays local and waits for Qwen. The classifier never answers the request.
A failed local generation or a retry alone does not authorize cloud overflow.
If Kimi fails or has no credits, the request falls back to local Qwen.

The guard uses its native safe/unsafe protocol, not JSON classification. Its 8K
context has a conservative 8,000-byte input limit to leave room for the template;
larger requests stay local. This is a model verdict, not a proof of safety.

Two worker slots are enabled. Busy detection tracks calls sharing this Router
instance, including its reviews; it does not detect another process using Ollama.
The delivery planner currently schedules one next increment at a time, so overflow
only occurs when callers actually overlap. This is not a claim of universal
parallel milestone execution. Kimi must be installed and authenticated in the
launcher's PATH; otherwise the local fallback remains available.

Llama Guard and the 32K-context local Qwen were observed fully GPU-resident
together on a 24 GB RTX 3090 Ti, with roughly 3.4 GiB free. Larger contexts or
other workloads may change this. No INTENT run is started by configuring routing.
