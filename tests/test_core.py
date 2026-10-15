import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from soso.engine import Run, DEFAULTS, evaluate, fingerprint, load_config
from soso.models import Budget, BudgetExceeded, Router, object_json
from soso.process import execute, git
from soso.workspace import Workspace

class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Test')
        git(self.root, 'config', 'user.email', 'test@example.invalid')
        (self.root/'.gitignore').write_text('__pycache__/\n')
        (self.root/'value.txt').write_text('0')
        (self.root/'evaluate.py').write_text("import json\nfrom pathlib import Path\nprint(json.dumps({'score':int(Path('value.txt').read_text())}))\n")
        git(self.root,'add','--all')
        git(self.root,'commit','-m','fixture')
    def tearDown(self):
        self.tmp.cleanup()
    def config(self, **changes):
        return {**DEFAULTS,'models':{'test':{'kind':'command','command':['false'],'parallel':2}},
                'routes':{'worker':['test']},'evaluate':[sys.executable,'evaluate.py'],
                'rounds':3,'patience':1,'max_steps':5,'max_calls':40,**changes}

class WorkspaceTests(RepoCase):
    def test_traversal(self):
        ws=Workspace(self.root,[])
        for name in ('../x','/tmp/x','.git/config','a/../../x'):
            with self.assertRaises(ValueError): ws.path(name)
    def test_symlink_escape(self):
        (self.root/'escape').symlink_to('/tmp')
        with self.assertRaises(ValueError): Workspace(self.root,[]).path('escape/file')
    def test_protection(self):
        ws=Workspace(self.root,['evaluate.py'])
        with self.assertRaises(PermissionError): ws.tool({'tool':'write','path':'evaluate.py','content':''})
        with self.assertRaises(PermissionError): ws.tool({'tool':'run','argv':['true']})
    def test_nested_instructions(self):
        (self.root/'AGENTS.md').write_text('root rules')
        (self.root/'src').mkdir()
        (self.root/'src/AGENTS.md').write_text('nested rules')
        ws=Workspace(self.root,[])
        action={'tool':'write','path':'src/new.py','content':'x=1'}
        result=ws.tool(action)
        self.assertTrue(result['retry_required'])
        self.assertIn('root rules',result['instructions'])
        self.assertIn('nested rules',result['instructions'])
        self.assertFalse((self.root/'src/new.py').exists())
        self.assertTrue(ws.tool(action)['ok'])
    def test_unique_replace(self):
        (self.root/'value.txt').write_text('xx')
        ws=Workspace(self.root,[])
        with self.assertRaises(ValueError): ws.tool({'tool':'replace','path':'value.txt','old':'x','new':'a'})
        ws.tool({'tool':'replace','path':'value.txt','old':'xx','new':'ok'})
        self.assertEqual((self.root/'value.txt').read_text(),'ok')
    def test_search_and_read(self):
        ws=Workspace(self.root,[])
        self.assertEqual(ws.tool({'tool':'search','text':'score'})['hits'][0]['path'],'evaluate.py')
        self.assertEqual(ws.tool({'tool':'read','path':'evaluate.py','start':2,'lines':1})['content'],'from pathlib import Path')
    def test_deleted_protected_fails_closed(self):
        self.assertIn('evaluate.py',fingerprint(self.root,['evaluate.py']))
        (self.root/'evaluate.py').unlink()
        with self.assertRaises(OSError): fingerprint(self.root,['evaluate.py'])

class ProcessTests(unittest.TestCase):
    def test_timeout(self):
        result=execute([sys.executable,'-c','import time; time.sleep(5)'],'.',timeout=.1)
        self.assertTrue(result['timed_out'])
        self.assertLess(result['seconds'],3)
    def test_bounded_output(self):
        result=execute([sys.executable,'-c','print("x"*1000000)'],'.',limit=512)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['output']),512)
    def test_stdin_exit(self):
        result=execute([sys.executable,'-c','import sys; print(sys.stdin.read()); sys.exit(3)'],'.',stdin='hello')
        self.assertEqual(result['returncode'],3)
        self.assertIn('hello',result['output'])
    def test_descendant_cleanup(self):
        start=time.monotonic()
        result=execute([sys.executable,'-c','import subprocess; subprocess.Popen(["sleep","10"])'],'.',timeout=2)
        self.assertEqual(result['returncode'],0)
        self.assertLess(time.monotonic()-start,3)

class ModelTests(unittest.TestCase):
    def test_json(self):
        self.assertEqual(object_json('```json\n{"x":1}\n```'),{'x':1})
        for text in ('[]','junk {"x":1}','{"x":1} junk'):
            with self.assertRaises(ValueError): object_json(text)
    def test_atomic_budget(self):
        budget=Budget(3,10)
        outcomes=[]
        def claim():
            try: budget.claim(); outcomes.append(True)
            except BudgetExceeded: outcomes.append(False)
        threads=[threading.Thread(target=claim) for _ in range(10)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(outcomes),3)
    def test_fallback(self):
        config={'models':{'bad':{'kind':'command','command':['false']},'ok':{'kind':'command','command':[sys.executable,'-c','print(\'{"ok":true}\')']}},'routes':{'worker':['bad','ok']}}
        budget=Budget(5,10)
        self.assertEqual(Router(config,budget,lambda *a,**kw:None).ask('worker','test','.'),{'ok':True})
        self.assertEqual(budget.calls,2)
    def test_context_limit(self):
        config={'models':{'x':{'kind':'command','command':['false'],'context_bytes':3}},'routes':{'worker':['x']}}
        budget=Budget(1,10)
        with self.assertRaises(RuntimeError): Router(config,budget,lambda *a,**kw:None).ask('worker','long','.')
        self.assertEqual(budget.calls,0)

