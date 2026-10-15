# Internal configuration reference

The user-facing interface is `./so-so` with INTENT.md. The launcher generates and
stores these settings internally. This document is for developers inspecting
state or exercising the older diagnostic/optimization interfaces; users do not
need to author this JSON or manage Python environments.

Commands are argv arrays, not shell strings. The following illustrates the
internal representation of models, routes, acceptance checks and epoch limits.

```json
{
  "models": {
    "local": {
      "kind": "ollama",
      "url": "http://127.0.0.1:11434",
      "model": "YOUR_INSTALLED_MODEL",
      "context_tokens": 8192,
      "context_bytes": 18000,
      "max_tokens": 2048,
      "parallel": 1,
      "timeout": 180
    },
    "strong": {
      "kind": "codex",
      "executable": "codex",
      "args": ["--ignore-user-config"],
      "parallel": 2,
      "context_bytes": 24000,
      "timeout": 180
    }
  },
  "routes": {"planner": ["strong"], "worker": ["local", "strong"], "reviewer": ["strong"]},
  "evaluate": ["python3", "evaluate.py"],
  "checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
  "direction": "max",
  "repeats": 3,
  "min_delta": 0.01,
  "parallel": 2,
  "rounds": 8,
  "patience": 3,
  "max_steps": 18,
  "max_calls": 200,
  "max_seconds": 3600,
  "command_timeout": 60,
  "evaluation_timeout": 120,
  "protected": ["benchmarks/*"]
}
```

`worker` is required. Omit `planner` to use the intent directly for every alternative; omit `reviewer` for evaluator-only acceptance. Those omissions reduce cost but reduce guidance and defense against evaluator blind spots.

## Providers

- `ollama`: `url` is the server root; uses `/api/chat`, JSON format, explicit context/output limits.
- `openai`: OpenAI-compatible Chat Completions protocol; `url` includes `/v1`, such as `http://localhost:8000/v1`. Set `api_key_env` to the **name** of an environment variable. `json_mode: false` supports servers without JSON-mode support. This is a protocol adapter, not a requirement to use any specific hosted provider.
- `codex`: launches `codex exec --ephemeral --sandbox read-only`, passes a prompt over stdin, reads the final response from a separate temporary output file. Optional `model` selects a model; omitted uses the CLI default. Extra `args` are trusted. The CLI's auth, availability and own internal limits apply.
- `command`: executes `command` and expects one JSON object on stdout. Prompt goes to stdin unless an argument contains `{prompt}`. `{cwd}` expands to the candidate directory. Diagnostics must go to stderr or be suppressed by your wrapper; the runner's process capture combines streams, so noisy CLI output needs an adapter that isolates its final response.

Example command adapter for Claude Code (verify flags with your installed CLI):

```json
{"kind":"command", "command":["claude","-p","--output-format","text","--tools",""], "parallel":1}
```

The dedicated `kimi` adapter invokes prompt mode with a temporary agent profile that disables tools and subagents, and extracts the final assistant answer from the JSON event stream. Per-process reasoning and output limits default to low effort and 8192 tokens; model settings can override these without changing global CLI configuration. Provider truncation is reported separately from JSON syntax failure. Generic command adapters must provide their own permission settings and return protocol JSON. No vendor CLI is automatically installed or authenticated.

[Codex non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode) describes its final-message output support. Local CLI help was also checked during implementation.

## Budgets and permissions

A call is counted before dispatch, including failed calls. Endpoint waiting and inference consume wall time. An HTTP request or subprocess has its own configured timeout, capped by remaining run time. `max_steps` is per worker; skills, reads and checks count as steps. Repeated checks can be costly. No dollar budget is inferred from provider prices; bound calls and output tokens and use provider-side spend limits where needed.

Default protection covers root/nested `AGENTS.md`, `INTENT.md`, the default config, evaluator, existing tests, CI and `.gitignore`; custom patterns are added, not substituted. Pin every evaluator dependency and data file you want immutable. Protection is checked again after worker completion and after evaluation. It is a correctness guard for cooperative workers, not a security boundary against arbitrary shell access.

Use `check` for the fixed verification commands without granting arbitrary command execution. Use `--allow-exec` only when your surrounding environment is suitable for arbitrary project commands.

## Automatic goal setup

`soso "goal text"` and bare `soso` use delivery setup rather than requiring this
file. Bare invocation reads `INTENT.md`. If no policy exists, a planning call
produces a milestone/acceptance contract and a Python evaluator; the runner freezes
it before implementation. This generated contract is inspectable source in the
goal worktree. Existing policy can be used when you want externally defined checks.

`--backend ollama --model NAME` uses the exact local model name. `--backend codex`
or `--backend claude` selects that adapter. Auto discovery prefers local inference
for worker steps and can use installed stronger CLI adapters for planning, review
and fallback. `SOSO_OLLAMA_URL` chooses the Ollama server, and `SOSO_LOCAL_ONLY`
restricts automatic selection to local inference.

Delivery supports `--once`, `--resume-goal ID`, and `--no-exec`. Provider waiting
and configured epoch limits are separate from optimization `run --resume`, whose
original absolute deadline remains unchanged.
