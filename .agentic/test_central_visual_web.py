"""Contract and opt-in live Chromium/Vite fixture verification."""
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import visual_web

class VisualWebTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name).resolve();self.app=self.root/'admin';self.app.mkdir()
        (self.app/'package.json').write_text('{"name":"visual-fixture","dependencies":{"vite":"6","react":"1"}}')
    def test_no_dependencies_is_blocked_not_faked(self):
        result=visual_web.run(self.root,'admin')
        self.assertEqual(result['status'],'blocked')
        self.assertIn('Vite',result['reason'])
    def test_external_navigation_rejected(self):
        result=visual_web.run(self.root,'admin',routes=['//example.com'])
        self.assertEqual(result['status'],'blocked')
        self.assertIn('local application routes',result['reason'])
    @unittest.skipUnless(os.environ.get('GASTON_LIVE_WEB_MODULES'),'opt-in actual browser integration')
    def test_real_app_and_responsive_screenshots(self):
        modules=Path(os.environ['GASTON_LIVE_WEB_MODULES']).resolve()
        (self.app/'node_modules').symlink_to(modules,target_is_directory=True)
        (self.app/'index.html').write_text('<!doctype html><html><head><title>Actual Vite fixture</title></head><body><h1>Real app</h1><script type="module" src="/src.js"></script></body></html>')
        (self.app/'src.js').write_text('document.querySelector("h1").textContent="Real Vite application";')
        result=visual_web.run(self.root,'admin')
        self.assertEqual(result['status'],'pass',result)
        self.assertTrue(result['visual_only'])
        self.assertEqual(result['process_cleanup'],'complete')
        self.assertEqual({row['width'] for row in result['screenshots']},{1440,820,390})
        for row in result['screenshots']:
            data=(self.root/row['path']).read_bytes()
            self.assertEqual(data[:8],b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II',data[16:24]),(row['width'],900))
        import urllib.request
        with self.assertRaises(OSError):urllib.request.urlopen(result['frontend_url'],timeout=2)
