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
