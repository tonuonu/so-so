import contextlib
import http.server
import json
import os
import signal
import sys
import threading
import time
import unittest

from soso.models import Budget, BudgetExceeded, Router
from soso.process import execute


class ProcessDeadlineTests(unittest.TestCase):
    def test_detached_child_cannot_hold_output_pipe_open(self):
        code = ("import subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(10)'],start_new_session=True); "
                "print(p.pid,flush=True)")
        result = execute([sys.executable, "-c", code], ".", timeout=2)
        pid = int(result["output"].strip())
        try:
            self.assertEqual(result["returncode"], 0)
            self.assertFalse(result["timed_out"])
            self.assertLess(result["seconds"], 2.5)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def test_large_input_and_output_do_not_deadlock(self):
        code = ("import sys; sys.stdout.write('x'*2000000);sys.stdout.flush();"
                "data=sys.stdin.read();print(len(data))")
        result = execute([sys.executable, "-c", code], ".", timeout=4,
                         stdin="y" * 2000000, limit=64)
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["timed_out"])
        self.assertTrue(result["truncated"])
        self.assertTrue(result["output"].endswith("2000000\n"))

    def test_child_that_never_reads_input_obeys_deadline(self):
        result = execute([sys.executable, "-c", "import time;time.sleep(10)"], ".",
                         timeout=0.3, stdin="y" * 2000000)
        self.assertTrue(result["timed_out"])
        self.assertLess(result["seconds"], 1.5)


class HTTPDeadlineTests(unittest.TestCase):
    def setUp(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.dumps({"message": {"content": '{"ok":true}'}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    for offset in range(0, len(payload), 2):
                        self.wfile.write(payload[offset:offset + 2])
                        self.wfile.flush()
                        time.sleep(0.08)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.05), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def router(self, seconds, timeout):
        config = {"models": {"slow": {"kind": "ollama", "model": "test",
                  "url": f"http://127.0.0.1:{self.server.server_port}", "timeout": timeout}},
                  "routes": {"worker": ["slow"]}}
        return Router(config, Budget(2, seconds), lambda *args, **kwargs: None)

    def test_continuous_response_cannot_extend_global_deadline(self):
        router = self.router(seconds=.5, timeout=5)
        started = time.monotonic()
        with self.assertRaises(BudgetExceeded):
            router.ask("worker", "test", ".")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_continuous_response_cannot_extend_model_timeout(self):
        router = self.router(seconds=5, timeout=.5)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "all routes failed"):
            router.ask("worker", "test", ".")
        self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()

class ObservedDeadlineTests(unittest.TestCase):
    def test_success_after_scheduling_delay_is_timeout(self):
        from unittest.mock import patch
        from soso import process
        real = process.selectors.DefaultSelector
        class DelayedSelector:
            def __init__(self): self.selector=real()
            def register(self,*args): return self.selector.register(*args)
            def unregister(self,*args): return self.selector.unregister(*args)
            def close(self): self.selector.close()
            def select(self,timeout):
                time.sleep(.2)
                return self.selector.select(0)
        with patch.object(process.selectors,'DefaultSelector',DelayedSelector):
            result=process.execute([sys.executable,'-c','print("done")'],'.',timeout=.1)
        self.assertTrue(result['timed_out'])
