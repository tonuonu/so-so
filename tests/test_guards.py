import contextlib
import io
import json
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_core
from test_core import RepoCase
from soso.engine import Run
from soso.models import Budget, BudgetExceeded, Router
from soso.workspace import worker

class GuardTests(RepoCase):
    def test_baseline_cannot_modify_source(self):
        command=[sys.executable,'-c','from pathlib import Path; Path("value.txt").write_text("7"); print(\'{"score":7}\')']
        runner=Run(self.root,self.config(evaluate=command),'Improve')
        with contextlib.redirect_stdout(io.StringIO()): report=runner.run()
        self.assertEqual(report['status'],'invalid_baseline')
        self.assertFalse(report['baseline']['valid'])
        self.assertEqual((self.root/'value.txt').read_text(),'0')

    def test_evaluation_cannot_change_commit(self):
        code = """from pathlib import Path
import subprocess,json
n=int(Path('value.txt').read_text())
if n:
    Path('value.txt').write_text('9')
    subprocess.run(['git','add','value.txt'],check=True)
    subprocess.run(['git','-c','commit.gpgsign=false','commit','-m','unexpected eval edit'],check=True)
print(json.dumps({'score':9 if n else 0}))
"""
        runner=Run(self.root,self.config(evaluate=[sys.executable,'-c',code]),'Improve')
        with patch.object(Router,'ask',test_core.EngineTests.actions),contextlib.redirect_stdout(io.StringIO()):
            report=runner.run()
        self.assertEqual(report['best'],0)
        self.assertTrue(all(not e['evaluation']['valid'] for e in report['rounds'][0]['experiments']))

    def test_resume_keeps_absolute_deadline(self):
        config=self.config(rounds=1)
        runner=Run(self.root,config,'Improve')
        with patch.object(Router,'ask',test_core.EngineTests.actions),contextlib.redirect_stdout(io.StringIO()):
            report=runner.run()
        expiry=report['deadline_unix']
        time.sleep(.05)
        resumed=Run(self.root,config,'Improve',resume=runner.run_id)
        with contextlib.redirect_stdout(io.StringIO()): resumed.run()
        self.assertEqual(resumed.report['deadline_unix'],expiry)
        self.assertAlmostEqual(time.time()+resumed.budget.remaining(),expiry,delta=.15)

    def test_check_obeys_shared_deadline(self):
        checks=[[sys.executable,'-c','import time; time.sleep(.2)']]*3
        config=self.config(checks=checks,max_steps=2)
        router=Router(config,Budget(10,.3),lambda *a,**kw:None)
        started=time.monotonic()
        with patch.object(Router,'ask',return_value={'tool':'check'}):
            with self.assertRaises(BudgetExceeded):
                worker(router,'test',self.root,config,lambda *a,**kw:None)
        self.assertLess(time.monotonic()-started,.8)


class WorkerMemoryTests(RepoCase):
    def test_read_survives_intervening_list_without_model_note(self):
        prompts = []
        actions = iter([{'tool': 'read', 'path': 'value.txt'}, {'tool': 'list'}, {'tool': 'done'}])
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            prompts.append(prompt)
            return next(actions)
        router = Router(self.config(), Budget(5, 5), lambda *a, **k: None)
        with patch.object(Router, 'ask', answer):
            worker(router, 'improve value', self.root, self.config(), lambda *a, **k: None)
        history = json.loads(prompts[-1].split('RECENT ACTIONS:\n')[1].split('\nLAST RESULT:')[0])
        self.assertEqual([entry['tool'] for entry in history], ['read', 'list'])
        self.assertEqual(history[0]['path'], 'value.txt')
        self.assertEqual(history[0]['status'], 'ok')
        self.assertEqual(history[0]['read_excerpt'], '0')
        self.assertIn('proceed to implementation', prompts[-1])

    def test_recent_action_history_has_byte_and_count_bounds(self):
        prompts = []
        count = 0
        def answer(router, role, prompt, cwd, attempt=0, task=None):
            nonlocal count
            prompts.append(prompt)
            count += 1
            return {'tool': 'read', 'path': ('長' * 80) + str(count)} if count <= 9 else {'tool': 'done'}
        config = self.config(max_steps=10)
        router = Router(config, Budget(15, 5), lambda *a, **k: None)
        with patch.object(Router, 'ask', answer):
            worker(router, 'improve', self.root, config, lambda *a, **k: None)
        for prompt in prompts:
            text = prompt.split('RECENT ACTIONS:\n')[1].split('\nLAST RESULT:')[0]
            self.assertLessEqual(len(text.encode()), 1500)
            self.assertLessEqual(len(json.loads(text)), 6)
        self.assertTrue(json.loads(prompts[-1].split('RECENT ACTIONS:\n')[1].split('\nLAST RESULT:')[0]))
