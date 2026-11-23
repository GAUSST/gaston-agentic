"""Offline regression coverage for the Lead-only PostgreSQL diagnostics."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import eng
import lead_workspace_ops as ops
import verification_mcp as vm

URL='postgresql://fixture:fixture-pass@127.0.0.1:5433/fixture-db'

class DatabaseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.env=patch.dict(os.environ,{'GASTON_READONLY_DATABASE_URL':URL},clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        binary=patch.object(ops.shutil,'which',side_effect=lambda name:'/fake/'+name)
        binary.start(); self.addCleanup(binary.stop)

    def _result(self,stdout='',code=0,stderr=''):
        return subprocess.CompletedProcess([],code,stdout,stderr)

    def test_status_does_not_connect_or_reveal_credentials(self):
        with patch.object(ops.subprocess,'run') as run:
            result=ops.database(self.root,'status')
            self.assertTrue(result['psql_installed']);self.assertTrue(result['docker_cli_installed'])
            self.assertTrue(result['readonly_url_configured']);self.assertFalse(result['fixture_data_enabled'])
            self.assertNotIn('fixture-pass',json.dumps(result));run.assert_not_called()

    def test_status_identifies_missing_client_and_url(self):
        with patch.dict(os.environ,{},clear=True),patch.object(ops.shutil,'which',return_value=None):
            result=ops.database(self.root,'status')
            self.assertFalse(result['psql_installed']);self.assertFalse(result['readonly_url_configured'])
            self.assertEqual(ops.database(self.root)['status'],'blocked')

    def test_metadata_fixed_queries_and_fields(self):
        for action,fragment,fields in [('constraints','pg_constraint',['name','type','definition']),
                                     ('indexes','pg_indexes',['name','definition']),
                                     ('table_stats','pg_stat_user_tables',['estimated_live_rows','estimated_dead_rows','seq_scan','idx_scan'])]:
            with self.subTest(action=action),patch.object(ops.subprocess,'run',return_value=self._result('example\n')) as run:
                result=ops.database(self.root,action,table='Orders')
                self.assertEqual(result['status'],'ready');self.assertEqual(result['fields'],fields)
                self.assertIn(fragment,str(run.call_args.args[0]));self.assertIn("'Orders'",str(run.call_args.args[0]))
                self.assertTrue(result['read_only']);self.assertIn('BEGIN READ ONLY;',run.call_args.args[0]);self.assertIn('ROLLBACK;',run.call_args.args[0])

    def test_data_requires_opt_in_not_model_argument(self):
        with patch.object(ops.subprocess,'run') as run:
            for action in ops.DB_FIXTURE_ACTIONS:
                kwargs={'columns':['id']} if action in ('duplicates','sample') else {}
                self.assertEqual(ops.database(self.root,action,table='orders',**kwargs)['status'],'blocked')
            run.assert_not_called()

    def test_exact_count_and_duplicates_are_distinguished_from_estimates(self):
        with patch.dict(os.environ,{'GASTON_DATABASE_ALLOW_FIXTURE_DATA':'1'}):
            for action,kwargs,fragment,stdout,fields in [
                ('count',{},'FROM "public"."orders"','7\n',['visible_rows']),
                ('duplicates',{'columns':['customer_id','external_id']},'GROUP BY "customer_id", "external_id"','2 | 3\n',['duplicate_groups','extra_rows'])]:
                with self.subTest(action=action),patch.object(ops.subprocess,'run',side_effect=[self._result('t\n'),self._result(stdout)]) as run:
                    result=ops.database(self.root,action,table='orders',**kwargs)
                    self.assertEqual(result['status'],'ready');self.assertEqual(result['fields'],fields)
                    self.assertIn(fragment,str(run.call_args.args[0]));self.assertIn('Diagnostic evidence',result['note'])
                    self.assertIn('pg_inherits',str(run.call_args_list[0].args[0]))

    def test_samples_use_explicit_projection_value_and_row_limits(self):
        with patch.dict(os.environ,{'GASTON_DATABASE_ALLOW_FIXTURE_DATA':'1'}),patch.object(ops.subprocess,'run',side_effect=[self._result('t\n'),self._result('{"id":"1","currency":"XOF"}\n')]) as run:
            result=ops.database(self.root,'sample',table='Orders',columns=['id','currency'],limit=5)
            self.assertEqual(result['status'],'ready'); self.assertEqual(result['fields'],['id','currency'])
            sql=next(a for a in run.call_args.args[0] if a.startswith('SELECT'))
            self.assertIn('"public"."Orders" LIMIT 5',sql);self.assertIn('"currency"::text, 160',sql);self.assertNotIn('SELECT *',sql)

    def test_invalid_requests_never_make_connection(self):
        bad=[('sample',{'columns':['*']}),('sample',{'columns':['password_hash']}),('sample',{'columns':['a','a']}),
             ('sample',{'columns':['x; DROP TABLE y']}),('sample',{'columns':['id'],'limit':51}),
             ('sample',{'columns':['id'],'limit':True}),('sample',{'columns':[]}),('sample',{'columns':'id'}),
             ('duplicates',{'columns':['id']*9}),('count',{'columns':['id']}),('count',{'table':'orders; SELECT 1'}),
             ('indexes',{'table':''}),('query',{})]
        with patch.dict(os.environ,{'GASTON_DATABASE_ALLOW_FIXTURE_DATA':'1'}),patch.object(ops.subprocess,'run') as run:
            for action,kwargs in bad:
                with self.subTest(action=action,kwargs=kwargs):
                    self.assertEqual(ops.database(self.root,action,**({'table':'orders'}|kwargs))['status'],'blocked')
            run.assert_not_called()

    def test_relation_guard_refuses_views_or_foreign_descendants(self):
        with patch.dict(os.environ,{'GASTON_DATABASE_ALLOW_FIXTURE_DATA':'1'}),patch.object(ops.subprocess,'run',return_value=self._result('f\n')) as run:
            result=ops.database(self.root,'sample',table='orders',columns=['id'])
            self.assertEqual(result['status'],'blocked');self.assertEqual(run.call_count,1)

    def test_env_overrides_cannot_redirect_local_connection(self):
        with patch.dict(os.environ,{'PGHOSTADDR':'203.0.113.1','PGSERVICE':'prod','PGPASSFILE':'/prod/pass','PGPORT':'99','PGOPTIONS':'-c search_path=evil','PGSSLMODE':'require'}),patch.object(ops.subprocess,'run',return_value=self._result('db | role | on\n')) as run:
            self.assertEqual(ops.database(self.root)['status'],'ready')
            env=run.call_args.kwargs['env']; argv=run.call_args.args[0]
            self.assertEqual(env['PGHOSTADDR'],'127.0.0.1');self.assertEqual(env['PGPORT'],'5433');self.assertEqual(env['PGPASSFILE'],os.devnull)
            self.assertNotIn('PGSERVICE',env);self.assertNotIn('PGSSLMODE',env);self.assertNotIn('GASTON_READONLY_DATABASE_URL',env)
            self.assertIn('search_path=pg_catalog',env['PGOPTIONS']);self.assertNotIn('fixture-pass',str(argv));self.assertIn('-w',argv)

    def test_reject_invalid_url_and_conninfo_expansion(self):
        with patch.object(ops.subprocess,'run') as run:
            for url in ('postgresql://fixture@remote/db','postgresql://fixture@localhost/db?host=remote','postgresql://fixture@localhost:0/db',
                        'postgresql://fixture@localhost/host%3Dremote','postgresql://fixture@localhost/postgresql%3A%2F%2Fremote%2Fdb','postgresql://fixture@[broken/db'):
                with self.subTest(url=url),patch.dict(os.environ,{'GASTON_READONLY_DATABASE_URL':url}):
                    self.assertEqual(ops.database(self.root)['status'],'blocked')
            run.assert_not_called()

    def test_errors_hide_server_values_and_timeouts_are_blocked(self):
        with patch.object(ops.subprocess,'run',return_value=self._result('',1,'secret plaintext customer-data fixture-pass')):
            result=ops.database(self.root,'tables')
            self.assertEqual(result['status'],'blocked');self.assertNotIn('customer-data',json.dumps(result));self.assertNotIn('fixture-pass',json.dumps(result))
        with patch.object(ops.subprocess,'run',side_effect=subprocess.TimeoutExpired('psql',9)):
            self.assertEqual(ops.database(self.root,'tables')['status'],'blocked')
        with patch.object(ops.subprocess,'run',return_value=self._result('db | role | off\n')):
            self.assertEqual(ops.database(self.root)['status'],'blocked')

    def test_output_cap_is_explicit(self):
        with patch.object(ops.subprocess,'run',return_value=self._result('a'*(ops.MAX_OUTPUT+10))):
            result=ops.database(self.root,'indexes',table='orders')
            self.assertTrue(result['output_truncated']);self.assertEqual(len(result['rows']),ops.MAX_OUTPUT)

    def test_mcp_action_schema_matches_and_routes_new_fields(self):
        tool=next(t for t in vm.TOOLS if t['name']=='workspace_database')
        self.assertEqual(set(tool['inputSchema']['properties']['action']['enum']),set(ops.DB_ACTIONS))
        with patch.object(vm,'_workspace_op') as call:
            vm.CALLS['workspace_database']({'action':'sample','table':'orders','columns':['id'],'limit':3})
            call.assert_called_once_with('database','sample','public','orders',['id'],3)

    def test_builder_does_not_inherit_readonly_credentials(self):
        with patch.dict(os.environ,{'GASTON_DATABASE_ALLOW_FIXTURE_DATA':'1'}):
            for env in (eng.codex_builder_env(),eng.claude_advisor_env('builder',{})):
                self.assertNotIn('GASTON_READONLY_DATABASE_URL',env);self.assertNotIn('GASTON_DATABASE_ALLOW_FIXTURE_DATA',env)

if __name__=='__main__':unittest.main()
