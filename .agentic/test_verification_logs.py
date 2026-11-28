"""Private full-log persistence regression for compact verification responses."""
import os
from pathlib import Path
import stat
import tempfile
import unittest

from platform_compat import persist_verification_log


class VerificationLogPersistenceTests(unittest.TestCase):
    def test_complete_stdout_and_stderr_are_written_to_private_local_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'.agentic/local/verification-logs'
            path=persist_verification_log(directory,'full stdout output','full stderr output')
            self.assertIsNotNone(path)
            log=Path(path)
            self.assertTrue(log.is_file())
            text=log.read_text(encoding='utf8')
            self.assertIn('STDOUT\nfull stdout output',text)
            self.assertIn('STDERR\nfull stderr output',text)
            if os.name!='nt':
                self.assertEqual(stat.S_IMODE(log.stat().st_mode),0o600)
                self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode),0o700)

    def test_missing_log_directory_is_created_without_exposing_output_in_return_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=persist_verification_log(Path(tmp)/'nested/logs','secret-like output','diagnostic')
            self.assertTrue(Path(path).exists())
            self.assertNotIn('secret-like output',path)


if __name__=='__main__':unittest.main()
