"""Offline project-scope and browser lifecycle regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import verification_brief as brief
import web_acceptance as web
import managed_backend


class WebAcceptanceTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve()
        self.admin=self.make_web('kapitale-admin',browser=False)
        self.legacy=self.make_web('kapitale-backend/web-e2e',browser=True)
        self.calls=[]

    def make_web(self, rel, browser):
        folder=self.root/rel
        folder.mkdir(parents=True)
        (folder/'package.json').write_text(json.dumps({'dependencies':{'react':'1'},'scripts':{'typecheck':'tsc --noEmit','lint':'eslint .','build':'vite build','test':'vitest run'}}))
        (folder/'node_modules/@playwright/test').mkdir(parents=True)
        (folder/'node_modules/@playwright/test/cli.js').write_text('')
        if browser:
            (folder/'playwright.config.ts').write_text('export default { use: { baseURL: process.env.PLAYWRIGHT_BASE_URL } };')
            (folder/'e2e').mkdir()
            (folder/'e2e/smoke.spec.ts').write_text('test("example", async()=>{});')
        return folder

    def execute(self, argv, **kwargs):
        self.calls.append((argv,kwargs))
        return {'exit_code':0,'stdout':'28 tests passed','stderr':''}

    def test_http_slice_requires_local_project_checks_not_legacy_browser(self):
        result=brief.produce(self.root,{'goal':'Typed HTTP layer','work_plan_id':'plan-1'},
                             {'changed_files':['kapitale-admin/src/api/http.ts']},{})
        gates={(x['name'].split('__')[0],x['cwd']) for x in result['selected_checks']}
        self.assertIn(('web_verify','kapitale-admin'),gates)
        self.assertNotIn(('web_e2e','kapitale-backend/web-e2e'),gates)
        self.assertFalse(result['missing_gates'],result)

    def test_integrated_checkpoint_requires_affected_browser_suite(self):
        result=brief.produce(self.root,{'goal':'Checkpoint','work_plan_id':'plan-1','checkpoint_acceptance':True},
                             {'changed_files':['kapitale-admin/src/api/http.ts']},{})
        self.assertIn('web_e2e:kapitale-admin',result['missing_gates'])
        self.assertFalse(any(x['cwd']=='kapitale-backend/web-e2e' for x in result['selected_checks']))

    def test_local_checks_execute_real_scripts(self):
        with patch.object(web.shutil,'which',return_value='npm'):
            result=web.run_local(self.root,'kapitale-admin',execute=self.execute)
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(set(result['checks']),{'typecheck','lint','build','test'})
        self.assertTrue(all(k['cwd']==self.admin for _,k in self.calls))

    def test_local_failure_never_passes(self):
        with patch.object(web.shutil,'which',return_value='npm'):
            result=web.run_local(self.root,'kapitale-admin',execute=lambda *a,**k:{'exit_code':1})
        self.assertEqual(result['status'],'fail')

    def test_native_webserver_needs_no_export(self):
        (self.legacy/'playwright.config.ts').write_text('export default { webServer: { command: "npm run dev", url: "http://127.0.0.1:4173" } };')
        with patch.object(web.shutil,'which',return_value='node'):
            result=web.run_browser(self.root,'kapitale-backend/web-e2e',execute=self.execute)
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(result['lifecycle'],'playwright_webServer')
        self.assertNotIn('PLAYWRIGHT_BASE_URL',self.calls[0][1]['env'])

    def test_nested_backend_launch_supplies_fresh_url_only_to_child(self):
        backend=self.legacy.parent
        (backend/'pom.xml').write_text('<project/>')
        (backend/'gaston-e2e.json').write_text('{}')
        def launch(root,relative_path,*,browser_check):
            self.assertEqual(relative_path,'kapitale-backend')
            result=browser_check('http://127.0.0.1:49152')
            return {'status':result['status'],'managed_application':True,'process_cleanup':'complete'}
        with patch.object(web.shutil,'which',return_value='node'), patch.object(managed_backend,'run',side_effect=launch):
            result=web.run_browser(self.root,'kapitale-backend/web-e2e',execute=self.execute)
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(self.calls[0][1]['env']['PLAYWRIGHT_BASE_URL'],'http://127.0.0.1:49152')
        self.assertEqual(result['process_cleanup'],'complete')

    def test_no_contract_does_not_guess_unrelated_app(self):
        with patch.object(web.shutil,'which',return_value='node'):
            result=web.run_browser(self.root,'kapitale-backend/web-e2e',execute=self.execute)
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(self.calls)


if __name__=='__main__':
    unittest.main()
