"""Real Git fixtures for reservation, conflict and evidence invalidation."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import verification_coordination as c
import orchestration as orch

class CoordinationTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve()
        for name,files in [('backend',{'pom.xml':'<project/>','gaston-e2e.json':'{}','service.java':'class Service {}'}),('admin',{'package.json':'{"dependencies":{"react":"1"}}','app.ts':'export {}'})]:
            p=self.root/name;p.mkdir()
            for rel,content in files.items():(p/rel).write_text(content)
            subprocess.run(['git','init','-q'],cwd=p,check=True)
            subprocess.run(['git','add','.'],cwd=p,check=True)
            subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','baseline'],cwd=p,check=True)
    def test_disjoint_api_and_frontend(self):
        with patch.object(orch,'active_builders',return_value=[{'task_id':'auth-task','scope':['admin/app.ts']}]):
            with c.reserve(self.root,'api','backend') as r:
                self.assertEqual(r['read_scope'],['backend'])
                self.assertEqual(r['parallel_with'],['auth-task'])
                c.assert_write_allowed(self.root,['admin/app.ts'])
                with self.assertRaises(ValueError):c.assert_write_allowed(self.root,['backend/service.java'])
                with self.assertRaises(c.Busy): 
                    with c.reserve(self.root,'backend','backend'):pass
            self.assertIsNone(c.active(self.root))
    def test_conflicting_and_integrated_gates_blocked(self):
        with patch.object(orch,'active_builders',return_value=[{'task_id':'worker','scope':['backend/service.java']}]):
            with self.assertRaises(c.Busy):
                with c.reserve(self.root,'api','backend'):pass
        with patch.object(orch,'active_builders',return_value=[{'task_id':'worker','scope':['admin/app.ts']}]):
            with self.assertRaises(c.Busy):
                with c.reserve(self.root,'api','backend',integrated=True):pass
    def test_relevant_fingerprint_only(self):
        before=c.fingerprints(self.root,['backend'])
        self.assertIsNotNone(before)
        (self.root/'admin/app.ts').write_text('export const x=1')
        self.assertEqual(c.fingerprints(self.root,['backend']),before)
        (self.root/'backend/service.java').write_text('class Service { int x; }')
        self.assertNotEqual(c.fingerprints(self.root,['backend']),before)
    def test_declared_cross_repo_dependency_blocks_parallel(self):
        (self.root/'backend/gaston-e2e.json').write_text(json.dumps({'read_projects':['admin']}))
        with patch.object(orch,'active_builders',return_value=[{'task_id':'worker','scope':['admin/app.ts']}]):
            with self.assertRaises(c.Busy):
                with c.reserve(self.root,'api','backend'):pass
    def test_reservation_released_on_failure(self):
        with self.assertRaises(RuntimeError):
            with c.reserve(self.root,'backend','backend'):raise RuntimeError('failed')
        self.assertIsNone(c.active(self.root))
