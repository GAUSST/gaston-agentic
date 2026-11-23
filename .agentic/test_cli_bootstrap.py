"""Unit and end-to-end proof that no pip installation is needed on Unix."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import cli_bootstrap

ROOT = Path(__file__).resolve().parent.parent

@unittest.skipIf(sys.platform=='win32','Unix CLI installation tests')
class CliBootstrapTests(unittest.TestCase):
    def test_idempotent_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {'PATH':str(home/'.local/bin'), 'SHELL':'/bin/bash'}
            self.assertEqual(cli_bootstrap.register(home=home,environ=env),'installed')
            command = home/'.local/bin/dev'
            self.assertEqual(cli_bootstrap.register(home=home,environ=env),'already installed')
            command.write_text('#!/bin/sh\necho other\n')
            self.assertIn('not managed',cli_bootstrap.register(home=home,environ=env))
            self.assertEqual(command.read_text(),'#!/bin/sh\necho other\n')

    def test_profiles_only_when_path_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {'PATH':'/usr/bin:/bin', 'SHELL':'/bin/bash'}
            self.assertIn('new terminal',cli_bootstrap.register(home=home,environ=env))
            rc = home/'.bashrc'
            self.assertEqual(rc.read_text().count(cli_bootstrap.PATH_MARKER),1)
            self.assertEqual(cli_bootstrap.register(home=home,environ=env),'already installed')
            self.assertEqual(rc.read_text().count(cli_bootstrap.PATH_MARKER),1)

    def test_global_command_works_from_workspace_and_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            td=Path(tmp)
            home=td/'home';home.mkdir()
            workspace=td/'project';workspace.mkdir()
            shutil.copytree(ROOT/'.agentic',workspace/'.agentic',ignore=shutil.ignore_patterns('__pycache__','local'))
            shutil.copy2(ROOT/'dev',workspace/'dev')
            env=dict(os.environ,HOME=str(home),SHELL='/bin/bash',PATH=str(home/'.local/bin')+os.pathsep+os.environ.get('PATH',''))
            r=subprocess.run([str(workspace/'dev'),'--help'],cwd=workspace,env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertTrue((home/'.local/bin/dev').is_file())
            child=workspace/'src';child.mkdir()
            r=subprocess.run(['dev','--help'],cwd=child,env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertIn('init',r.stdout)

if __name__=='__main__':unittest.main()
