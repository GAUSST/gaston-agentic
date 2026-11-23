"""Regression tests for complete distribution and loaded runtime identity."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import evolution
import release_audit
import verification_mcp as vm

ROOT = Path(__file__).resolve().parent.parent


class ReleaseIntegrityTests(unittest.TestCase):
    def test_current_release_package(self):
        result = release_audit.audit(ROOT)
        self.assertEqual(result['status'], 'pass', result)
        self.assertGreater(result['python_files'], 30)

    def test_checkout_preserves_manifest_bytes_on_all_platforms(self):
        # Git for Windows otherwise converts checked-out LF to CRLF,
        # invalidating strict SHA-256 checks for nearly every toolkit file.
        attributes = (ROOT / '.gitattributes').read_text(encoding='utf-8')
        self.assertIn('* text=auto eol=lf', attributes.splitlines())
        manifest = json.loads((ROOT / '.agentic/manifest.json').read_text())
        self.assertIn('.gitattributes', manifest['files'])

    def test_release_ci_installs_native_runtime_dependencies(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn('python -m pip install .', workflow)
        self.assertLess(workflow.index('python -m pip install .'),
                        workflow.index("- name: Update and CLI regression tests"))
        pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('psutil>=5.9', pyproject)

    def test_handshake_and_live_status_agree_with_manifest(self):
        manifest_bytes = (ROOT / '.agentic/manifest.json').read_bytes()
        manifest = json.loads(manifest_bytes)
        messages = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'environment_status','arguments':{}}},
        ]
        result = subprocess.run([sys.executable, str(ROOT/'.agentic/verification_mcp.py')],
            input='\n'.join(map(json.dumps, messages))+'\n', capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {x['id']:x['result'] for x in map(json.loads,result.stdout.splitlines())}
        self.assertEqual(replies[1]['serverInfo']['version'],manifest['build'])
        names = {x['name'] for x in replies[2]['tools']}
        self.assertEqual(names, set(vm.CALLS))
        self.assertTrue({'plan_work','task_code_review','stage_evolution','record_engineering_lesson'} <= names)
        status = json.loads(replies[3]['content'][0]['text'])
        self.assertEqual(status['toolkit_release'],manifest['release'])
        self.assertEqual(status['toolkit_build'],manifest['build'])
        self.assertEqual(status['manifest_sha256'],hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual(Path(status['server_path']),ROOT/'.agentic/verification_mcp.py')

    def test_audit_catches_stale_headings_even_with_valid_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp)/'copy'
            manifest=evolution.manifest(ROOT)
            evolution.snapshot(ROOT,copy,manifest)
            p=copy/'AGENTS.md'
            p.write_text(p.read_text()+'\n## v35 — old instructions\n')
            evolution.rebuild(copy,manifest)
            result=release_audit.audit(copy)
            self.assertEqual(result['status'],'fail')
            self.assertTrue(any('Historical release heading' in x for x in result['failures']))

    def test_review_required_even_if_future_manifest_release_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); agent=root/'.agentic';agent.mkdir()
            source=agent/'context_brief.py';source.write_text('def total(a,b): return a-b\n')
            evolution.put(agent/'manifest.json',{'release':'v99','files':{'.agentic/context_brief.py':evolution.digest(source)}})
            edits=[{'path':'.agentic/context_brief.py','before_sha256':evolution.digest(source),'content':'def total(a,b): return a+b\n'}]
            test='import unittest\nclass Regression(unittest.TestCase):\n    def test_one(self): self.assertEqual(1,1)\n'
            with self.assertRaisesRegex(ValueError,'Review exact required source paths'):
                evolution.stage(root,'Fix total','Wrong operator',edits,test)


if __name__ == '__main__':
    unittest.main()
