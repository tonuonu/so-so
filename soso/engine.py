"""Parallel experiments, immutable evaluation policy, durable evidence."""
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import shutil
import threading
import time
import uuid
from .models import Budget, BudgetExceeded, ModelUnavailable, Router, redact
from .process import execute, git
from .workspace import Workspace, worker
from .skills import prelude


DEFAULTS = {"rounds": 8, "patience": 3, "parallel": 2, "max_steps": 18,
            "max_calls": 200, "max_seconds": 3600, "command_timeout": 60,
            "evaluation_timeout": 120, "repeats": 1, "min_delta": 0.0,
            "direction": "max", "protected": ["AGENTS.md", "*/AGENTS.md", "INTENT.md",
            "soso.json", "evaluate.py", "tests/*", ".github/*", ".gitignore"]}


def load_config(path):
    config = {**DEFAULTS, **json.loads(Path(path).read_text())}
    for key in ("rounds", "patience", "parallel", "max_steps", "max_calls", "max_seconds",
                "command_timeout", "evaluation_timeout", "repeats"):
        if type(config[key]) is not int or not 1 <= config[key] <= 100000:
            raise ValueError(key + " must be a positive integer <= 100000")
    if config["direction"] not in ("min", "max"):
        raise ValueError("direction must be min or max")
    if not math.isfinite(config["min_delta"]) or config["min_delta"] < 0:
        raise ValueError("min_delta must be finite and nonnegative")
    if not isinstance(config["protected"], list) or not all(isinstance(x, str) for x in config["protected"]):
        raise ValueError("protected must be a list of glob patterns")
    config["protected"] = list(dict.fromkeys(DEFAULTS["protected"] + config["protected"]))
    commands = [config.get("evaluate")] + config.get("checks", [])
    for command in commands:
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("evaluate and checks must be nonempty argv arrays")
    models = config.get("models", {})
    if not models:
        raise ValueError("at least one model required")
    for name, model in models.items():
        if model.get("kind") not in ("openai", "ollama", "command", "codex", "kimi"):
            raise ValueError("invalid model kind: " + name)
        if type(model.get("parallel", 1)) is not int or model.get("parallel", 1) < 1:
            raise ValueError("model parallel must be a positive integer")
    routes = config.get("routes", {})
    if "worker" not in routes:
        raise ValueError("worker route required")
    for names in routes.values():
        if not isinstance(names, list) or not names or any(n not in models for n in names):
            raise ValueError("route must list configured model names")
    return config


