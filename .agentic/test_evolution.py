"""Real staged red/green validation, activation and recovery in disposable workspaces."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import evolution as evo
import learning_engine as learn

BAD='def total(a,b):\n    return a-b\n'
GOOD='def total(a,b):\n    return a+b\n'
REG='import unittest\nfrom context_brief import total\nclass Regression(unittest.TestCase):\n    def test_total(self): self.assertEqual(total(4,3),7)\n'
REVIEW={'clarity':'Named arithmetic helper', 'difficult_logic':'No hidden state or side effects', 'maintainability':'Small isolated function', 'correctness':'Regression checks expected arithmetic'}
BASE='import unittest\nfrom context_brief import total\nclass Existing(unittest.TestCase):\n    def test_zero(self): self.assertEqual(total(1,0),1)\n'

class EvolutionTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.agent=self.root/'.agentic';self.agent.mkdir();self.module=self.agent/'context_brief.py';self.module.write_text(BAD)
        (self.agent/'test_existing.py').write_text(BASE);(self.agent/'eng.py').write_text('print("startup OK")\n')
        files={p.relative_to(self.root).as_posix():evo.digest(p) for p in self.agent.iterdir()}
        (self.root/'application').mkdir();(self.root/'application/code.py').write_text('private application\n');(self.root/'.env').write_text('PRIVATE=do-not-copy')
        evo.put(self.agent/'manifest.json',{'version':1,'release':'v37','files':files});self.orig=(self.agent/'manifest.json').read_bytes()
    def stage(self,content=GOOD,reg=REG,kind='bug',path='.agentic/context_brief.py'):
        p=self.root/path
        return evo.stage(self.root,'Fix arithmetic','Used subtraction rather than addition',[{'path':path,'before_sha256':evo.digest(p) if p.is_file() else None,'content':content}],reg,kind=kind,source_reviews={path:REVIEW,'regression_test':REVIEW})['candidate_id']
    def validated(self):
        k=self.stage();r=evo.validate(self.root,k);self.assertEqual(r['status'],'pass',r);return k
    def test_real_red_green_activation_rollback(self):
        key=self.stage();folder=evo.entry(self.root,key);self.assertEqual(self.module.read_text(),BAD);self.assertFalse((folder/'candidate/application').exists());self.assertFalse((folder/'candidate/.env').exists())
        r=evo.validate(self.root,key);self.assertEqual(r['status'],'pass',r);self.assertEqual(r['checks']['reproduction_before']['exit_code'],1);self.assertEqual(r['checks']['regression_after']['tests'],1);self.assertEqual(r['checks']['compatibility']['tests'],2)
        evo.queue(self.root,key);self.assertEqual(self.module.read_text(),BAD);self.assertIn('applied',evo.apply_pending(self.root));self.assertEqual(self.module.read_text(),GOOD)
        m=evo.read(self.agent/'manifest.json');self.assertEqual(m['files']['.agentic/context_brief.py'],evo.digest(self.module));self.assertIn(evo.read(folder/'record.json')['regression_test'],m['files'])
        evo.queue(self.root,key,rollback=True);self.assertIn('rolled back',evo.apply_pending(self.root));self.assertEqual(self.module.read_text(),BAD);self.assertEqual((self.agent/'manifest.json').read_bytes(),self.orig);self.assertEqual((self.root/'application/code.py').read_text(),'private application\n')
    def test_no_repro_no_activation(self):
        key=self.stage(reg=REG.replace('total(4,3),7','total(4,0),4'));self.assertEqual(evo.validate(self.root,key)['status'],'fail');self.assertRaises(ValueError,evo.queue,self.root,key)
    def test_existing_regression_failure(self):
        key=self.stage(content='def total(a,b):\n    return a+b+int(b==0)\n');r=evo.validate(self.root,key);self.assertEqual(r['status'],'fail',r);self.assertEqual(r['checks']['compatibility']['exit_code'],1)
    def test_protected_and_stale(self):
        for path in ('.agentic/test_existing.py','application/code.py','.env','../outside','dev','.agentic/evolution.py','.agentic/manifest.json'):
            with self.subTest(path=path),self.assertRaises((ValueError,TypeError)):self.stage(path=path)
        with self.assertRaises(ValueError):evo.stage(self.root,'a','b',[{'path':'.agentic/context_brief.py','before_sha256':'wrong','content':GOOD}],REG)
        self.module.write_text('user customization');self.assertRaises(ValueError,self.stage)
    def test_candidate_tamper_and_live_conflict(self):
        key=self.stage();f=evo.entry(self.root,key);(f/'candidate/.agentic/context_brief.py').write_text('modified');self.assertRaises(ValueError,evo.validate,self.root,key)
        key=self.validated();evo.queue(self.root,key);self.module.write_text('customized later');self.assertRaises(ValueError,evo.apply_pending,self.root);self.assertEqual(self.module.read_text(),'customized later')
    def test_active_and_sensitive_confirmation(self):
        key=self.stage(path='.agentic/eng.py',content='print("better")\n',reg=REG.replace('total(4,3),7','total(1,0),1'),kind='improvement');self.assertEqual(evo.validate(self.root,key)['status'],'pass');evo.queue(self.root,key)
        locks=self.agent/'local/locks';locks.mkdir();(locks/'claude-reviewer').mkdir();self.assertIn('active',evo.apply_pending(self.root));(locks/'claude-reviewer').rmdir();self.assertIn('review required',evo.apply_pending(self.root));self.assertIn('applied',evo.apply_pending(self.root,confirm=lambda r:True))
    def test_interrupted_recovery(self):
        key=self.validated();evo.queue(self.root,key);folder=evo.entry(self.root,key);r=evo.read(folder/'record.json');p=evo.store(self.root)
        evo.atomic(folder/'journal-manifest.json',self.orig);evo.put(p/'journal.json',{'id':key,'operation':'apply','previous':r['base'],'previous_active':None});self.module.write_text(GOOD)
        self.assertEqual(evo.apply_pending(self.root),'recovered');self.assertEqual(self.module.read_text(),BAD);self.assertFalse((p/'journal.json').exists())
    def test_startup_failure_recovery(self):
        key=self.validated();evo.queue(self.root,key)
        with patch.object(evo,'run_check',return_value={'exit_code':1}):
            with self.assertRaises(RuntimeError):evo.apply_pending(self.root)
        self.assertEqual(self.module.read_text(),BAD);self.assertFalse((evo.store(self.root)/'journal.json').exists())
    def test_export_excludes_private_and_preserves_root_layout(self):
        import zipfile
        key=self.validated();evo.queue(self.root,key);evo.apply_pending(self.root)
        result=evo.export_toolkit(self.root)
        with zipfile.ZipFile(self.root/result['path']) as z:
            self.assertEqual(set(z.namelist()),set(evo.read(self.agent/'manifest.json')['files'])|{'.agentic/manifest.json'})
            self.assertIsNone(z.testzip())
        self.module.write_text('local modification')
        with self.assertRaises(ValueError):evo.export_toolkit(self.root)
    def test_cancelled_validation_never_queues(self):
        from threading import Event
        key=self.stage();event=Event();event.set();out=evo.validate(self.root,key,cancel_event=event)
        self.assertEqual(out['status'],'fail');self.assertRaises(ValueError,evo.queue,self.root,key)
    def test_chained_rollback_exact_manifest(self):
        first=self.validated();evo.queue(self.root,first);evo.apply_pending(self.root);first_manifest=(self.agent/'manifest.json').read_bytes()
        second=self.stage(content=GOOD+'\ndef more(): return 4\n',reg='import unittest\nimport context_brief\nclass More(unittest.TestCase):\n    def test_more(self): self.assertEqual(context_brief.more(),4)\n',kind='improvement');self.assertEqual(evo.validate(self.root,second)['status'],'pass');evo.queue(self.root,second);evo.apply_pending(self.root);evo.queue(self.root,second,rollback=True);evo.apply_pending(self.root)
        self.assertEqual((self.agent/'manifest.json').read_bytes(),first_manifest);evo.queue(self.root,first,rollback=True);evo.apply_pending(self.root);self.assertEqual(self.module.read_text(),BAD)

class LearningTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.folder=self.root/'.agentic/runs/test-task-0001';self.folder.mkdir(parents=True)
    def test_observations_and_anchored_lessons(self):
        evo.put(self.folder/'state.json',{'stage':'blocked','blocker':'provider_quota'});evo.put(self.folder/'revision-request.json',{'failure_kind':'test_fixture','correction_round':2});evo.put(self.folder/'acceptance-evidence.json',[{'kind':'api','result':'fail'}]);(self.folder/'build.log').write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':123,'cached_input_tokens':10,'output_tokens':22}})+'\n')
        a=learn.collect(self.root);b=learn.collect(self.root);self.assertEqual(a['signals'],b['signals']);self.assertEqual(b['observed_tasks'],1);self.assertEqual(b['usage']['tokens']['input_tokens'],123);self.assertIn('live_api_failure',b['signals'])
        args=dict(trigger='Fixture defect',cause='Invalid schema',prevention='Validate fixture schema',evidence=self.folder.name)
        with self.assertRaises(ValueError):learn.lesson(self.root,**args)
        evo.put(self.folder/'state.json',{'stage':'ready_for_human_release_review','lead_verified':True});a=learn.lesson(self.root,**args);b=learn.lesson(self.root,**args);self.assertEqual(a['lesson_id'],b['lesson_id']);self.assertIn('Validate fixture schema',learn.context(self.root))
    def test_static_hints_no_changes(self):
        p=self.root/'app';p.mkdir();src=p/'logic.py';src.write_text('import math\n\ndef _unused():\n    return 1\n    print(2)\n\ndef _registered():\n    return 2\n\nregistry={"_registered": _registered}\n');(p/'Example.java').write_text('class Example { private int unusedMethod() { return 1; } }');sha=evo.digest(src);r=learn.health(self.root,'app');symbols={x.get('symbol') for x in r['candidates']};self.assertIn('_unused',symbols);self.assertIn('math',symbols);self.assertIn('unusedMethod',symbols);self.assertNotIn('_registered',symbols);self.assertTrue(any(x['kind']=='unreachable_statement_candidate' for x in r['candidates']));self.assertEqual(evo.digest(src),sha)
    def test_auth_and_permissions(self):
        import verification_mcp as vm,eng
        with patch.object(vm,'_authorized_delegate',return_value=False):self.assertEqual(vm.CALLS['learning_report']({})['status'],'blocked')
        self.assertIn('mcp__gaston_verify__stage_evolution',eng.lead_safe_mcp_permission_rules());self.assertNotIn('mcp__gaston_verify__validate_evolution',eng.lead_safe_mcp_permission_rules())

if __name__=='__main__':unittest.main()
