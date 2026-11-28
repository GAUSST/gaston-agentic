"""Pre-delegation cache preparation regression, with real local subprocess execution."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backend_runner
import builder_compile
import verification_mcp as vm


WRAPPER = '''#!/bin/sh
repo=''
for arg in "$@"; do
  case "$arg" in -Dmaven.repo.local=*) repo="${arg#-Dmaven.repo.local=}";; esac
done
if test -f "$repo/prepared.marker"; then echo 'BUILD SUCCESS'; exit 0; fi
for arg in "$@"; do
  if test "$arg" = '-o'; then
    echo 'Cannot access central in offline mode: artifact has not been downloaded from it before' >&2
    exit 1
  fi
done
mkdir -p "$repo" || exit 2
printf prepared > "$repo/prepared.marker"
echo 'BUILD SUCCESS'
'''

class BuilderDependencyPreparationTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.addCleanup(self.dir.cleanup)
        self.root=Path(self.dir.name)
        self.backend=self.root/'arbitrary-java-service';self.backend.mkdir()
        (self.backend/'pom.xml').write_text('<project/>')
        wrapper=self.backend/'mvnw';wrapper.write_text(WRAPPER);wrapper.chmod(0o755)
        properties=self.backend/'.mvn/wrapper/maven-wrapper.properties'
        properties.parent.mkdir(parents=True)
        properties.write_text('distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.zip')
        cached=(self.root/'.agentic/local/backend-dependencies/maven-user/wrapper/dists/'
                'apache-maven-3.9.9-bin/hash/apache-maven-3.9.9/bin/mvn')
        cached.parent.mkdir(parents=True);cached.write_text('cached')
        self.patch_root=patch.object(vm,'ROOT',self.root);self.patch_root.start();self.addCleanup(self.patch_root.stop)
        self.patch_java=patch.object(backend_runner,'installed_jdks',return_value=[]);self.patch_java.start();self.addCleanup(self.patch_java.stop)

    def test_offline_preflight_reports_real_missing_cache_without_network(self):
        result=vm.prepare_builder_dependencies('arbitrary-java-service')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['preflight']['reason_code'],'DEPENDENCY_RESOLUTION_UNAVAILABLE')
        self.assertTrue(result['preflight']['network_authorization_required'])
        self.assertFalse((self.root/'.agentic/local/backend-dependencies/maven-repository/prepared.marker').exists())

    def test_online_requires_explicit_approval(self):
        result=vm.prepare_builder_dependencies('arbitrary-java-service',allow_network=True)
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(result['network_attempted'])
        self.assertFalse((self.root/'.agentic/local/backend-dependencies/maven-repository/prepared.marker').exists())

    def test_explicit_preparation_warms_cache_then_builder_offline_passes(self):
        result=vm.prepare_builder_dependencies('arbitrary-java-service',True,True,'Yes, download project build dependencies')
        self.assertEqual(result['status'],'pass',result)
        self.assertTrue(result['ready_for_delegation'])
        self.assertEqual(result['preflight']['resolution_mode'],'online_fallback')
        compiled=builder_compile.check(self.root,'arbitrary-java-service')
        self.assertEqual(compiled['status'],'pass',compiled)
        self.assertEqual(compiled['resolution_mode'],'offline_cache')
        self.assertFalse(compiled['network_authorized'])
        self.assertTrue((self.root/'.agentic/local/backend-dependencies/maven-repository/prepared.marker').is_file())

    def test_unrecognized_project_is_rejected(self):
        result=vm.prepare_builder_dependencies('../elsewhere',True,True,'approved')
        self.assertEqual(result['status'],'blocked')

    def test_offline_blocker_diagnostic_survives_attempt_receipt(self):
        folder=self.root/'.agentic/runs/individual-task';folder.mkdir(parents=True)
        attempt='b'*32;project='arbitrary-java-service'
        result=builder_compile.check(self.root,project)
        self.assertIn('offline_blocker',result)
        record={'attempt':attempt,'project':project,'fingerprint':'x','result':result}
        builder_compile.receipt_path(folder,attempt,project).parent.mkdir(parents=True)
        builder_compile.receipt_path(folder,attempt,project).write_text(json.dumps(record))
        with patch.object(builder_compile,'material_fingerprint',return_value='x'):
            observed=builder_compile.receipts(self.root,folder,attempt,[project])[0]
        self.assertEqual(observed['reason_code'],'DEPENDENCY_RESOLUTION_UNAVAILABLE')
        self.assertTrue(observed['network_authorization_required'])
        self.assertEqual(observed['offline_blocker'],'Dependency resolution unavailable; build did not complete')
        self.assertIsNotNone(observed['log_path'])

if __name__=='__main__':unittest.main()
