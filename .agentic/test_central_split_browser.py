"""Split frontend/backend URL contract and actual headed spec selection."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import web_acceptance as web
import managed_backend

class SplitBrowserTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);self.root=Path(td.name).resolve()
        self.front=self.root/'admin';self.front.mkdir()
        (self.front/'package.json').write_text('{"dependencies":{"react":"1"}}')
        (self.front/'node_modules/@playwright/test').mkdir(parents=True)
        (self.front/'node_modules/@playwright/test/cli.js').write_text('')
        (self.front/'e2e').mkdir();(self.front/'e2e/login.spec.ts').write_text('test("login",()=>{})')
        (self.front/'playwright.config.ts').write_text('export default { webServer: { command: "npm run dev", url: process.env.PLAYWRIGHT_BASE_URL } };')
        self.back=self.root/'backend';self.back.mkdir();(self.back/'gaston-e2e.json').write_text('{}')
        (self.front/'gaston-web-e2e.json').write_text(json.dumps({'backend_project':'backend','frontend':'playwright_webServer'}))
    def test_separate_frontend_and_api_urls(self):
        calls=[]
        def execute(argv,**kwargs):
            calls.append((argv,kwargs))
            return {'exit_code':0}
        def launcher(root,project,*,browser_check):
            self.assertEqual(project,'backend')
            return {'status':browser_check('http://127.0.0.1:18080')['status'],'process_cleanup':'complete'}
        with patch.object(web.shutil,'which',return_value='node'),patch.object(managed_backend,'run',side_effect=launcher):
            result=web.run_browser(self.root,'admin',execute=execute)
        self.assertEqual(result['lifecycle'],'managed_backend_and_playwright_webServer')
        env=calls[0][1]['env']
        self.assertEqual(env['AGENTIC_TEST_API_BASE_URL'],'http://127.0.0.1:18080')
        self.assertEqual(env['PLAYWRIGHT_BASE_URL'],'http://127.0.0.1:'+env['GASTON_WEB_PORT'])
        self.assertEqual(env['GASTON_API_BASE_URL'],env['AGENTIC_TEST_API_BASE_URL'])
    def test_missing_frontend_contract_blocks_before_launch(self):
        (self.front/'playwright.config.ts').write_text('export default {}')
        with patch.object(web.shutil,'which',return_value='node'),patch.object(managed_backend,'run') as launch:
            result=web.run_browser(self.root,'admin',execute=lambda *a,**k:{'exit_code':0})
        self.assertEqual(result['status'],'blocked')
        launch.assert_not_called()
