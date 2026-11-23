"""Codex command-shaped patch regression, with real managed task records."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_hooks as hooks
import orchestration as orch


class PatchGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.folder = self.root / '.agentic/runs/patch-guard'
        self.scope = ['backend/src/A.java', 'backend/src/B.java']
        self.attempt = 'c' * 32
        orch.put(self.folder / 'task.json', {'owner': 'builder', 'write_scope': self.scope})
        orch.put(self.folder / 'worker.json', {'attempt': self.attempt})
        orch.put(self.folder / 'state.json', {'stage': 'implementing'})
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(hooks, 'ROOT', self.root))
        stack.enter_context(patch.object(hooks, 'registered_role', return_value='builder'))
        stack.enter_context(patch.dict(os.environ, {'GASTON_BUILDER_TASK_ID': self.folder.name, 'GASTON_BUILDER_ATTEMPT': self.attempt}))
        self.errors = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def send(self, payload):
        return hooks.receive('codex', {'hook_event_name': 'PreToolUse', 'session_id': 'test-managed-builder', 'cwd': str(self.root), 'tool_name': 'apply_patch', 'tool_input': payload})

    def patch_text(self, *lines):
        return '\n'.join(('*** Begin Patch', *lines, '*** End Patch'))

    def blocked(self, payload, reason=None):
        self.errors.seek(0); self.errors.truncate(0)
        with self.assertRaises(SystemExit) as caught:
            self.send(payload)
        self.assertEqual(caught.exception.code, 2)
        if reason:
            self.assertIn(reason, self.errors.getvalue())

    def test_codex_command_payload_is_permitted_for_scoped_edits(self):
        text = self.patch_text('*** Update File: backend/src/A.java', '@@', '-old', '+new', '*** Add File: backend/src/B.java', '+class B {}')
        self.assertEqual(self.send({'command': text}), 'ignored-event')

    def test_legacy_payloads_remain_permitted(self):
        text = self.patch_text('*** Delete File: backend/src/A.java')
        for payload in (text, {'patch': text}, {'input': text}, {'command': text, 'patch': text}):
            with self.subTest(payload=payload):
                self.assertEqual(self.send(payload), 'ignored-event')

    def test_multi_file_patch_cannot_hide_out_of_scope_write(self):
        self.blocked({'command': self.patch_text('*** Add File: backend/src/B.java', '+ok', '*** Update File: backend/pom.xml', '@@', '-a', '+b')}, 'Outside approved task scope')

    def test_move_checks_both_source_and_destination(self):
        self.assertEqual(self.send({'command': self.patch_text('*** Update File: backend/src/A.java', '*** Move to: backend/src/B.java', '@@', '-a', '+b')}), 'ignored-event')
        for source, destination in [('backend/src/A.java','backend/pom.xml'),('backend/pom.xml','backend/src/A.java')]:
            with self.subTest(source=source, destination=destination):
                self.blocked({'command': self.patch_text('*** Update File: '+source, '*** Move to: '+destination, '@@', '-a', '+b')}, 'Outside approved task scope')

    def test_invalid_and_conflicting_payloads_fail_closed(self):
        text = self.patch_text('*** Delete File: backend/src/A.java')
        for payload in ({'command': None}, {'command': ['apply_patch', text]}, {'command': ''}, {'command': 'echo hello'}, {'command': text, 'patch': self.patch_text('*** Delete File: backend/pom.xml')}, {'patch': text, 'command': None}):
            with self.subTest(payload=payload):
                self.blocked(payload, 'Unrecognized direct edit input')

    def test_protected_and_external_paths_remain_blocked(self):
        for name in ('.agentic/native_hooks.py', '../outside.txt', str(self.root.parent/'outside.txt')):
            with self.subTest(name=name):
                self.blocked({'command': self.patch_text('*** Delete File: '+name)})

    def test_crlf_transport_and_absolute_workspace_path(self):
        text = self.patch_text('*** Update File: '+str(self.root/self.scope[0]), '@@', '-a', '+b').replace('\n', '\r\n')
        self.assertEqual(self.send({'command': text}), 'ignored-event')

    def test_no_active_scope_still_denied(self):
        orch.put(self.folder/'state.json', {'stage':'awaiting_lead_review'})
        self.blocked({'command': self.patch_text('*** Delete File: backend/src/A.java')}, 'no active delegated write scope')


if __name__ == '__main__':
    unittest.main()