class EngineTests(RepoCase):
    @staticmethod
    def actions(router,role,prompt,cwd,attempt=0,task=None):
        router.budget.claim()
        if role=='reviewer': return {'approve':True}
        if role=='planner': return {'experiments':[{'task':'improve value'},{'task':'another improvement'}]}
        last=json.loads(prompt.split('LAST RESULT:\n')[1])
        if 'files' in last: return {'tool':'read','path':'value.txt'}
        if 'content' in last: return {'tool':'write','path':'value.txt','content':'2' if 'CANDIDATE 1;' in prompt else '1'}
        return {'tool':'done','summary':'updated'}
    def run_fixture(self,**changes):
        runner=Run(self.root,self.config(**changes),'Improve value')
        with patch.object(Router,'ask',self.actions),contextlib.redirect_stdout(io.StringIO()): report=runner.run()
        return runner,report
    def test_parallel_selection_plateau_and_source(self):
        base=git(self.root,'rev-parse','HEAD')
        runner,report=self.run_fixture()
        self.assertEqual(report['best'],2)
        self.assertEqual(report['status'],'plateau')
        self.assertEqual(len(report['rounds'][0]['experiments']),2)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),base)
        self.assertEqual((self.root/'value.txt').read_text(),'0')
        self.assertEqual((runner.integration/'value.txt').read_text(),'2')
        self.assertEqual(git(self.root,'status','--porcelain'),'')
    def test_minimize(self):
        _,report=self.run_fixture(direction='min')
        self.assertEqual(report['best'],0)
    def test_delta(self):
        _,report=self.run_fixture(min_delta=2.0)
        self.assertEqual(report['best'],0)
    def test_fixed_check(self):
        command=[sys.executable,'-c','from pathlib import Path; assert Path("value.txt").read_text()=="0"']
        _,report=self.run_fixture(checks=[command])
        self.assertEqual(report['best'],0)
    def test_planner_review(self):
        _,report=self.run_fixture(routes={'worker':['test'],'planner':['test'],'reviewer':['test']})
        self.assertEqual(report['best'],2)
        self.assertTrue(any(e.get('review',{}).get('approve') for e in report['rounds'][0]['experiments']))
    def test_invalid_baseline(self):
        _,report=self.run_fixture(evaluate=[sys.executable,'-c','print("bad")'])
        self.assertEqual(report['status'],'invalid_baseline')
    def test_dirty(self):
        (self.root/'value.txt').write_text('dirty')
        with self.assertRaises(ValueError): self.run_fixture()
    def test_tampered_evaluator(self):
        def tamper(router,role,prompt,cwd,attempt=0,task=None):
            if 'LAST RESULT:\n{"files"' in prompt: return {'tool':'run','argv':[sys.executable,'-c','from pathlib import Path; Path("evaluate.py").write_text("print(999)")']}
            return {'tool':'done'}
        runner=Run(self.root,self.config(),'Improve',allow_exec=True)
        with patch.object(Router,'ask',tamper),contextlib.redirect_stdout(io.StringIO()): report=runner.run()
        self.assertEqual(report['best'],0)
        self.assertTrue(all('protected files changed' in e['error'] for e in report['rounds'][0]['experiments']))
    def test_resume(self):
        runner,report=self.run_fixture(rounds=1)
        resumed=Run(self.root,self.config(rounds=1),'Improve value',resume=runner.run_id)
        with contextlib.redirect_stdout(io.StringIO()): result=resumed.run()
        self.assertEqual(result['calls'],report['calls'])
        self.assertEqual(result['best'],2)
        with self.assertRaises(ValueError): Run(self.root,self.config(rounds=1),'Different',resume=runner.run_id).run()
    def test_nan(self):
        result=evaluate(self.root,self.config(evaluate=[sys.executable,'-c','print(\'{"score":NaN}\')']),Budget(1,10))
        self.assertFalse(result['valid'])
    def test_budget_exhaustion_is_run_status(self):
        _, report = self.run_fixture(max_calls=1, parallel=1)
        self.assertEqual(report['status'], 'budget_exhausted')
        self.assertEqual(len(report['rounds']), 1)

    def test_elapsed_includes_baseline(self):
        command = [sys.executable, '-c', 'import time; time.sleep(.2); print(\'{"score":0}\')']
        started = time.monotonic()
        _, report = self.run_fixture(evaluate=command, rounds=1)
        self.assertLess(time.monotonic() - started - report['elapsed'], .3)

    def test_try_next_candidate_when_review_rejects(self):
        def review(router, role, prompt, cwd, attempt=0, task=None):
            if role == 'reviewer':
                return {'approve': '+2' not in prompt, 'reason':'reject top candidate'}
            return self.actions(router, role, prompt, cwd, attempt)
        runner = Run(self.root, self.config(rounds=1, routes={'worker':['test'], 'reviewer':['test']}), 'Improve')
        with patch.object(Router, 'ask', review), contextlib.redirect_stdout(io.StringIO()):
            report = runner.run()
        self.assertEqual(report['best'], 1)

    def test_config(self):
        path=self.root/'config.json'
        path.write_text(json.dumps(self.config(parallel=0)))
        with self.assertRaises(ValueError): load_config(path)

if __name__=='__main__': unittest.main()
