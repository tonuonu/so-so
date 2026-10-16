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
