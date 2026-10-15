"""Inference adapters and explicit routing. Credentials stay in environment."""
import json
import os
import sys
import threading
import time
from pathlib import Path
from .process import execute


# DNS, connection setup and a slowly streaming response all run inside a
# killable subprocess. Socket timeouts alone limit idle reads, not wall time.
HTTP_TRANSPORT = '''import json, sys, urllib.request
payload = json.load(sys.stdin)
request = urllib.request.Request(payload["url"],
    json.dumps(payload["body"]).encode(), payload["headers"])
with urllib.request.urlopen(request, timeout=payload["timeout"]) as response:
    data = response.read(1_000_001)
if len(data) > 1_000_000:
    raise ValueError("model response too large")
sys.stdout.buffer.write(data)
'''


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, calls, seconds):
        self.limit = calls
        self.calls = 0
        self.deadline = time.monotonic() + seconds
        self.lock = threading.Lock()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("wall time exhausted")
        return remaining

    def claim(self):
        with self.lock:
            self.remaining()
            if self.calls >= self.limit:
                raise BudgetExceeded("model call budget exhausted")
            self.calls += 1


def object_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("expected one JSON object")
    return result


class Router:
    def __init__(self, config, budget, log):
        self.models = config["models"]
        self.routes = config["routes"]
        self.budget = budget
        self.log = log
        self.slots = {name: threading.BoundedSemaphore(m.get("parallel", 1))
                      for name, m in self.models.items()}

    def ask(self, role, prompt, cwd, attempt=0):
        names = self.routes.get(role, self.routes["worker"])
        # Escalate after invalid responses or tool errors; transport errors try next route.
        names = names[min(attempt, len(names) - 1):]
        errors = []
        for name in names:
            model = self.models[name]
            if len(prompt.encode()) > model.get("context_bytes", 24000):
                errors.append(name + ": context budget exceeded")
                continue
            acquired = self.slots[name].acquire(timeout=self.budget.remaining())
            if not acquired:
                raise BudgetExceeded("model queue deadline")
            try:
                self.budget.claim()
                timeout = min(model.get("timeout", 180), self.budget.remaining())
                self.log("model_request", role=role, model=name, call=self.budget.calls)
                response = self._call(model, prompt, cwd, timeout)
                self.budget.remaining()
                result = object_json(response)
                self.log("model", role=role, model=name, prompt_bytes=len(prompt.encode()))
                return result
            except BudgetExceeded:
                raise
            except Exception as exc:
                self.budget.remaining()
                # Avoid storing URLs/headers/credentials from raw transport exceptions.
                errors.append(name + ": " + type(exc).__name__)
                self.log("model_error", role=role, model=name, error=type(exc).__name__)
            finally:
                self.slots[name].release()
        raise RuntimeError("all routes failed: " + "; ".join(errors))

    def _call(self, model, prompt, cwd, timeout):
        kind = model["kind"]
        if kind == "command":
            argv = [x.replace("{cwd}", str(cwd)).replace("{prompt}", prompt) for x in model["command"]]
            stdin = None if any("{prompt}" in x for x in model["command"]) else prompt
            result = execute(argv, cwd, timeout, stdin=stdin, limit=128000)
            if result["returncode"] or result["timed_out"] or result["truncated"]:
                raise RuntimeError("command model failed")
            return result["output"]
        if kind == "codex":
            # Final message lives separately from progress/diagnostics.
            import tempfile
            with tempfile.TemporaryDirectory(prefix="soso-model-") as folder:
                out = Path(folder) / "answer.json"
                argv = [model.get("executable", "codex"), "exec", "--ephemeral",
                        "--sandbox", "read-only", "--color", "never",
                        "-o", str(out)]
                if model.get("model"):
                    argv += ["-m", model["model"]]
                argv += model.get("args", []) + ["-"]
                result = execute(argv, cwd, timeout, stdin=prompt)
                if result["returncode"] or result["timed_out"] or not out.exists():
                    raise RuntimeError("codex model failed")
                if out.stat().st_size > 128000:
                    raise ValueError("model response too large")
                return out.read_text()
        if kind not in ("openai", "ollama"):
            raise ValueError("unknown model kind")
        endpoint = model["url"].rstrip("/")
        messages = [{"role": "user", "content": prompt}]
        if kind == "ollama":
            endpoint += "/api/chat"
            body = {"model": model["model"], "messages": messages, "stream": False,
                    "format": "json", "options": {"temperature": model.get("temperature", 0.2),
                    "num_predict": model.get("max_tokens", 2048),
                    "num_ctx": model.get("context_tokens", 8192)}}
        else:
            endpoint += "/chat/completions"
            body = {"model": model["model"], "messages": messages,
                    "max_tokens": model.get("max_tokens", 2048),
                    "temperature": model.get("temperature", 0.2)}
            if model.get("json_mode", True):
                body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if model.get("api_key_env"):
            headers["Authorization"] = "Bearer " + os.environ[model["api_key_env"]]
        payload = json.dumps({"url": endpoint, "body": body, "headers": headers,
                              "timeout": timeout})
        result = execute([sys.executable, "-I", "-c", HTTP_TRANSPORT], cwd, timeout,
                         stdin=payload, limit=1_000_000)
        if result["timed_out"] or result["returncode"] or result["truncated"]:
            raise RuntimeError("HTTP model failed")
        reply = json.loads(result["output"])
        return reply["message"]["content"] if kind == "ollama" else reply["choices"][0]["message"]["content"]
