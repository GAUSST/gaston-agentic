"""Regression tests for UTF-8 subprocess output and resilient stream readers."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import eng


class RunProcessEncodingTests(unittest.TestCase):
    def _run_ai_child(self, code, stdin=None):
        with tempfile.TemporaryDirectory(prefix="gaston-utf8-") as td:
            root = Path(td)
            output = root / "worker.log"
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            with (
                patch.object(eng, "project_state_name", return_value="project"),
                patch.object(eng.SESSIONS, "record_child"),
            ):
                result = eng.run_process(
                    [sys.executable, "-c", code],
                    output,
                    stdin=stdin,
                    timeout=10,
                    cwd=root,
                    ai=True,
                    env=env,
                )
            log = output.read_text(encoding="utf-8")
            return result, log

    def test_ai_stdout_and_stderr_are_decoded_as_utf8(self):
        stdout = "Codex: \u009d — café — 東京 — 😀"
        stderr = "diagnostic: naïve façade — 😀"
        code = (
            "import sys; "
            f"sys.stdout.buffer.write({(stdout + chr(10)).encode('utf-8')!r}); "
            f"sys.stderr.buffer.write({(stderr + chr(10)).encode('utf-8')!r}); "
            "sys.stdout.flush(); sys.stderr.flush()"
        )

        result, log = self._run_ai_child(code)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, stdout + "\n")
        self.assertEqual(result.stderr, stderr + "\n")
        self.assertIn(stdout, log)
        self.assertIn("[stderr] " + stderr, log)

    def test_ai_stdin_is_encoded_as_utf8(self):
        prompt = "Prompt: \\u009d — café — 東京 — 😀"
        code = (
            "import sys; "
            "payload = sys.stdin.read(); "
            "sys.stdout.buffer.write(payload.encode('utf-8')); "
            "sys.stdout.flush()"
        )

        result, _ = self._run_ai_child(code, stdin=prompt)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, prompt)

    def test_invalid_utf8_byte_is_replaced_instead_of_killing_reader(self):
        invalid_bytes = b"bad:\x9d\n"
        code = "import sys; sys.stdout.buffer.write(" + repr(invalid_bytes) + "); sys.stdout.flush()"
        result, _ = self._run_ai_child(code)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "bad:\ufffd\n")

    def test_unexpected_reader_error_stops_child_and_is_reported(self):
        class BrokenStream:
            def __iter__(self):
                raise OSError("simulated pipe read failure")

            def close(self):
                pass

        class EmptyStream:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class FakeProcess:
            pid = 123
            stdin = None
            stdout = BrokenStream()
            stderr = EmptyStream()
            returncode = 0

            def wait(self, timeout=None):
                return self.returncode

        process = FakeProcess()

        def stop_child(child, **kwargs):
            child.returncode = -9

        with tempfile.TemporaryDirectory(prefix="gaston-reader-") as td:
            root = Path(td)
            output = root / "worker.log"
            with (
                patch.object(eng, "project_state_name", return_value="project"),
                patch.object(eng.subprocess, "Popen", return_value=process),
                patch("task_runtime.stop_owned_process", side_effect=stop_child) as stop,
            ):
                result = eng.run_process(
                    ["fake-provider"],
                    output,
                    timeout=5,
                    cwd=root,
                    ai=False,
                )

            self.assertEqual(result.returncode, -9)
            self.assertIn("STREAM READ ERROR", result.stderr)
            self.assertIn("simulated pipe read failure", result.stderr)
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
