#!/usr/bin/env python3
"""Focused workspace operations regression tests, including negative safety cases."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parent))
import lead_workspace_ops as ops
import verification_mcp as vm
import eng

class WorkspaceOperations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'backend').mkdir()
        (self.root/'backend'/'pom.xml').write_text('<project/>')
        (self.root/'web').mkdir()
        (self.root/'web'/'index.html').write_text('<h1>Local preview</h1>')
        (self.root/'web'/'main.css').write_text('body{}')

    def test_overview_lists_only_workspace_and_skips_env(self):
        (self.root/'.env').write_text('PASSWORD=secret')
        self.assertEqual(ops.inspect(self.root)['status'],'ready')
        names=[e['name'] for e in ops.inspect(self.root)['entries']]
        self.assertIn('backend',names)
        self.assertNotIn('.env',names)

    def test_bounded_find_and_hidden_git_excluded(self):
        (self.root/'backend'/'HealthController.java').write_text('class HealthController {}')
        (self.root/'backend'/'.git').mkdir()
        (self.root/'backend'/'.git'/'sensitive.txt').write_text('hush')
        self.assertEqual(ops.inspect(self.root,'find',pattern='Health')['matches'],['backend/HealthController.java'])
        self.assertEqual(ops.inspect(self.root,'find',pattern='sensitive')['matches'],[])
        self.assertEqual(ops.inspect(self.root,'overview','backend/.git')['status'],'blocked')

    def test_workspace_escape_and_symlink_traversal_denied(self):
        self.assertEqual(ops.inspect(self.root,'overview','../')['status'],'blocked')
        (self.root/'external').symlink_to('/etc',target_is_directory=True)
        self.assertEqual(ops.inspect(self.root,'overview','external')['status'],'blocked')
        self.assertEqual(ops.logs(self.root,'/etc/passwd')['status'],'blocked')

    def test_dependency_diagnostics_dont_run_build(self):
        out=ops.diagnostics(self.root,'dependencies','backend')
        self.assertEqual(out['status'],'ready')
        self.assertEqual(out['found']['Java Maven'],['pom.xml'])
        self.assertIn('Maven dependencies are resolved',out['guidance'][0])

    def test_toolchain_is_strict_command_allowlist(self):
        with patch.object(ops,'_run_version',return_value={'installed':False}) as run:
            out=ops.diagnostics(self.root,'toolchain')
        self.assertEqual(out['status'],'ready')
        self.assertEqual(run.call_count,len(ops.VERSION_PROBES))
        self.assertNotIn('bash',ops.VERSION_PROBES)

    def test_ports_only_explicit_loopback(self):
        self.assertEqual(ops.diagnostics(self.root,'ports',ports=['443'])['status'],'blocked')
        self.assertEqual(ops.diagnostics(self.root,'ports',ports=[22])['status'],'ready')

    def test_log_tail_and_secret_redaction(self):
        log=self.root/'backend'/'build.log'
        log.write_text('start\n{"password":"sensitive123","token":"abcdefgh12345"}\nERROR: test failed\n')
        out=ops.logs(self.root,'backend/build.log',lines=5)
        self.assertEqual(out['status'],'ready')
        self.assertNotIn('sensitive123',out['lines'])
        self.assertNotIn('abcdefgh12345',out['lines'])
        self.assertIn('ERROR: test failed',out['lines'])
        self.assertEqual(ops.logs(self.root,'backend/build.log',pattern='ERROR')['lines'],'ERROR: test failed')

    def test_log_rejects_private_token_and_unrelated_binary(self):
        d=self.root/'.agentic'/'local';d.mkdir(parents=True)
        (d/'lead-verification-token').write_text('very secret')
        self.assertEqual(ops.logs(self.root,'.agentic/local/lead-verification-token')['status'],'blocked')
        (self.root/'backend'/'secret.key').write_text('private')
        self.assertEqual(ops.logs(self.root,'backend/secret.key')['status'],'blocked')

    def test_log_tail_is_bounded_even_for_long_files(self):
        f=self.root/'backend'/'build.log';f.write_text('x'*100000+'\nLAST\n')
        o=ops.logs(self.root,'backend/build.log')
        self.assertTrue(o['truncated'])
        self.assertIn('LAST',o['lines'])
        self.assertLessEqual(len(o['lines']),ops.MAX_OUTPUT)

    def test_note_only_operational_paths_create_and_append(self):
        out=ops.note(self.root,'release.md','initial',mode='create')
        self.assertEqual(out['status'],'pass')
        self.assertEqual(ops.note(self.root,'release.md','duplicate',mode='create')['status'],'blocked')
        self.assertEqual(ops.note(self.root,'release.md','added',mode='append')['status'],'pass')
        body=(self.root/out['path']).read_text()
        self.assertIn('initial',body)
        self.assertIn('added',body)
        self.assertEqual(ops.note(self.root,'../../bad.md','bad')['status'],'blocked')

    def test_note_for_known_task_but_no_fabricated_task(self):
        self.assertEqual(ops.note(self.root,'note.md','test',task_id='task-123456')['status'],'blocked')
        task=self.root/'.agentic'/'runs'/'task-123456';task.mkdir(parents=True)
        (task/'task.json').write_text('{}')
        out=ops.note(self.root,'note.md','test',task_id='task-123456')
        self.assertEqual(out['status'],'pass')
        self.assertEqual(out['path'],'.agentic/runs/task-123456/lead-notes/note.md')

    def test_housekeeping_doesnt_clean_recent_unknown_or_caches(self):
        folder=self.root/'.agentic'/'local';folder.mkdir(parents=True)
        stale=folder/'.sessions-foo.tmp';stale.write_text('a')
        now=folder/'.sessions-bar.tmp';now.write_text('new')
        code=folder/'backend-dependencies';code.mkdir()
        important=code/'cache.jar';important.write_text('do not delete')
        os.utime(stale,(time.time()-90000,time.time()-90000))
        inspect=ops.housekeeping(self.root,'inspect')
        self.assertIn('.agentic/local/.sessions-foo.tmp',inspect['eligible'])
        self.assertEqual(ops.diagnostics(self.root,'maintenance')['eligible'],inspect['eligible'])
        self.assertEqual(ops.housekeeping(self.root,'cleanup',True,'')['status'],'blocked')
        self.assertTrue(stale.exists())
        result=ops.housekeeping(self.root,'cleanup',True,'please clean stale temporary files')
        self.assertEqual(result['status'],'pass')
        self.assertFalse(stale.exists())
        self.assertTrue(now.exists());self.assertTrue(important.exists())

    def test_processes_are_read_only(self):
        result=ops.processes(self.root)
        self.assertIn(result['status'],('ready','blocked'))
        self.assertNotIn('kill',str(result).lower())

    def test_preview_never_starts_without_index_html(self):
        self.assertEqual(ops.preview(self.root,'start','backend')['status'],'blocked')
        self.assertEqual(ops.preview(self.root,'stop')['status'],'blocked')

    def test_preview_lifecycle_and_forbidden_paths(self):
        start=ops.preview(self.root,'start','web')
        self.assertEqual(start['status'],'pass',start)
        self.addCleanup(lambda:ops.preview(self.root,'stop'))
        url=start['url']
        self.assertIn(b'Local preview',urlopen(url,timeout=3).read())
        with self.assertRaises(HTTPError) as ctx:
            urlopen(url+'.git/config',timeout=3)
        self.assertIn(ctx.exception.code,(403,404))
        self.assertTrue(ops.preview(self.root,'status')['running'])
        stopped=ops.preview(self.root,'stop')
        self.assertEqual(stopped['status'],'pass',stopped)
        self.assertFalse(ops.preview(self.root,'status')['running'])
        self.assertEqual(ops.preview(self.root,'stop')['status'],'blocked')

    def test_preview_cannot_stop_unowned_pid(self):
        pdir=self.root/'.agentic'/'local'/'preview';pdir.mkdir(parents=True)
        (pdir/'owner.json').write_text(json.dumps({'pid':os.getpid(),'starttime':0,'workspace':str(self.root)}))
        self.assertEqual(ops.preview(self.root,'stop')['status'],'blocked')
        self.assertEqual(ops.preview(self.root,'start','web')['status'],'blocked')

    def test_docs_exact_edits_are_scoped_and_require_real_request(self):
        docs=self.root/'docs';docs.mkdir()
        self.assertEqual(ops.document(self.root,'docs/release.md','create','Launch notes','', '')['status'],'blocked')
        created=ops.document(self.root,'docs/release.md','create','Launch notes','', 'Please create release notes')
        self.assertEqual(created['status'],'pass')
        self.assertEqual(ops.document(self.root,'docs/release.md','create','overwrite','', 'write docs')['status'],'blocked')
        updated=ops.document(self.root,'docs/release.md','replace_exact','Release notes','Launch notes','Fix heading')
        self.assertEqual(updated['status'],'pass')
        self.assertEqual((docs/'release.md').read_text(),'Release notes\n')
        self.assertEqual(ops.document(self.root,'docs/release.md','replace_exact','X','MISSING','Fix')['status'],'blocked')
        self.assertEqual(ops.document(self.root,'docs/release.md','append','Add this note','', 'Please append')['status'],'pass')
        self.assertIn('Add this note',(docs/'release.md').read_text())

    def test_docs_denies_app_source_policy_and_symlink(self):
        (self.root/'docs').mkdir()
        for path in ('backend/Main.java','../../etc/passwd','CLAUDE.md','docs/AGENTS.md','.agentic/docs/rules.md'):
            self.assertEqual(ops.document(self.root,path,'create','bad','', 'write docs')['status'],'blocked',path)
        (self.root/'docs'/'link.md').symlink_to(self.root/'docs'/'test.md')
        self.assertEqual(ops.document(self.root,'docs/link.md','create','bad','', 'write docs')['status'],'blocked')
        self.assertFalse((self.root/'docs'/'test.md').exists())

    def test_database_never_accesses_remote_hosts(self):
        for value in ('postgresql://user:pass@db.production.com:5432/payments',
                      'postgresql://user:pass@127.0.0.1/db?host=db.production.com'):
            with patch.dict(os.environ,{'GASTON_READONLY_DATABASE_URL':value}):
                self.assertEqual(ops.database(self.root)['status'],'blocked')
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(ops.database(self.root)['status'],'blocked')

    def test_database_uses_fixed_readonly_metadata_sql_and_hides_password(self):
        from types import SimpleNamespace
        url='postgresql://audit:fixture-password@127.0.0.1:5432/sample-project'
        def examine(argv,**kwargs):
            self.assertNotIn('fixture-password',str(argv))
            self.assertNotIn('DELETE',str(argv))
            self.assertIn('default_transaction_read_only=on',kwargs['env']['PGOPTIONS'])
            self.assertEqual(kwargs['env']['PGHOST'],'127.0.0.1')
            self.assertEqual(kwargs['env']['PGHOSTADDR'],'127.0.0.1')
            self.assertNotIn('PGSERVICE',kwargs['env'])
            self.assertEqual(kwargs['env']['PGUSER'],'audit')
            self.assertEqual(kwargs['env']['PGDATABASE'],'sample-project')
            self.assertEqual(kwargs['env']['PGPASSWORD'],'fixture-password')
            return SimpleNamespace(returncode=0,stdout='sample-project | audit | on\n',stderr='')
        with patch.dict(os.environ,{'GASTON_READONLY_DATABASE_URL':url,
                                     'PGHOSTADDR':'203.0.113.10',
                                     'PGSERVICE':'external-production'}):
            with patch.object(ops.shutil,'which',return_value='/usr/bin/psql'), patch.object(ops.subprocess,'run',side_effect=examine):
                self.assertEqual(ops.database(self.root)['status'],'ready')
                self.assertEqual(ops.database(self.root,'columns',table='customers')['status'],'ready')
        self.assertEqual(ops.database(self.root,'columns',table='x; DROP TABLE users')['status'],'blocked')
        with patch.dict(os.environ,{'GASTON_READONLY_DATABASE_URL':'postgresql://audit@127.0.0.1:notaport/sample-project'}):
            self.assertEqual(ops.database(self.root)['status'],'blocked')

    def test_mcp_tools_exist_and_use_registered_wrapper(self):
        names={t['name'] for t in vm.TOOLS}
        for tool in ('workspace_overview','workspace_diagnostics','workspace_logs','workspace_processes',
                     'workspace_preview','workspace_housekeeping','workspace_note','workspace_document','workspace_database'):
            self.assertIn(tool,names)
            self.assertIn(tool,vm.CALLS)
        with patch.object(vm,'ROOT',self.root),patch.object(vm,'_authorized_delegate',return_value=True):
            result=vm.CALLS['workspace_overview']({'action':'overview'})
        self.assertEqual(result['status'],'ready')
        with patch.object(vm,'_authorized_delegate',return_value=False):
            self.assertEqual(vm.CALLS['workspace_diagnostics']({'action':'toolchain'})['status'],'blocked')

    def test_permission_boundary_and_no_unrestricted_shell(self):
        approved=eng.lead_safe_mcp_permissions().split(',')
        for key in ('workspace_overview','workspace_diagnostics','workspace_logs','workspace_processes',
                    'workspace_note','workspace_database'):
            self.assertIn('mcp__gaston_verify__'+key,approved)
        for key in ('git_commit','workspace_preview','workspace_housekeeping','workspace_document'):
            self.assertNotIn('mcp__gaston_verify__'+key,approved)
        self.assertNotIn('execute_shell',vm.CALLS)

if __name__=='__main__':unittest.main()
