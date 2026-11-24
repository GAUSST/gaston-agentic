"""Regression: a background shell is not a Builder completion handoff."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_hooks
import wait_guard as guard
from project_layout import project_state


class WaitGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.folder = project_state(self.root) / 'runs' / 'task-12345678'
        self.folder.mkdir(parents=True)
        self.lease = 'lead-lease'
        self.attempt = 'a' * 32
        self.launch(self.attempt)

    def launch(self, attempt):
        native_hooks.atomic_json(self.folder / 'worker-launch.json', {'attempt': attempt})
        native_hooks.atomic_json(self.folder / 'worker.json', {'attempt': attempt, 'pid': 12345})
        native_hooks.atomic_json(self.folder / 'state.json', {'stage': 'repairing'})
        guard.begin(self.folder, attempt, self.lease)

    def test_stop_after_builder_done_without_wait_is_blocked_once(self):
        native_hooks.atomic_json(self.folder / 'state.json', {'stage': 'awaiting_lead_review'})
        self.assertIn(self.folder.name, guard.stop_reason(self.root, self.lease))
        self.assertIsNone(guard.stop_reason(self.root, self.lease))

    def test_active_native_subscription_is_not_interrupted(self):
        guard.attach(self.folder, self.lease)
        with patch.object(guard, 'process_state', return_value='live'):
            self.assertIsNone(guard.stop_reason(self.root, self.lease))

    def test_dead_subscription_can_be_recovered(self):
        guard.attach(self.folder, self.lease)
        with patch.object(guard, 'process_state', return_value='dead'):
            self.assertIn('wait_for_builder', guard.stop_reason(self.root, self.lease))

    def test_real_completion_clears_obligation(self):
        attempt = guard.attach(self.folder, self.lease)
        guard.finish(self.folder, self.lease, attempt, {'status': 'ready', 'builder_finished': True})
        self.assertIsNone(guard.stop_reason(self.root, self.lease))

    def test_cancelled_wait_is_not_completion(self):
        attempt = guard.attach(self.folder, self.lease)
        guard.finish(self.folder, self.lease, attempt, {'status': 'ready', 'builder_finished': True}, cancelled=True)
        self.assertIn('wait_for_builder', guard.stop_reason(self.root, self.lease))

    def test_actual_blocker_is_reportable_without_loop(self):
        attempt = guard.attach(self.folder, self.lease)
        guard.finish(self.folder, self.lease, attempt, {'status': 'blocked', 'builder_finished': False})
        self.assertIsNone(guard.stop_reason(self.root, self.lease))

    def test_new_correction_cannot_reuse_old_wait(self):
        old = guard.attach(self.folder, self.lease)
        self.launch('b' * 32)
        guard.finish(self.folder, self.lease, old, {'status': 'ready', 'builder_finished': True})
        self.assertIn('wait_for_builder', guard.stop_reason(self.root, self.lease))

    def test_worker_poll_blocked_normal_shell_untouched(self):
        event = {'tool_name': 'Bash', 'tool_input': {'command': 'while kill -0 12345; do sleep 5; done'}}
        self.assertIn(self.folder.name, guard.shell_reason(self.root, self.lease, event))
        for command in ('npm test', 'git status', 'tail application.log', 'sleep 1'):
            event['tool_input']['command'] = command
            self.assertIsNone(guard.shell_reason(self.root, self.lease, event))

    def test_other_session_and_unregistered_session_unaffected(self):
        self.assertIsNone(guard.stop_reason(self.root, 'another-lease'))
        self.assertIsNone(guard.stop_reason(self.root, None))

    def test_already_reviewed_task_not_forced_to_wait(self):
        native_hooks.atomic_json(self.folder / 'state.json', {'stage': 'slice_reviewed'})
        self.assertIsNone(guard.stop_reason(self.root, self.lease))


if __name__ == '__main__':
    unittest.main()
