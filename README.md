# so-so

**Software on software.** Write what you want in `INTENT.md`, then run:

```sh
./so-so
```

Or give it the assignment directly:

```sh
./so-so "Build a browser platform game"
```

That is the normal interface. No user-written JSON, virtual environment, package
installation commands, model-backend switches, or manual Git setup are required.
The launcher prepares its runtime when necessary, reads your intent, discovers
available inference, and works in a managed copy of your project. Running it again
resumes matching unfinished work. Your original files remain available.

so-so is MIT-licensed. The host needs access to this repository and to the
inference services your assignment uses. The launcher cannot invent credentials
or system installation permission.

## Describe the outcome

`INTENT.md` is ordinary Markdown. Include the outcome and whatever resources or
preferences matter. For example:

```markdown
# Inventory application

Build a small inventory website. Users can add products, change stock counts,
search, and export CSV. Store data locally and include automated tests.

## Available resources

Ollama on http://my-server:11434 has my Qwen model and a smaller classifier model.
Kimi is available through the installed command-line tool.

## Routing preferences

Use the small model to classify jobs.
Routine code generation goes to local Qwen because it has no provider charge.
Difficult debugging and final review go to Kimi.
For a job that does not match a rule, use Kimi.
Wait when providers exhaust their quota and resume when capacity returns.
```

Use your actual endpoint and model names. You can describe any number of models,
command adapters, match rules, exclusions, specialists, and a default route. Model
preferences are translated into validated internal policy and retained with the
goal. Explicit policy determines eligible models; context limits and quota
availability then determine which eligible model receives the call. A small local
model or your own command can classify jobs into named routes. Initial policy
setup currently needs a discoverable Ollama service or an installed Codex, Claude
or Kimi CLI; a custom API or command alone is not yet sufficient to bootstrap it.

## What happens

so-so connects three things: your intent, sandboxed language models, and the host
command line. It sends bounded context to a model, executes its requested actions,
returns observations, and continues. The host handles files, Git, builds, tests and
commands; the model does not need direct access to your filesystem or GitHub.

The loop creates an acceptance plan, implements small steps, runs checks and reviews
results. Useful intermediate work survives later attempts. Completion requires
observed acceptance evidence and a separate review; a plateau or expired budget is
not delivery completion. Per-model quota waits are durable. Eligible alternatives
can keep working while another model waits.

Internal state and managed results live under `.so-so/`. The launcher prints the
result and evidence locations. Source snapshots exclude common credential files;
keep credentials in the host's normal credential stores, not in intent text.

Selected [Superpowers](https://github.com/obra/superpowers) planning, debugging,
testing and review skills are pinned and bundled with their license. Experiments
can run in parallel isolated worktrees; delivery integrates reviewed steps in order.

## Current evidence and limits

so-so supports local model servers and command-line model adapters. Your routing
preferences determine which models handle each job; no particular GPU, machine
or model is required.

See the [validation record](docs/validation.md) for automated checks, live project
trials, rejected candidates and measured self-improvement. Those results describe
specific test setups and do not guarantee completion of arbitrary assignments.

The runtime itself uses Python's standard library. Bootstrap can provision Python
locally and install Git through supported Linux package managers when permission
is available. The bundled self-improvement assignment also prepares missing Node
and C build tools for its existing checks. Models are not downloaded automatically. Commands run with host
permissions: Git worktrees are not an operating-system sandbox, and rolling back
source does not undo cloud operations. Model-generated acceptance checks and model
reviews can miss requirements.

Developer-only details: [routing internals](docs/routing.md),
[configuration schema](docs/configuration.md), [operations](docs/operations.md),
[Superpowers provenance](docs/superpowers.md). These are not setup steps for users.
