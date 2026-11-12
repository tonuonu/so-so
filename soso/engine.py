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
import stat
import threading
import time
import uuid
from .models import Budget, BudgetExceeded, ModelProtocolError, ModelUnavailable, Router, redact_value
from .process import GIT_OUTPUT_LIMIT, OutputLimit, blob_head, execute, git
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


class Unrestorable(RuntimeError):
    """A watched difference that a restore cannot undo; files names where."""
    def __init__(self, files, message):
        super().__init__(message)
        self.files = files


IRREGULAR, UNRECORDED = object(), False  # a path that is no regular file; state an older record lacks


def config_bytes(name):
    """A regular file's bytes, at most one over MAX_CONFIG_BYTES; None when it does not exist. Opened without
    following a final symlink and without blocking, so a FIFO cannot hang the caller: OSError instead."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file: " + os.fspath(name))
        return stream.read(MAX_CONFIG_BYTES + 1)


def current_bytes(name):
    """config_bytes, or IRREGULAR for anything that is no regular file (it then differs from any record)."""
    try:
        return config_bytes(name)
    except OSError:
        return IRREGULAR


def replace_file(name, data):
    """Put data at name through a new file renamed into place, never half written and never through a
    symlink; a regular file there keeps its mode, anything else becomes owner-only."""
    try:
        before = os.lstat(name).st_mode
        mode = stat.S_IMODE(before) if stat.S_ISREG(before) else 0o600
    except FileNotFoundError:
        mode = 0o600
    temp = f"{name}.soso-{uuid.uuid4().hex[:8]}"
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
    try:
        os.chmod(temp, mode)
        os.replace(temp, name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def replace_refs(path):
    """refs/replace/ entries: replace refs let Git show other content than a commit holds."""
    listing = git(path, "for-each-ref", "--format=%(refname) %(objectname)", "refs/replace/")
    return dict(line.split() for line in listing.splitlines())


def outside(root, links):
    """Symlinks among links whose target is absolute or resolves outside the worktree."""
    top = os.path.realpath(root)
    return [link for link in links if os.path.islink(os.path.join(top, link)) and (
        os.path.isabs(os.readlink(os.path.join(top, link)))
        or os.path.commonpath([top, os.path.realpath(os.path.join(top, link))]) != top)]


ATTR_SOURCE = {}  # whether this Git accepts the global --attr-source option, probed once


def supports_attr_source(root, tree):
    """Whether this Git has --attr-source: probed by running it, not inferred from a version number."""
    if "probed" not in ATTR_SOURCE:
        probe = execute(["git", "--attr-source=" + tree, "rev-parse", "--git-dir"], root, timeout=30)
        ATTR_SOURCE["probed"] = probe["returncode"] == 0 and not probe["timed_out"]
    return ATTR_SOURCE["probed"]


def blob_kinds(root, blobs, stop):
    """{blob: (kind, size)} for the blobs that can be read, sizes from one cat-file --batch-check. As Git judges
    it, kind is "empty", "binary" (a NUL byte in the first 8000 bytes) or "text"; "late" once time.monotonic()
    passes stop, when no more blobs are read."""
    kinds = {}
    listing = git(root, "cat-file", "--batch-check", stdin="".join(blob + "\n" for blob in blobs), quiet=True)
    for line in listing.splitlines():
        blob, kind, *size = line.split() + [""]
        if kind == "blob" and size[0].isdigit():
            size = int(size[0])
            if time.monotonic() > stop:
                kinds[blob] = ("late", size)
                continue
            head = blob_head(root, blob, min(size, 8000), whole=size <= 8000) if size else b""
            if head is not None:
                kinds[blob] = ("empty" if not size else "binary" if b"\0" in head else "text", size)
    return kinds


def listed_changes(raw):
    """(old mode, new mode, old blob, new blob, name) from diff --raw -z, with renames and copies."""
    tokens = iter(raw.split("\0"))
    for token in tokens:
        if token.startswith(":"):
            old_mode, new_mode, old, new, status = token[1:].split()[:5]
            name = next(tokens)
            if status[:1] in ("R", "C"):
                name = next(tokens)  # the new name
            yield old_mode, new_mode, old, new, name


def listed_binaries(numstat):
    """For each file of diff --numstat -z, with renames and copies, whether Git shows it as binary."""
    tokens = iter(numstat.split("\0"))
    for token in tokens:
        if token:
            if token.count("\t") >= 2 and not token.split("\t", 2)[2]:  # a rename or copy: two names follow
                next(tokens), next(tokens)
            yield token.startswith("-\t-\t")


def size_text(size):
    return f"{size} B" if size < 1000 else f"{size / 1000:.1f} kB" if size < 1_000_000 else f"{size / 1_000_000:.1f} MB"


def quoted(name):
    """name as Git quotes it in diff headers with core.quotePath=true: a name with a control character, a
    double quote, a backslash or a non-ASCII byte is put in double quotes with C escapes, so it cannot break
    a line of what the reviewer reads."""
    data = name.encode()
    if all(32 <= byte < 127 and byte not in b'"\\' for byte in data):
        return name
    escapes = {7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r", 34: '"', 92: "\\"}
    return '"' + "".join("\\" + escapes[byte] if byte in escapes else chr(byte) if 32 <= byte < 127
                         else f"\\{byte:03o}" for byte in data) + '"'


def review_diff(root, base, commit, seconds=300):
    """(diff, None) for a reviewer, or (None, reason) when the change cannot be shown in full.

    No external diff program or textconv, and, where Git accepts --attr-source, attributes from the base
    commit, so a -diff, binary or diff driver the candidate commits cannot hide its change, while the
    user's own attributes still apply. Each file Git still shows only as binary gets a line with both sides'
    kinds and sizes, read by blob id (a gitlink side counts as absent), and its text sides in full: both
    sides as a text diff when neither is binary, otherwise the one text side alone (its old text removed or
    its new text added). A binary side is never shown as text. So attributes, an older Git, or a NUL byte
    added or removed cannot hide text: the reviewer sees all of it, and a change to binary is flagged.
    Withheld: text over 2 MB (UTF-8 bytes of what the reviewer would receive), a side that cannot be read,
    or binary-shown files beyond the seconds allowed."""
    # Attributes as the base commit holds them: the user's own (e.g. "diff" forcing text) apply, while
    # attributes the candidate commits do not. Without --attr-source Git reads them from root's worktree,
    # which both reviewers keep at the base. Ids are used as arguments: no stderr in them.
    base = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}", quiet=True)
    options = ["--attr-source=" + base] if supports_attr_source(root, base) else []
    too_large = f"the diff exceeds {GIT_OUTPUT_LIMIT // 1_000_000} MB; split the change into smaller ones"
    stop = time.monotonic() + seconds
    try:
        diff = git(root, *options, "diff", "--no-ext-diff", "--no-textconv", base, commit)
        # The same files, paired by the same rename and copy detection, in the same order as the diff above.
        changes = list(listed_changes(git(root, *options, "diff", "--raw", "-z", "--no-abbrev", base, commit, quiet=True)))
        binaries = list(listed_binaries(git(root, *options, "diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv",
                                            base, commit, quiet=True)))
        if len(changes) != len(binaries):
            return None, "could not match the diff's files to show them"
        # A gitlink side names a commit of another repository: as absent as a missing side.
        shown = [[(mode, blob if blob.strip("0") and mode != "160000" else None) for mode, blob in
                  ((old_mode, old), (new_mode, new))] + [name]
                 for (old_mode, new_mode, old, new, name), binary in zip(changes, binaries) if binary]
        kinds = blob_kinds(root, sorted({blob for *sides, _ in shown for _, blob in sides if blob}), stop) if shown else {}
        nothing = git(root, "hash-object", "-w", "-t", "blob", "--stdin", stdin="", quiet=True) if shown else None
        sent = len(diff.encode())  # what the reviewer receives, in bytes
        for *sides, name in shown:
            found = [("absent", 0) if blob is None else kinds.get(blob) for _, blob in sides]
            if time.monotonic() > stop or any(kind == "late" for kind, _ in filter(None, found)):
                return None, "could not show every binary-shown file in time; split the change"
            if None in found:
                return None, f"could not read {quoted(name)} to show it"
            (old_kind, old_size), (new_kind, new_size) = found
            note = f"\n({quoted(name)}: {old_kind} {size_text(old_size)} \u2192 {new_kind} {size_text(new_size)}"
            if old_kind == "text" and new_kind == "binary":
                note += "; this change adds NUL bytes; later edits to this file will show only as binary"
            note += ")"
            # Text sides only: an empty, absent or binary side is diffed as the empty blob.
            text = [blob if kind == "text" else nothing for (_, blob), (kind, _) in zip(sides, found)]
            if text != [nothing, nothing]:
                note += ("\n(Git shows it as binary; the change as text:)\n" if "binary" not in (old_kind, new_kind)
                         else "\n(Git shows it as binary; its text side as text, the binary side not shown:)\n")
                note += git(root, "diff", "--text", "--no-ext-diff", "--no-textconv", *text)
            sent += len(note.encode())
            if sent > GIT_OUTPUT_LIMIT:
                return None, too_large
            diff += note
    except OutputLimit:
        return None, too_large
    return diff, None


def included(path, config):
    """Bytes (None if missing) of every file that config reaches through include.path or includeIf.*.path,
    transitively and whatever the condition: a worker can edit them, but key-level restore cannot. Files that
    are no regular file, too large or too many are left out, and never stop a run."""
    found, pending = {}, [os.fspath(config)]
    while pending:
        source = pending.pop()
        try:
            listing = git(path, "config", "--file", source, "--list", "-z") if os.path.isfile(source) else ""
        except RuntimeError:
            continue  # e.g. a file a never-taken includeIf names; Git would fail on it wherever it applies
        for entry in listing.split("\0"):
            key, _, value = entry.partition("\n")
            if not INCLUDE.fullmatch(key) or not value:
                continue
            named = os.path.abspath(os.path.join(os.path.dirname(source), os.path.expanduser(value)))
            name = os.path.realpath(named)  # e.g. a dotfiles symlink: the file itself is recorded and restored
            data = None if name in found else current_bytes(name)  # e.g. /dev/null: nothing to restore
            if data is IRREGULAR or len(data or b"") > MAX_CONFIG_BYTES or len(found) >= MAX_INCLUDED:
                continue  # not recorded: a change it brings in cannot be restored, and fails closed instead
            if name not in found:
                found[name] = data
                pending.append(named)
    return found


def dump_watched(watched):
    encode = lambda data: data if data is None or data is UNRECORDED else base64.b64encode(data).decode()
    return {"local": watched[0], "attributes": encode(watched[1]),
            "included": {name: encode(data) for name, data in watched[2].items()},
            "exclude": encode(watched[3]), "replace": watched[4]}


def load_watched(saved):
    """A record as dump_watched wrote it; what an older record lacks is UNRECORDED (included: none) and left alone."""
    decode = lambda text: text if text is None or text is UNRECORDED else base64.b64decode(text)
    return (tuple([tuple(entry) for entry in entries] for entries in saved["local"]), decode(saved["attributes"]),
            {name: decode(text) for name, text in saved.get("included", {}).items()},
            decode(saved["exclude"]) if "exclude" in saved else UNRECORDED, saved.get("replace", UNRECORDED))


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


def atomic_json(path, value, private=False):
    temp = path.with_suffix(".tmp")  # private: owner-only, e.g. a record holding the bytes of config files
    with open(temp, "w", opener=lambda name, flags: os.open(name, flags, 0o600 if private else 0o666)) as stream:
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
        self.attributes, self.exclude = (Path(os.path.realpath(common / "info" / name)) for name in ("attributes", "exclude"))
        self.watched = None
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
            keep = f"delete {self.unrestored} to keep the current ones, before starting a new run"
            try:
                failed = json.loads(self.unrestored.read_text())
                record = self.storage / failed["run"] / "watched-config.json"
                recorded = load_watched(json.loads(record.read_text()))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise ValueError(f"a run could not restore repository settings and its record is gone; check them and "
                                 + keep) from None
            try:
                repaired = self.repaired(recorded)
            except (RuntimeError, OSError):
                repaired = False  # what cannot be read cannot be shown to match
            if not repaired:
                raise ValueError(f"run {failed['run']} could not restore {failed['file']}; put back the settings recorded "
                                 f"in {record}, or " + keep)
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
        diff, withheld = review_diff(self.integration, "HEAD", candidate["commit"], min(300, self.budget.remaining()))
        if withheld:  # never ask a model to approve a change it cannot see
            candidate["review"] = {"approve": False, "reason": withheld}
            return False
        # Do not silently review a truncated patch.
        prompt = (prelude('reviewer') + 'Review this measured candidate for correctness, evaluator gaming and scope. '
                  'Return JSON {"approve":true,"reason":"..."}.\nINTENT:\n' + self.intent
                  + "\nDIFF:\n" + diff + "\nEVALUATION:\n" + json.dumps(candidate["evaluation"]))
        reply = self.router.ask("reviewer", prompt, self.integration, task=candidate["task"])
        candidate["review"] = reply
        return reply.get("approve") is True

    def shared(self, path, files=True):
        """Watched shared state as it is now: settings, info/attributes, included config files (with files),
        info/exclude and replace refs."""
        def info(name):  # anything but a regular file (e.g. /dev/null) is left alone: UNRECORDED
            data = current_bytes(name)
            if data is not IRREGULAR and len(data or b"") > MAX_CONFIG_BYTES:
                raise ValueError(f"{name} is too large to record")
            return UNRECORDED if data is IRREGULAR else data
        return (watched_config(path, "--local", self.config_file)[:2], info(self.attributes),
                included(path, self.config_file) if files else {}, info(self.exclude), replace_refs(path))

    def repaired(self, recorded):
        """Whether the shared state matches recorded (included files aside: their watched values are part of the
        settings), read in a detached worktree as candidates are, so includeIf onbranch and gitdir conditions
        evaluate as they did when it was recorded."""
        probe = self.storage / ("probe-" + uuid.uuid4().hex[:8])
        git(self.root, "worktree", "add", "--detach", "--no-checkout", str(probe), "HEAD")
        try:
            now = self.shared(probe, files=False)
        finally:
            git(self.root, "worktree", "remove", "--force", str(probe))
        return all(old is UNRECORDED or old == new for i, (old, new) in enumerate(zip(recorded, now)) if i != 2)

    def unsettled(self, path, own, restored, restores):
        """Whether a watched setting changed after restores was read, e.g. while files were checked out."""
        with self.lock:
            self.restore_watched(path, own, restored)
            return self.restores != restores

    def restore_watched(self, path, own, restored):
        """restore_changes; any failure, a difference it cannot restore or an error on the way, also writes
        soso/unrestored.json, so that no new run adopts the state as its baseline. Call with self.lock held."""
        try:
            return self.restore_changes(path, own, restored)
        except (RuntimeError, OSError) as exc:
            files = exc.files if isinstance(exc, Unrestorable) else str(self.config_file)
            atomic_json(self.unrestored, {"run": self.run_id, "file": files})
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"could not restore {files}: {exc}") from exc

    def restore_changes(self, path, own, restored):
        """Put back watched settings (shared, and this worktree's config.worktree), included config files and
        info/attributes as recorded; call with self.lock held. Included files get their recorded bytes back;
        otherwise only entries written in the config files themselves are rewritten, never an override for an
        included value. Each change is added to restored with its old and new values; return the changed
        names. A difference that cannot be restored raises Unrestorable after the rest is done. Whatever a
        restore replaces is first kept, owner-only, under replaced/ in the run folder and named as "kept"."""
        changed, problems = [], []
        text = lambda data: None if data is None else "(not a regular file)" if data is IRREGULAR else data.decode(
            errors="replace")[:4000]

        def put(name, data):  # the recorded bytes, or no file, in place of whatever is there now; returns what was kept
            kept = None
            try:
                if os.path.lexists(name):  # e.g. the user's unrelated edits: recoverable from the run folder
                    kept = self.folder / "replaced" / f"{uuid.uuid4().hex[:8]}-{os.path.basename(name)}"
                    kept.parent.mkdir(mode=0o700, exist_ok=True)
                    if os.path.isdir(name) and not os.path.islink(name):
                        shutil.move(name, kept)  # a folder in place of the file: moved aside whole
                    elif isinstance(found := current_bytes(name), bytes):
                        replace_file(kept, found)
                    else:
                        kept = None  # a FIFO, link or device: nothing to keep
                if data is None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(name)
                else:
                    replace_file(name, data)
            except OSError:
                problems.append((name, f"could not restore {name}; see config_restored"))
            return kept and str(kept)

        def included_files(found, before, after):
            for name, data in self.watched[2].items():
                if found(name):
                    changed.append("included:" + name)
                    restored.append({"key": "included:" + name, "restored": before, "found": after})
                    restored[-1]["kept"] = put(name, data)

        # A FIFO or other non-file would block Git or the reads below: the recorded file goes back first.
        included_files(lambda name: current_bytes(name) is IRREGULAR, "a regular file", "not a regular file")
        for source, file, recorded, scope in (("--local", self.config_file, self.watched[0], "local"),
                                              (own[0], own[1], own[2], "worktree")):
            where = ["--local"] if source == "--local" else ["--file", str(source)]
            direct, effective, _ = watched_config(path, source, file)
            if (direct, effective) == recorded:
                continue
            supplied = lambda entries, own_entries: [entry for entry in entries if entry not in own_entries]
            if scope == "local" and supplied(effective, direct) != supplied(recorded[1], recorded[0]):
                # Watched values an included file supplies changed: its recorded bytes go back. Other edits to
                # it are kept, and only the watched values are logged, never the file's other content.
                keys = {k for k, _ in recorded[1] + effective if [v for x, v in recorded[1] if x == k]
                        != [v for x, v in effective if x == k]}
                included_files(lambda name: current_bytes(name) != self.watched[2][name],
                               [f"{k}={v}" for k, v in recorded[1] if k in keys], [f"{k}={v}" for k, v in effective if k in keys])
                direct, effective, _ = watched_config(path, source, file)
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
        for label, name, data in (("info/attributes", self.attributes, self.watched[1]),
                                  ("info/exclude", self.exclude, self.watched[3])):
            found = current_bytes(name)
            if data is UNRECORDED or found == data:
                continue
            changed.append(label)
            restored.append({"key": label, "restored": text(data), "found": text(found)})
            name.parent.mkdir(exist_ok=True)
            restored[-1]["kept"] = put(name, data)
        if self.watched[4] is not UNRECORDED:  # replace refs: the runner ignores them, the user's own Git does not
            refs = replace_refs(path)
            for ref in dict.fromkeys([*self.watched[4], *refs]):
                before, after = self.watched[4].get(ref), refs.get(ref)
                if before != after:
                    changed.append(ref)
                    restored.append({"key": ref, "restored": before, "found": after})
                    git(path, "update-ref", *(["-d", ref] if before is None else [ref, before]))
        self.restores += bool(changed)
        if problems:
            raise Unrestorable(", ".join(file for file, _ in problems), "; ".join(message for _, message in problems))
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
                    atomic_json(self.folder / "watched-config.json", dump_watched(self.watched), private=True)
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
            changes = git(path, "diff-tree", "-r", "-z", base, result["commit"]).split("\0")
            changes = [(meta.split()[1], name) for meta, name in zip(changes[0::2], changes[1::2])]
            modes, links = [mode for mode, _ in changes], [name for mode, name in changes if mode == "120000"]
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
            # A link out of the worktree scores files that are not in the commit, and breaks on a clone.
            elif outside(path, links):
                result["evaluation"] = {"valid": False, "reason": "symlinks must stay inside the worktree"}
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
