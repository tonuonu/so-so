"""Bounded subprocesses; POSIX process groups also cover spawned children."""
import os
import selectors
import signal
import subprocess
import time


def execute(argv, cwd, timeout=60, stdin=None, limit=16000, env=None):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("command must be a nonempty array of strings")
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env, bufsize=0)
    chunks = bytearray()
    total = 0
    pending = memoryview((stdin or "").encode())
    deadline = started + max(0.01, timeout)
    selector = selectors.DefaultSelector()
    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stdin.fileno(), False)
    selector.register(proc.stdout, selectors.EVENT_READ)
    if pending:
        selector.register(proc.stdin, selectors.EVENT_WRITE)
    else:
        proc.stdin.close()

    def retain(chunk):
        nonlocal total
        total += len(chunk)
        chunks.extend(chunk)
        if len(chunks) > limit:
            del chunks[:-limit]

    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = proc.poll() is None
                break
            for key, _ in selector.select(min(remaining, 0.05)):
                if key.fileobj is proc.stdout:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if chunk:
                        retain(chunk)
                    else:
                        selector.unregister(proc.stdout)
                else:
                    try:
                        pending = pending[os.write(proc.stdin.fileno(), pending[:65536]):]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
            if proc.poll() is not None:
                break
    finally:
        # Kill descendants even when their parent exited before them.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        # Read only bytes already available. A daemon may have detached from the
        # group and retained its pipe, so waiting for EOF would be unbounded.
        for _ in range(64):
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            retain(chunk)
        selector.close()
        proc.stdout.close()
        proc.stdin.close()
    return {"returncode": proc.returncode, "output": chunks.decode(errors="replace"),
            "truncated": total > limit, "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3)}


def git(root, *args):
    result = execute(["git", "-c", "core.hooksPath=/dev/null", *args], root, timeout=60,
                     limit=2_000_000)
    if result["returncode"] or result["timed_out"]:
        raise RuntimeError("git " + args[0] + ": " + result["output"][-4000:])
    if result["truncated"]:
        raise RuntimeError("Git output exceeds limit")
    return result["output"].strip()
