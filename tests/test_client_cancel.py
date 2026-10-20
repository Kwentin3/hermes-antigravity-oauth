"""Real OS request cancellation; no network, token, or mocked termination."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from client import AntigravityClient
from process import _own_process_group
from stream import AntigravityStream


WORKER = """
import json,pathlib,sys,time
print(json.dumps({'event':'init','conversation_id':'synthetic-cancel'}),flush=True)
line=sys.stdin.readline()
if sys.argv[2:]==['--oneshot']:assert line
else:assert json.loads(line)['event']=='user'
pathlib.Path(sys.argv[1]).write_text('ready')
while True:time.sleep(.1)
"""


class NativeClientCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-cancel-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        auth = root / "fake-auth"
        auth.mkdir()
        # An explicit missing token override never searches real credentials.
        self.env = patch.dict(os.environ, {"ANTIGRAVITY_CONFIG_DIR": str(auth),
            "ANTIGRAVITY_WORKSPACE_RULES": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.worker = root / "worker.py"
        self.worker.write_text(WORKER, encoding="utf-8")
        self.ready = root / "ready"

    def _consume(self, stream):
        errors = []
        def read():
            try:
                next(stream)
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=read, name="test-agy-consumer", daemon=True)
        thread.start()
        return thread, errors

    def _ready(self):
        deadline = time.monotonic() + 5
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.ready.exists(), "Synthetic OS worker did not receive its request")

    def _assert_cross_thread_cancel(self, client, stream, proc):
        consumer, errors = self._consume(stream)
        self._ready()
        self.assertIsNone(proc.poll())
        workspace = Path(client._temp_dir.name)
        failures = []
        def cancel():
            try:
                client.cancel()
            except Exception as exc:
                failures.append(exc)
        interrupter = threading.Thread(target=cancel, daemon=True)
        interrupter.start()
        interrupter.join(timeout=5)
        self.assertFalse(interrupter.is_alive(), "Cancel deadlocked on a blocked reader")
        self.assertFalse(failures, repr(failures))
        consumer.join(timeout=5)
        self.assertFalse(consumer.is_alive(), "Native request did not unwind after cancel")
        self.assertIsNotNone(proc.poll(), "Owned worker survived cancellation")
        self.assertTrue(client.is_closed, "Cancelled request client must not be reused")
        self.assertTrue(workspace.is_dir(), "Cross-thread cancel removed the owner workspace")
        self.assertFalse(proc.stdout.closed, "Cross-thread cancel closed the owner's pipe")
        self.assertTrue(errors, "Interrupted synthetic turn must not fabricate a completion")
        self.assertIsNotNone(stream._reader_thread)
        self.assertFalse(stream._reader_thread.is_alive(), "Provider reader survived cancellation")
        client.cancel()  # Idempotent; no replacement process or workspace cleanup.
        self.assertTrue(workspace.is_dir())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            client._get_or_spawn_worker("gemini-4-argon-low", None)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            client._run_oneshot_completion(model="", effort=None, messages=[],
                timeout=30, tools=[], tool_choice=None, stream=True)
        self.assertIsNone(client._worker_proc)
        self.assertFalse(client._active_processes)
        client.close()  # The owning request performs full teardown.
        self.assertFalse(workspace.exists())

    def test_cancel_persistent_worker_unblocks_reader_without_cross_thread_close(self):
        client = AntigravityClient(command=sys.executable)
        self.addCleanup(client.close)
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(self.worker), str(self.ready)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, **_own_process_group())
        self.addCleanup(self._dispose, proc)
        # Register an actual already-started persistent request, exactly as the
        # native spawn seam does. The endpoint itself needs no AGY executable.
        with client._lock:
            client._worker_proc = proc
            client._worker_model = "gemini-4-argon-low"
            client._active_processes.add(proc)
        client._worker_lock.acquire()
        proc.stdin.write(json.dumps({"event": "user"}) + "\n")
        proc.stdin.flush()
        stream = AntigravityStream(proc=proc, client=client, model="gemini-4-argon-low",
            timeout=30, tools=[], is_worker=True, worker_lock=client._worker_lock,
            worker_lock_held=True, messages=[], usage_baseline={})
        self.addCleanup(stream.close)
        self._assert_cross_thread_cancel(client, stream, proc)

    def test_cancel_real_oneshot_request_preserves_workspace_for_owner_close(self):
        client = AntigravityClient(command=sys.executable,
            args=["-I", "-S", "-B", str(self.worker), str(self.ready), "--oneshot"])
        self.addCleanup(client.close)
        stream = client._run_oneshot_completion(model="", effort=None, messages=[],
            timeout=30, tools=[], tool_choice=None, stream=True)
        self.addCleanup(self._dispose, stream.proc)
        self.addCleanup(stream.close)
        self._assert_cross_thread_cancel(client, stream, stream.proc)

    @staticmethod
    def _dispose(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


if __name__ == "__main__":
    unittest.main()
