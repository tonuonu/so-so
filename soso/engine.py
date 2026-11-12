"""Parallel experiments, immutable evaluation policy, durable evidence."""
import concurrent.futures
import contextlib
import fcntl
import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
import statistics
import shutil
import threading
import time
import uuid
from .models import Budget, BudgetExceeded, ModelProtocolError, ModelUnavailable, Router, redact_value
from .process import execute, git
from .workspace import Workspace, gitlinks, worker
from .skills import prelude
from .architecture import planning_reply


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
        if ws.is_protected(name) or Path(name).name.casefold() == "agents.md":
            file = ws.path(name)
            if file.is_symlink():
                raise ValueError("protected files cannot be symlinks")
            values[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    return values


def index_flags(root):
    """Index entries marked skip-worktree ("S") or assume-unchanged (lowercase); Git ignores their on-disk edits."""
    return {x for x in git(root, "ls-files", "-v", "-z").split("\0") if x[:1] == "S" or x[:1].islower()}


# Settings that change how content is staged or checked out, and the include and worktree-config
# switches that can bring such settings in; other config (remotes, branches, user edits) is never touched.
WATCHED = re.compile(r"filter\..*|include\..*|includeif\..*|extensions\.worktreeconfig|"
                     r"core\.(attributesfile|autocrlf|eol|safecrlf|fsmonitor|worktree|sparsecheckout.*)|index\..*|"
                     r"core\.(symlinks|filemode)|sparse\..*|attr\..*|diff\..*", re.I)  # diff.*: the reviewer's view
INCLUDE = re.compile(r"include\.path|includeif\..*\.path", re.I)
MAX_INCLUDED, MAX_CONFIG_BYTES = 64, 1 << 20


def watched_config(path, source, file):
    """Watched settings of --local or one config file, includes expanded, in file order: ([(key, value)]
    written in that file itself, [(key, value)] in effect, {key: [files]} that supply included values)."""
    if source != "--local" and not source.is_file():
        return [], [], {}
    where = ["--local"] if source == "--local" else ["--file", str(source)]
    parts = git(path, "config", *where, "--includes", "--show-origin", "--list", "-z", ok=(0, 1)).split("\0")
    direct, effective, included = [], [], {}
    for origin, entry in zip(parts[::2], parts[1::2]):
        key, _, value = entry.partition("\n")
        if WATCHED.fullmatch(key):
            value = value if "\n" in entry else "true"
            origin = os.path.realpath(os.path.join(path, origin.removeprefix("file:")))
            effective.append((key, value))
            if origin == file:
                direct.append((key, value))
            else:
                included.setdefault(key, []).append(origin)
    return direct, effective, included


def config_bytes(name):
    """A config file's bytes, at most one over MAX_CONFIG_BYTES; None when it does not exist."""
    try:
        with open(name, "rb") as stream:
            return stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None


def included(path, config):
    """Bytes (None if missing) of every file that config reaches through include.path or includeIf.*.path,
    transitively and whatever the condition: a worker can edit them, but key-level restore cannot."""
    found, pending = {}, [os.fspath(config)]
    while pending:
        source = pending.pop()
        if not os.path.isfile(source):
            continue
        for entry in git(path, "config", "--file", source, "--list", "-z").split("\0"):
            key, _, value = entry.partition("\n")
            if not INCLUDE.fullmatch(key) or not value:
                continue
            name = os.path.abspath(os.path.join(os.path.dirname(source), os.path.expanduser(value)))
            if name not in found:
                found[name] = config_bytes(name)
                if len(found) > MAX_INCLUDED or len(found[name] or b"") > MAX_CONFIG_BYTES:
                    raise ValueError("included Git configuration is too large to record: " + name)
                pending.append(name)
    return found


def dump_watched(watched):
    encode = lambda data: None if data is None else base64.b64encode(data).decode()
    return {"local": watched[0], "attributes": encode(watched[1]),
            "included": {name: encode(data) for name, data in watched[2].items()}}


def load_watched(saved):
    decode = lambda text: None if text is None else base64.b64decode(text)
    return (tuple([tuple(entry) for entry in entries] for entries in saved["local"]), decode(saved["attributes"]),
            {name: decode(text) for name, text in saved.get("included", {}).items()})  # older runs: none recorded

def differs_from_commit(path, commit, flagged):
    """Tracked files whose content on disk differs from the commit under Git's own conversion rules.

    A fresh index has no stat cache, so every file is hashed again; a worker's own git add with a
    filter cannot carry its blob past this check. Skip-worktree entries the fresh worktree had are not on disk."""
    index = Path(git(path, "rev-parse", "--absolute-git-dir")) / "soso-check-index"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        git(path, "read-tree", commit, env=env)
        git(path, "update-index", "-q", "--refresh", env=env, ok=(0, 1))
        names = git(path, "diff-files", "--name-only", "-z", env=env).split("\0")
    finally:
        index.unlink(missing_ok=True)
    skipped = {entry[2:] for entry in flagged if entry[:1] == "S"}
    return [name for name in names if name and name not in skipped]


def checkout(path, commit, flagged):
    """Make the worktree a fresh checkout of commit under the current config; return what Git left behind.

    The index is rebuilt from the commit with only the fresh worktree's skip-worktree entries, every file
    on disk is deleted (git clean against an empty index), and every file is written again, so no file,
    index flag or stat cache of the worker survives; filters, ident and line endings apply as on a clone."""
    git(path, "-c", "core.sparseCheckout=false", "-c", "index.sparse=false", "read-tree", commit)
    skipped = "".join(entry[2:] + "\0" for entry in sorted(flagged) if entry[:1] == "S")
    if skipped:  # outside the sparse checkout: absent, as in the fresh worktree and the baseline
        git(path, "update-index", "-z", "--skip-worktree", "--stdin", stdin=skipped)
    empty = Path(git(path, "rev-parse", "--absolute-git-dir")) / "soso-empty-index"
    empty.unlink(missing_ok=True)
    git(path, "clean", "-ffdqx", ok=(0, 1), env={"GIT_INDEX_FILE": str(empty)})
    left = [name for name in os.listdir(path) if name != ".git"]  # e.g. special files or read-only folders
    if not left:
        git(path, "checkout-index", "-u", "-a", "-f")  # -u: the index records the new files, as a checkout does
    return left


def nested_git(root):
    """Entries named .git below the worktree root: Git skips them and everything inside them."""
    found, top = set(), os.fspath(root)
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            if name.casefold() == ".git" and not (folder == top and name == ".git"):
                found.add(os.path.relpath(os.path.join(folder, name), top))
        dirs[:] = [d for d in dirs if d.casefold() != ".git"]
    return found


PYTHON = re.compile(r"(python|pypy)[0-9.]*[tw]?(\.exe)?", re.I)  # interpreter names, not python-dateutil
VALUE_OPTIONS = {"--python", "-p", "--with", "--interpreter"}  # wrapper options whose value may look like python


def _python_entry(command):
    """("-m", module) or ("path", arg) that python runs, also behind wrappers such as env, uv run or
    poetry run, or through coverage run -m and uv run -m; None for -c, stdin or a command without python."""
    for i, arg in enumerate(command):
        if PYTHON.fullmatch(Path(arg).name) and (i == 0 or command[i - 1] not in VALUE_OPTIONS):
            return _python_options(command[i + 1:])
    for i, arg in enumerate(command[:-1]):
        if Path(arg).name.casefold() in ("coverage", "uv") and command[i + 1] == "run":
            # These run a module themselves; an -m anywhere after "run" is treated as one.
            for j, flag in enumerate(command[i + 2:], i + 2):
                if flag in ("-m", "--module"):
                    return ("-m", command[j + 1]) if j + 1 < len(command) else None
                if flag.startswith("-m"):
                    return ("-m", flag[2:])
            return None
    return None


def _python_options(args):
    """Python's own options: what -m, -c, a script path or a directory after them runs."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return ("path", args[i + 1]) if i + 1 < len(args) else None
        if arg == "-" or not arg.startswith("-"):
            return None if arg == "-" else ("path", arg)
        if not arg.startswith("--"):
            for j, flag in enumerate(arg[1:], 2):
                if flag in "cmWX":  # takes a value: the rest of this argument, or the next one
                    value = arg[j:] or (args[i + 1] if i + 1 < len(args) else "")
                    if flag in "cm":
                        return ("-m", value) if flag == "m" else None
                    if not arg[j:]:
                        i += 1
                    break
        i += 1
    return None


def local_programs(root, commands):
    """Repository-relative names of in-repo files that evaluator and check commands run.

    Existing argument files are named as written and as resolved through symlinks. For python3 -m a.b,
    every file that could supply a or a.b is named even before it exists, as is the __main__.py of a
    python3 directory argument, so creating one changes the fingerprint.
    """
    root = os.path.realpath(root)
    names = [(arg, True) for command in commands for arg in command]
    for command in commands:
        kind, target = _python_entry(command) or (None, "")
        parts = target.split(".")
        if kind == "-m" and all(parts):
            for i in range(1, len(parts)):
                names += [("/".join(parts[:i]) + "/__init__.py", False), ("/".join(parts[:i]) + ".py", False)]
            module = "/".join(parts)
            names += [(module + ".py", False), (module + "/__init__.py", False), (module + "/__main__.py", False)]
        elif kind == "path" and os.path.isdir(os.path.join(root, target)):
            names.append((target + "/__main__.py", False))
    programs = []
    for name, existing in names:
        try:
            written = os.path.normpath(os.path.join(root, name))  # "a/../b" and "../<repo>/b" name "b"
            for path in (written, os.path.realpath(written)):
                if os.path.commonpath([root, path]) == root and (not existing or os.path.isfile(path)):
                    programs.append(os.path.relpath(path, root).replace(os.sep, "/"))
        except (OSError, ValueError):
            continue  # e.g. a -c program text that is no usable file name
    return list(dict.fromkeys(programs))


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
    metrics = payload.get("metrics", {})
    metrics = {k: v for k, v in metrics.items() if isinstance(k, str) and len(k) <= 80
               and type(v) in (int, float) and math.isfinite(v)} if isinstance(metrics, dict) else {}
    return {"valid": True, "score": statistics.median(scores), "scores": scores, "metrics": dict(list(metrics.items())[:30]),
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
        # Watched shared settings, info/attributes and included config files, recorded when the first
        # candidate worktree is created; restores counts restores that changed something.
        self.attributes, self.watched = common / "info" / "attributes", None
        self.config_file = os.path.realpath(common / "config")
        self.started_experiments = self.active_experiments = self.restores = 0
        self.storage.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.run_id = resume or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        if not self.run_id.replace("-", "").isalnum():
            raise ValueError("invalid run id")
        self.unrestored = self.storage / "unrestored.json"
        if not resume and self.unrestored.is_file():
            # A setting a worker left in place must not become a new run's baseline.
            failed = json.loads(self.unrestored.read_text())
            recorded = load_watched(json.loads((self.storage / failed["run"] / "watched-config.json").read_text()))
            if self.shared(self.root, recorded[2]) != recorded:
                raise ValueError(f"run {failed['run']} could not restore {failed['file']}; put back the settings recorded "
                                 f"in {self.storage / failed['run'] / 'watched-config.json'}, or delete "
                                 f"{self.unrestored} to keep the current ones, before starting a new run")
            self.unrestored.unlink()
        self.folder = self.storage / self.run_id
        self.folder.mkdir(exist_ok=bool(resume))
        saved = self.folder / "watched-config.json"
        if resume and saved.is_file():  # a change left by a killed worker must not become the baseline
            self.watched = load_watched(json.loads(saved.read_text()))
        self.integration = self.folder / "result"
        self.report_path = self.folder / "report.json"
        self.router = Router(config, self.budget, self.log)
        self.resume = resume

    def log(self, event, **data):
        with self.lock:
            with (self.folder / "events.jsonl").open("a") as stream:
                record = json.loads(json.dumps({"time": time.time(), "event": event, **data}, default=str))
                stream.write(json.dumps(redact_value(record)) + "\n")

    def checkpoint(self):
        self.report["elapsed"] = self.elapsed_before + time.monotonic() - self.started
        self.report["calls"] = self.budget.calls
        self.report["head"] = git(self.integration, "rev-parse", "HEAD")
        atomic_json(self.report_path, redact_value(self.report))

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
        self.checkpoint()  # The worktree and branch are durable evidence before evaluation.
        try:
            if not self.report.get("baseline", {}).get("valid"):
                # Fresh, interrupted or previously invalid baselines are measured before any round.
                before = fingerprint(self.integration, self.config["protected"])
                self.log("activity", phase="baseline", task="Baseline checks")
                self.report["baseline"] = evaluate(self.integration, self.config, self.budget)
                if (git(self.integration, "rev-parse", "HEAD") != self.report["base"]
                        or git(self.integration, "status", "--porcelain")
                        or fingerprint(self.integration, self.config["protected"]) != before):
                    self.report["baseline"] = {"valid": False, "reason": "baseline evaluation modified source"}
                if not self.report["baseline"]["valid"]:
                    self.report["status"] = "invalid_baseline"
                    return self.report
                self.report["best"] = self.report["baseline"]["score"]
            self.pinned = fingerprint(self.integration, self.config["protected"])
            self.checkpoint()
            if self.report.get("next_retry_at", 0) > time.time():
                self.report["status"] = "waiting"
                return self.report
            while self.completed_rounds() < self.config["rounds"]:
                self.budget.remaining()
                if self.report["plateau"] >= self.config["patience"]:
                    self.report["status"] = "plateau"
                    break
                previous = self.report["rounds"][-1] if self.report["rounds"] else None
                if previous and previous.get("outcome") == "incomplete" and self.accept(previous):
                    round_result = previous  # Resume reviewed a valid candidate its interruption left pending.
                else:
                    number = self.completed_rounds() + 1
                    tasks = self.plan()
                    # Recorded before work starts; an incomplete round is retried, not counted.
                    round_result = {"round": number, "experiments": [], "accepted": None, "outcome": "incomplete"}
                    self.report["rounds"].append(round_result)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.config["parallel"], resources()["cpus"])) as pool:
                        futures = [pool.submit(self.experiment, task, number, i) for i, task in enumerate(tasks)]
                        for future in concurrent.futures.as_completed(futures):
                            round_result["experiments"].append(future.result())
                    exhausted = [e for e in round_result["experiments"] if e.get("budget_exhausted")]
                    unavailable = [e["unavailable"] for e in round_result["experiments"] if e.get("unavailable")]
                    resets = [e["retry_at"] for e in unavailable if e["retry_at"] is not None]
                    stop = (BudgetExceeded(exhausted[0]["error"]) if exhausted else ModelUnavailable(
                        min(resets) if resets else None, unavailable[0].get("reason", "provider unavailable")) if unavailable else None)
                    if self.accept(round_result) and stop:
                        round_result["outcome"] = "accepted"
                        self.report["plateau"] = 0
                    if stop:
                        raise stop
                number = round_result["round"]
                failures = [e for e in round_result['experiments'] if e.get('error')]
                if failures and not round_result['accepted']:
                    round_result['outcome'] = 'execution_failed'
                    self.report.update(status='execution_failed', reason=
                        f"{len(failures)} of {len(round_result['experiments'])} attempts failed to execute: "
                        + failures[0]['error'] + '. This is not an optimization plateau.')
                    self.log('round', round=number, outcome='execution_failed', reason=self.report['reason'])
                    break
                round_result['outcome'] = 'accepted' if round_result['accepted'] else 'no_improvement'
                self.report["plateau"] = 0 if round_result["accepted"] else self.report["plateau"] + 1
                self.log("round", round=number, best=self.report["best"], accepted=round_result["accepted"])
                self.checkpoint()
                print(json.dumps({"round": number, "best": self.report["best"], "accepted": round_result["accepted"]}), flush=True)
            else:
                self.report["status"] = "round_limit"
        except ModelProtocolError as exc:
            self.report.update(status='execution_failed', reason=str(exc))
        except ModelUnavailable as exc:
            self.report["status"] = "waiting"
            self.report["reason"] = str(exc)
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
            self.report.setdefault("baseline", {"valid": False, "reason": "baseline not evaluated"})
            self.checkpoint()
        return self.report

    def utility(self, score):
        return score if self.config["direction"] == "max" else -score

    def completed_rounds(self):
        return sum(r.get("outcome") != "incomplete" for r in self.report["rounds"])

    def accept(self, round_result):
        """Merge the best approved improvement among valid, not yet reviewed candidates."""
        valid = [x for x in round_result["experiments"] if x.get("evaluation", {}).get("valid") and "review" not in x]
        valid.sort(key=lambda x: (self.utility(x["evaluation"]["score"]), -x["candidate"]), reverse=True)
        for best in valid:
            if self.utility(best["evaluation"]["score"]) <= self.utility(self.report["best"]) + self.config["min_delta"]:
                break
            try:
                approved = self.review(best)
            except ModelProtocolError as exc:
                best.update(error=str(exc), error_kind=type(exc).__name__)
                round_result['outcome'] = 'execution_failed'
                raise
            if approved:
                git(self.integration, "merge", "--ff-only", best["commit"])
                self.report["best"] = best["evaluation"]["score"]
                round_result["accepted"] = best["commit"]
                return True
        return False

    def plan(self):
        if not any(role in self.config["routes"] for role in ("planner", "architect")):
            return [self.intent] * self.config["parallel"]
        feedback = [{"round": r["round"], "accepted": r["accepted"],
                     "experiments": [{"task": e.get("task"), "evaluation": e.get("evaluation"),
                                      "error": e.get("error"), "review": e.get("review")} for e in r["experiments"]]}
                    for r in self.report["rounds"][-2:]]
        prompt = (prelude("planner") + "Propose independent competing experiments for the same current source baseline. "
                  "They are alternatives, not dependent tasks. Return only JSON: "
                  '{"experiments":[{"task":"specific small improvement and files to inspect"}],"monitoring":[{"label":"intent-specific metric name","source":"best_score","unit":"optional unit"}]}. '
                  'Optionally define up to six dashboard metrics relevant to this intent. Sources: best_score, baseline_score, accepted_count, plateau_count, or metric:NAME for numeric evaluator metrics. Labels describe observations, not promises. '
                  f"At most {self.config['parallel']} experiments. Optimize {self.config['direction']}.\n"
                  + "INTENT:\n" + self.intent + "\nFILES:\n"
                  + json.dumps(Workspace(self.integration, []).files()[:100])
                  + "\nRECENT FEEDBACK:\n" + json.dumps(feedback)[-6500:])
        reply = planning_reply(self.router, self.config, self.intent, self.storage, self.integration, prompt, feedback)
        tasks = reply.get("experiments", [])[:self.config["parallel"]]
        if not tasks or any(not isinstance(t, dict) or not isinstance(t.get("task"), str) or not t["task"] for t in tasks):
            raise ValueError("planner returned invalid experiments")
        self.log("plan", tasks=[t["task"][:2000] for t in tasks], skills=["writing-plans"], monitoring=reply.get("monitoring", []))
        return [self.intent + "\nEXPERIMENT:\n" + t["task"][:2000] for t in tasks]

    def review(self, candidate):
        if "reviewer" not in self.config["routes"]:
            return True
        # No external diff program or textconv: either could show the reviewer other text than the change.
        diff = git(self.integration, "diff", "--no-ext-diff", "--no-textconv", "HEAD", candidate["commit"])
        # Do not silently review a truncated patch.
        prompt = (prelude('reviewer') + 'Review this measured candidate for correctness, evaluator gaming and scope. '
                  'Return JSON {"approve":true,"reason":"..."}.\nINTENT:\n' + self.intent
                  + "\nDIFF:\n" + diff + "\nEVALUATION:\n" + json.dumps(candidate["evaluation"]))
        reply = self.router.ask("reviewer", prompt, self.integration, task=candidate["task"])
        candidate["review"] = reply
        return reply.get("approve") is True

    def shared(self, path, names=None):
        """Watched shared settings, info/attributes and included config files: those named, or all reached now."""
        files = included(path, self.config_file) if names is None else {name: config_bytes(name) for name in names}
        attributes = self.attributes.read_bytes() if self.attributes.is_file() else None
        return watched_config(path, "--local", self.config_file)[:2], attributes, files

    def unsettled(self, path, own, restored, restores):
        """Whether a watched setting changed after restores was read, e.g. while files were checked out."""
        with self.lock:
            self.restore_watched(path, own, restored)
            return self.restores != restores

    def restore_watched(self, path, own, restored):
        """Put back watched settings (shared, and this worktree's config.worktree), included config files and
        info/attributes as recorded; call with self.lock held. Included files get their recorded bytes back;
        otherwise only entries written in the config files themselves are rewritten, never an override for an
        included value. Each change is added to restored with its old and new values; return the changed
        names. A difference that cannot be restored raises after the rest is done, and keeps new runs out."""
        changed, problems = [], []
        text = lambda data: None if data is None else data.decode(errors="replace")[:4000]
        for name, data in self.watched[2].items():  # first: the expanded settings below read these files
            found = config_bytes(name)
            if found == data:
                continue
            changed.append("included:" + name)
            restored.append({"key": "included:" + name, "restored": text(data), "found": text(found)})
            try:
                if data is None:
                    os.unlink(name)
                else:  # in place, keeping its mode; never through a symlink
                    with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644), "wb") as stream:
                        stream.write(data)
            except OSError:
                problems.append((name, f"could not restore {name}; see config_restored"))
        for source, file, recorded, scope in (("--local", self.config_file, self.watched[0], "local"),
                                              (own[0], own[1], own[2], "worktree")):
            where = ["--local"] if source == "--local" else ["--file", str(source)]
            direct, effective, _ = watched_config(path, source, file)
            if (direct, effective) == recorded:
                continue
            for key in dict.fromkeys(k for k, _ in recorded[0] + direct):
                before, after = [v for k, v in recorded[0] if k == key], [v for k, v in direct if k == key]
                if before != after:
                    changed.append(scope + ":" + key)
                    restored.append({"key": scope + ":" + key, "restored": before, "found": after})
                    git(path, "config", *where, "--unset-all", key, ok=(0, 5))
                    for value in before:
                        git(path, "config", *where, "--add", key, value)
            direct, effective, included = watched_config(path, source, file)
            if direct != recorded[0]:
                problems.append((file, f"could not restore the {scope} Git configuration; see config_restored"))
            elif effective != recorded[1]:  # e.g. an include that an unwatched setting turned on
                keys = sorted(k for k in dict.fromkeys(k for k, _ in recorded[1] + effective)
                              if [v for x, v in recorded[1] if x == k] != [v for x, v in effective if x == k])
                files = sorted({f for k in keys for f in included.get(k, [])}) or ["an included file"]
                problems.append((", ".join(files), f"{scope} settings {', '.join(keys)} changed in {', '.join(files)}; "
                                 "repair them there, the runner restores only included files it recorded"))
        attributes = self.attributes.read_bytes() if self.attributes.is_file() else None
        if attributes != self.watched[1]:
            changed.append("info/attributes")
            restored.append({"key": "info/attributes", "restored": text(self.watched[1]), "found": text(attributes)})
            if self.watched[1] is None:
                self.attributes.unlink()
            else:
                self.attributes.parent.mkdir(exist_ok=True)
                self.attributes.write_bytes(self.watched[1])  # in place: keeps its mode
        self.restores += bool(changed)
        if problems:  # a new run must not adopt them as its baseline: Run() refuses to start until repaired
            atomic_json(self.unrestored, {"run": self.run_id, "file": ", ".join(file for file, _ in problems)})
            raise RuntimeError("; ".join(message for _, message in problems))
        return changed

    def experiment(self, task, round_number, index, continuation=None):
        name = f"r{round_number}-c{index}-{uuid.uuid4().hex[:6]}"
        with self.lock:  # a shared change is attributable only to a candidate that ran alone
            sequence, alone = self.started_experiments, self.active_experiments == 0
            self.started_experiments += 1
            self.active_experiments += 1
        own, recorded, restored = None, False, []
        guarded = self.config.get("allow_exec")  # without command execution a worker cannot change config
        path = self.folder / name
        result = {"candidate": index, "task": task}
        preserve = getattr(self, 'retain_candidate', None)
        cleanup = True
        phase = 'worker'
        self.log("activity", phase="writing", candidate=index, task=task[:2500])
        expected = retained = None
        def retain():
            nonlocal cleanup, retained
            # Once the commit is retained for evaluation, the checkout may leave the files half written.
            if preserve and path.exists() and expected and retained != phase:
                cleanup = False  # Keep the worktree if its durable checkpoint fails.
                preserve(path, result, base, expected, name, phase)
                cleanup, retained = True, phase
        try:
            base = git(self.integration, "rev-parse", "HEAD")
            expected = base
            if continuation:
                if not preserve:
                    raise ValueError('candidate continuation requires delivery mode')
                expected = continuation['commit']
            git(self.root, "worktree", "add", "--detach", str(path), expected)
            flagged = index_flags(path)  # e.g. skip-worktree entries a sparse checkout gives every worktree
            nested = nested_git(path)
            own_file = Path(git(path, "rev-parse", "--absolute-git-dir")) / "config.worktree"
            with self.lock:
                if self.watched is None:
                    self.watched = self.shared(path)
                    atomic_json(self.folder / "watched-config.json", dump_watched(self.watched))
                own_real = os.path.realpath(own_file)  # read even if the extension is off
                own = (own_file, own_real, watched_config(path, own_file, own_real)[:2])
                recorded = guarded
            if continuation and fingerprint(path, self.config['protected']) != self.pinned:
                raise ValueError('retained candidate policy changed')
            if not continuation or continuation['phase'] == 'worker':
                result["worker"] = worker(self.router, task, path, self.config, self.log, index)
            # Under the lock no sibling restores or commits, and a watched change that appears before the
            # re-check came from elsewhere while this commit was made.
            with self.lock:
                changed = self.restore_watched(path, own, restored) if guarded else []
                alone = alone and self.started_experiments == sequence + 1
                if fingerprint(path, self.config["protected"]) != self.pinned:
                    raise ValueError("protected files changed")
                if git(path, "rev-parse", "HEAD") != expected:
                    raise ValueError("worker changed Git history")
                staged = bool(git(path, "status", "--porcelain"))
                if staged:
                    git(path, "add", "--all")
                    git(path, "-c", "commit.gpgsign=false", "commit", "-m", f"Experiment {round_number}.{index}")
                foreign = self.restore_watched(path, own, restored) if guarded else []
            # Its own worktree settings, or shared ones it changed alone, are the worker's doing.
            reconfigured = (bool(foreign) and staged or any(key.startswith("worktree:") for key in changed)
                            or bool(changed and alone))
            if not staged and expected == base:
                result["evaluation"] = {"valid": False, "reason": "no changes"}
                if result.get('worker', {}).get('finished') is False:
                    result.update(error='Worker exhausted its step budget without producing a candidate', error_kind='worker_incomplete')
                return result
            result["commit"] = expected = git(path, "rev-parse", "HEAD")
            phase = 'evaluate'
            if preserve:
                retain()  # Persist before evaluation or review can exhaust the epoch.
            self.log("activity", phase="testing", candidate=index)
            # A sibling may have changed watched settings since; compare and check out with the repository's
            # own. Outside the lock (which logging also takes): unsettled() then sees any change meanwhile.
            with self.lock:
                if guarded:
                    self.restore_watched(path, own, restored)
                restores = self.restores
            # The worker's files on disk against its commit, without trusting the index's stat cache.
            drift = differs_from_commit(path, result["commit"], flagged)
            modes = [line.split()[1] for line in git(path, "diff-tree", "-r", base, result["commit"]).splitlines()]
            # A nested repository commits only as a gitlink: its files stay on disk, unrecorded and uncleaned.
            if any(mode == "160000" for mode in modes):
                result["evaluation"] = {"valid": False, "reason": "embedded repositories are not allowed"}
            elif reconfigured:
                result["evaluation"] = {"valid": False, "reason": "worker changed repository configuration"}
            # Index flags the worker set keep its on-disk edits out of the commit.
            elif index_flags(path) != flagged:
                result["evaluation"] = {"valid": False, "reason": "index flags hide worktree changes"}
            elif nested_git(path) != nested:
                result["evaluation"] = {"valid": False, "reason": "nested .git entries hide files from Git"}
            # Git neither records nor cleans files in a submodule directory; fresh worktrees leave it empty.
            elif any(any((path / link).iterdir()) for link in gitlinks(path) if (path / link).is_dir()):
                result["evaluation"] = {"valid": False, "reason": "files inside a submodule directory"}
            elif drift:
                result["evaluation"] = {"valid": False, "reason": "evaluated files differ from the commit"}
            # A checkout would write the new link as a file holding its target, as the worker's file did.
            elif "120000" in modes and git(path, "config", "--type=bool", "core.symlinks", ok=(0, 1)) == "false":
                result["evaluation"] = {"valid": False, "reason": "symlinks cannot be checked out with core.symlinks=false"}
            # Score exactly the commit: from here on the files are a fresh checkout of it, as the baseline's were.
            elif checkout(path, result["commit"], flagged):
                result["evaluation"] = {"valid": False, "reason": "files Git cannot remove"}
            elif guarded and self.unsettled(path, own, restored, restores):
                result["evaluation"] = {"valid": False, "reason": "repository configuration changed during checkout"}
            # The commit's attributes, e.g. eol or an encoding, decide how protected files are written.
            elif fingerprint(path, self.config["protected"]) != self.pinned:
                result["evaluation"] = {"valid": False, "reason": "checkout changed protected files"}
            elif git(path, "status", "--porcelain"):  # e.g. paths that collide on a case-insensitive file system
                result["evaluation"] = {"valid": False, "reason": "checked-out files differ from the commit"}
            else:
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
                result["unavailable"] = {"retry_at": exc.retry_at, "reason": str(exc)}
                result["evaluation"] = {"valid": False, "reason": str(exc)}
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
            result["error_kind"] = type(exc).__name__
            result["evaluation"] = {"valid": False, "reason": type(exc).__name__}
        finally:
            with self.lock:
                self.active_experiments -= 1
                if recorded:  # also after early returns and errors; never mask the original error
                    with contextlib.suppress(RuntimeError):
                        self.restore_watched(path, own, restored)
            if restored:  # logged on every path, including errors, with names only; values stay in the result
                restored = list({change["key"]: change for change in reversed(restored)}.values())[::-1]  # first per key
                result["config_restored"] = redact_value(restored)
                self.log("repository_config_restored", candidate=index, keys=[change["key"] for change in restored])
            if cleanup and path.exists():
                with contextlib.suppress(RuntimeError):
                    git(self.root, "worktree", "remove", "--force", str(path))
        self.log("experiment", **result)
        return result