def resources():
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    quota = Path("/sys/fs/cgroup/cpu.max")
    if quota.exists():
        parts = quota.read_text().split()
        if parts[0] != "max":
            cpus = min(cpus, max(1, math.ceil(int(parts[0]) / int(parts[1]))))
    gpus = []
    try:
        result = execute(["nvidia-smi", "--query-gpu=index,name,memory.free", "--format=csv,noheader"],
                         Path.cwd(), timeout=3)
        if result["returncode"] == 0:
            gpus = result["output"].strip().splitlines()
    except OSError:
        pass
    return {"cpus": cpus, "nvidia_gpus": gpus,
            "credential_sources": [name for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS") if os.environ.get(name)],
            "tools": {name: bool(shutil.which(name)) for name in
                      ("git", "python3", "node", "npm", "cc", "xcodebuild", "xcrun", "aws", "codex", "claude", "ollama")}}


def fingerprint(root, patterns):
    ws = Workspace(root, patterns)
    values = {}
    for name in ws.files():
        if ws.is_protected(name) or Path(name).name == "AGENTS.md":
            file = ws.path(name)
            if file.is_symlink():
                raise ValueError("protected files cannot be symlinks")
            values[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    return values


def evaluate(root, config, budget):
    results = []
    for command in config.get("checks", []):
        result = execute(command, root, min(config["evaluation_timeout"], budget.remaining()))
        budget.remaining()
        results.append(result)
        if result["returncode"] or result["timed_out"]:
            return {"valid": False, "reason": "check failed", "checks": results}
    scores, samples = [], []
    for _ in range(config["repeats"]):
        result = execute(config["evaluate"], root, min(config["evaluation_timeout"], budget.remaining()))
        budget.remaining()
        samples.append(result)
        if result["returncode"] or result["timed_out"]:
            return {"valid": False, "reason": "evaluator failed", "samples": samples}
        try:
            # Evaluators may print diagnostic lines, followed by the score object.
            payload = json.loads(result["output"].strip().splitlines()[-1])
            metric = payload["score"]
            if isinstance(metric, bool) or not isinstance(metric, (float, int)) or not math.isfinite(metric):
                raise ValueError("score must be finite numeric")
            if config.get("delivery") and (not 0 <= metric <= 100 or type(payload.get("complete")) is not bool):
                raise ValueError("delivery evaluator requires score 0..100 and boolean complete")
            scores.append(float(metric))
        except (ValueError, KeyError, IndexError, TypeError):
            return {"valid": False, "reason": "invalid metric", "samples": samples}
    return {"valid": True, "score": statistics.median(scores), "scores": scores,
            "complete": all(json.loads(s["output"].strip().splitlines()[-1]).get("complete") is True for s in samples),
            "checks": results, "samples": samples}


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class Run:
    def __init__(self, root, config, intent, allow_exec=False, resume=None):
        self.root = Path(root).resolve()
        self.config = dict(config, allow_exec=allow_exec)
        self.intent = intent
        self.budget = Budget(config["max_calls"], config["max_seconds"])
        self.started = time.monotonic()
        self.created_wall = time.time()
        self.elapsed_before = 0
        common = Path(git(self.root, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = (self.root / common).resolve()
        self.storage = common / "soso"
        self.storage.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.run_id = resume or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        if not self.run_id.replace("-", "").isalnum():
            raise ValueError("invalid run id")
        self.folder = self.storage / self.run_id
        self.folder.mkdir(exist_ok=bool(resume))
        self.integration = self.folder / "result"
        self.report_path = self.folder / "report.json"
        self.router = Router(config, self.budget, self.log)
        self.resume = resume

    def log(self, event, **data):
        with self.lock:
            with (self.folder / "events.jsonl").open("a") as stream:
                stream.write(redact(json.dumps({"time": time.time(), "event": event, **data}, default=str)) + "\n")

    def checkpoint(self):
        self.report["elapsed"] = self.elapsed_before + time.monotonic() - self.started
        self.report["calls"] = self.budget.calls
        self.report["head"] = git(self.integration, "rev-parse", "HEAD")
        atomic_json(self.report_path, json.loads(redact(json.dumps(self.report))))

    def run(self):
        with (self.storage / "lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another soso run owns this repository") from None
            return self._run()

    def _run(self):
        config_digest = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        intent_digest = hashlib.sha256(self.intent.encode()).hexdigest()
        if self.resume:
            self.report = json.loads(self.report_path.read_text())
            if self.report["config_sha256"] != config_digest or self.report["intent_sha256"] != intent_digest:
                raise ValueError("resume requires unchanged config, intent and execution permission")
            if git(self.integration, "status", "--porcelain"):
                raise ValueError("result worktree is dirty; inspect before resuming")
            if git(self.integration, "rev-parse", "HEAD") != self.report["head"]:
                raise ValueError("result HEAD changed since checkpoint; inspect before resuming")
            self.budget.calls = self.report["calls"]
            events = self.folder / "events.jsonl"
            if events.exists():
                for line in events.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # A torn final journal record is not a completed request.
                    if event.get("event") == "model_request":
                        self.budget.calls = max(self.budget.calls, event["call"])
            expiry = self.report["deadline_unix"]
            self.elapsed_before = max(0, self.created_wall - (expiry - self.config["max_seconds"]))
            self.budget.deadline = self.started + max(0, expiry - self.created_wall)
            self.report["status"] = "running"
        else:
            if git(self.root, "status", "--porcelain"):
                raise ValueError("commit or move pending changes before starting; source must be clean")
            base = git(self.root, "rev-parse", "HEAD")
            branch = "soso/" + self.run_id
            git(self.root, "worktree", "add", "-b", branch, str(self.integration), base)
            self.report = {"run_id": self.run_id, "branch": branch, "base": base,
                           "status": "running", "config_sha256": config_digest,
                           "intent_sha256": intent_digest, "rounds": [], "plateau": 0,
                           "resources": resources(), "elapsed": 0,
                           "deadline_unix": self.created_wall + self.config["max_seconds"]}
            before = fingerprint(self.integration, self.config["protected"])
            self.report["baseline"] = evaluate(self.integration, self.config, self.budget)
            if (git(self.integration, "rev-parse", "HEAD") != base
                    or git(self.integration, "status", "--porcelain")
                    or fingerprint(self.integration, self.config["protected"]) != before):
                self.report["baseline"] = {"valid": False, "reason": "baseline evaluation modified source"}
            if not self.report["baseline"]["valid"]:
                self.report["status"] = "invalid_baseline"
                self.checkpoint()
                return self.report
            self.report["best"] = self.report["baseline"]["score"]
        self.pinned = fingerprint(self.integration, self.config["protected"])
        self.checkpoint()
        try:
            if self.report.get("next_retry_at", 0) > time.time():
                self.report["status"] = "waiting"
                return self.report
            while len(self.report["rounds"]) < self.config["rounds"]:
                self.budget.remaining()
                if self.report["plateau"] >= self.config["patience"]:
                    self.report["status"] = "plateau"
                    break
                number = len(self.report["rounds"]) + 1
                tasks = self.plan()
                round_result = {"round": number, "experiments": [], "accepted": None}
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.config["parallel"], resources()["cpus"])) as pool:
                    futures = [pool.submit(self.experiment, task, number, i) for i, task in enumerate(tasks)]
                    for future in concurrent.futures.as_completed(futures):
                        round_result["experiments"].append(future.result())
                exhausted = [e for e in round_result["experiments"] if e.get("budget_exhausted")]
                if exhausted:
                    self.report["rounds"].append(round_result)
                    raise BudgetExceeded(exhausted[0]["error"])
                unavailable = [e["unavailable"] for e in round_result["experiments"] if e.get("unavailable")]
                if unavailable:
                    self.report["rounds"].append(round_result)
                    resets = [e["retry_at"] for e in unavailable if e["retry_at"] is not None]
                    raise ModelUnavailable(min(resets) if resets else None)
                valid = [x for x in round_result["experiments"] if x.get("evaluation", {}).get("valid")]
                valid.sort(key=lambda x: (self.utility(x["evaluation"]["score"]), -x["candidate"]), reverse=True)
                for best in valid:
                    if self.utility(best["evaluation"]["score"]) <= self.utility(self.report["best"]) + self.config["min_delta"]:
                        break
                    if self.review(best):
                        git(self.integration, "merge", "--ff-only", best["commit"])
                        self.report["best"] = best["evaluation"]["score"]
                        round_result["accepted"] = best["commit"]
                        break
                self.report["plateau"] = 0 if round_result["accepted"] else self.report["plateau"] + 1
                self.report["rounds"].append(round_result)
                self.log("round", round=number, best=self.report["best"], accepted=round_result["accepted"])
                self.checkpoint()
                print(json.dumps({"round": number, "best": self.report["best"], "accepted": round_result["accepted"]}), flush=True)
            else:
                self.report["status"] = "round_limit"
        except ModelUnavailable as exc:
            self.report["status"] = "waiting"
            self.report["next_retry_at"] = max(time.time() + 1, exc.retry_at) if exc.retry_at is not None else time.time() + 30
        except BudgetExceeded as exc:
            self.report["status"] = "budget_exhausted"
            self.report["reason"] = str(exc)
        except KeyboardInterrupt:
            self.report["status"] = "interrupted"
        except Exception as exc:
            self.report["status"] = "error"
            self.report["reason"] = str(exc)
        finally:
            self.checkpoint()
        return self.report

    def utility(self, score):
        return score if self.config["direction"] == "max" else -score

    def plan(self):
        if "planner" not in self.config["routes"]:
            return [self.intent] * self.config["parallel"]
        feedback = [{"round": r["round"], "accepted": r["accepted"],
                     "experiments": [{"task": e.get("task"), "evaluation": e.get("evaluation"),
                                      "error": e.get("error"), "review": e.get("review")} for e in r["experiments"]]}
                    for r in self.report["rounds"][-2:]]
        prompt = (prelude("planner") + "Propose independent competing experiments for the same current source baseline. "
                  "They are alternatives, not dependent tasks. Return only JSON: "
                  '{"experiments":[{"task":"specific small improvement and files to inspect"}]}. '
                  f"At most {self.config['parallel']} experiments. Optimize {self.config['direction']}.\n"
                  + "INTENT:\n" + self.intent + "\nFILES:\n"
                  + json.dumps(Workspace(self.integration, []).files()[:100])
                  + "\nRECENT FEEDBACK:\n" + json.dumps(feedback)[-6500:])
        reply = self.router.ask("planner", prompt, self.integration, task=self.intent)
        tasks = reply.get("experiments", [])[:self.config["parallel"]]
        if not tasks or any(not isinstance(t, dict) or not isinstance(t.get("task"), str) or not t["task"] for t in tasks):
            raise ValueError("planner returned invalid experiments")
        return [self.intent + "\nEXPERIMENT:\n" + t["task"][:2000] for t in tasks]

    def review(self, candidate):
        if "reviewer" not in self.config["routes"]:
            return True
        diff = git(self.integration, "diff", "HEAD", candidate["commit"])
        # Do not silently review a truncated patch.
        prompt = (prelude('reviewer') + 'Review this measured candidate for correctness, evaluator gaming and scope. '
                  'Return JSON {"approve":true,"reason":"..."}.\nINTENT:\n' + self.intent
                  + "\nDIFF:\n" + diff + "\nEVALUATION:\n" + json.dumps(candidate["evaluation"]))
        reply = self.router.ask("reviewer", prompt, self.integration, task=candidate["task"])
        candidate["review"] = reply
        return reply.get("approve") is True

    def experiment(self, task, round_number, index, continuation=None):
        name = f"r{round_number}-c{index}-{uuid.uuid4().hex[:6]}"
        path = self.folder / name
        result = {"candidate": index, "task": task}
        preserve = getattr(self, 'retain_candidate', None)
        cleanup = True
        phase = 'worker'
        expected = None
        def retain():
            nonlocal cleanup
            if preserve and path.exists() and expected:
                cleanup = False  # Keep the worktree if its durable checkpoint fails.
                preserve(path, result, base, expected, name, phase)
                cleanup = True
        try:
            base = git(self.integration, "rev-parse", "HEAD")
            expected = base
            if continuation:
                if not preserve:
                    raise ValueError('candidate continuation requires delivery mode')
                expected = continuation['commit']
            git(self.root, "worktree", "add", "--detach", str(path), expected)
            if continuation and fingerprint(path, self.config['protected']) != self.pinned:
                raise ValueError('retained candidate policy changed')
            if not continuation or continuation['phase'] == 'worker':
                result["worker"] = worker(self.router, task, path, self.config, self.log, index)
            if fingerprint(path, self.config["protected"]) != self.pinned:
                raise ValueError("protected files changed")
            if git(path, "rev-parse", "HEAD") != expected:
                raise ValueError("worker changed Git history")
            if git(path, "status", "--porcelain"):
                git(path, "add", "--all")
                git(path, "-c", "commit.gpgsign=false", "commit", "-m", f"Experiment {round_number}.{index}")
            elif expected == base:
                result["evaluation"] = {"valid": False, "reason": "no changes"}
                return result
            result["commit"] = expected = git(path, "rev-parse", "HEAD")
            phase = 'evaluate'
            if preserve:
                retain()  # Persist before evaluation or review can exhaust the epoch.
            result["evaluation"] = evaluate(path, self.config, self.budget)
            if (git(path, "rev-parse", "HEAD") != result["commit"]
                    or fingerprint(path, self.config["protected"]) != self.pinned
                    or git(path, "status", "--porcelain")):
                raise ValueError("evaluation modified source or protected files")
            # Retain accepted/rejected commits beyond Git's dangling-object grace period.
            git(self.root, "update-ref", f"refs/soso/{self.run_id}/{name}", result["commit"])
        except (ModelUnavailable, BudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
            retain()
            if isinstance(exc, ModelUnavailable):
                result["unavailable"] = {"retry_at": exc.retry_at}
                result["evaluation"] = {"valid": False, "reason": "provider unavailable"}
            elif isinstance(exc, BudgetExceeded):
                result["budget_exhausted"] = True
                result["error"] = str(exc)
                result["evaluation"] = {"valid": False, "reason": "budget exhausted"}
            elif isinstance(exc, SystemExit) or not preserve:
                raise
            else:
                result['interrupted'] = True
                result['evaluation'] = {'valid': False, 'reason': 'interrupted'}
        except Exception as exc:
            result["error"] = str(exc)[:2000]
            result["evaluation"] = {"valid": False, "reason": type(exc).__name__}
        finally:
            if cleanup and path.exists():
                with contextlib.suppress(RuntimeError):
                    git(self.root, "worktree", "remove", "--force", str(path))
        self.log("experiment", **result)
        return result
