import json
import sys
from unittest.mock import patch

from test_core import RepoCase
from soso.models import Budget, Router
from soso.workspace import worker


class ScriptedRouter:
    def __init__(self, actions, before=None):
        self.actions = iter(actions)
        self.budget = Budget(100, 30)
        self.calls = []
        self.before = before

    def ask(self, role, prompt, cwd, attempt=0, task=None):
        self.budget.claim()
        self.calls.append({'prompt': prompt, 'attempt': attempt, 'task': task})
        if self.before:
            self.before(len(self.calls), cwd)
        return next(self.actions)


class WorkerProgressTests(RepoCase):
    def setUp(self):
        super().setUp()
        (self.root / 'ledger.py').write_text('\n'.join('line ' + str(i) for i in range(1, 301)))

    @staticmethod
    def read(start=1):
        return {'tool': 'read', 'path': 'ledger.py', 'start': start, 'lines': 60}

    @staticmethod
    def last(call):
        return json.loads(call['prompt'].split('LAST RESULT:\n')[1])

    def run_actions(self, actions, before=None):
        router = ScriptedRouter(actions + [{'tool': 'done'}], before)
        result = worker(router, 'Improve ledger', self.root, self.config(max_steps=len(actions) + 1),
                        lambda *args, **kwargs: None)
        self.assertTrue(result['finished'])
        return router

    def test_repeated_three_page_cycle_has_ranges_feedback_and_escalation(self):
        router = self.run_actions([self.read(1), self.read(61), self.read(121), self.read(1)])
        last = self.last(router.calls[-1])
        self.assertEqual(last['loop_feedback']['next_unread_start'], 181)
        self.assertEqual(last['loop_feedback']['previous_step'], 1)
        self.assertEqual(router.calls[-1]['attempt'], 1)
        recent = json.loads(router.calls[3]['prompt'].split('RECENT ACTIONS:\n')[1].split('\nLAST RESULT:')[0])
        self.assertEqual([(row['start'], row['end']) for row in recent], [(1, 60), (61, 120), (121, 180)])

    def test_new_read_range_is_progress(self):
        router = self.run_actions([self.read(1), self.read(61), self.read(121), self.read(181)])
        self.assertTrue(all(call['attempt'] == 0 for call in router.calls))
        self.assertNotIn('loop_feedback', self.last(router.calls[-1]))
        self.assertEqual(self.last(router.calls[-1])['start'], 181)

    def test_reading_later_section_does_not_claim_earlier_lines_read(self):
        router = self.run_actions([self.read(121), self.read(121)])
        self.assertEqual(self.last(router.calls[-1])['loop_feedback']['next_unread_start'], 1)

    def test_recent_ranges_stay_bounded_without_false_loop_feedback(self):
        router = self.run_actions([{'tool': 'read', 'path': 'ledger.py', 'start': start, 'lines': 10}
                                   for start in range(1, 201, 10)])
        recent = json.loads(router.calls[-1]['prompt'].split('RECENT ACTIONS:\n')[1].split('\nLAST RESULT:')[0])
        self.assertLessEqual(len(recent), 6)
        self.assertLessEqual(len(json.dumps(recent, ensure_ascii=True, separators=(',', ':')).encode()), 1500)
        self.assertNotIn('loop_feedback', self.last(router.calls[-1]))

    def test_successful_mutation_allows_legitimate_reread_and_resets_escalation(self):
        router = self.run_actions([self.read(), self.read(),
                                  {'tool': 'write', 'path': 'ledger.py', 'content': 'changed source'}, self.read()])
        self.assertEqual(router.calls[2]['attempt'], 1)
        self.assertEqual(router.calls[3]['attempt'], 0)
        self.assertEqual(self.last(router.calls[-1])['content'], 'changed source')
        self.assertNotIn('loop_feedback', self.last(router.calls[-1]))

    def test_external_file_change_allows_reread(self):
        def change(step, root):
            if step == 2:
                (root / 'ledger.py').write_text('external edit')
        router = self.run_actions([self.read(), self.read()], change)
        self.assertNotIn('loop_feedback', self.last(router.calls[-1]))
        self.assertEqual(self.last(router.calls[-1])['content'], 'external edit')

    def test_changed_ignored_file_is_not_falsely_reported_as_unchanged(self):
        (self.root / '.gitignore').write_text('__pycache__/\nledger.py\n')
        def change(step, root):
            if step == 2:
                (root / 'ledger.py').write_text('changed ignored input')
        router = self.run_actions([self.read(), self.read()], change)
        self.assertNotIn('loop_feedback', self.last(router.calls[-1]))
        self.assertEqual(self.last(router.calls[-1])['content'], 'changed ignored input')

    def test_failed_protected_write_does_not_reset_detection(self):
        router = self.run_actions([self.read(), self.read(),
                                  {'tool': 'write', 'path': 'evaluate.py', 'content': 'not permitted'}, self.read()])
        self.assertIn('loop_feedback', self.last(router.calls[-1]))
        self.assertEqual(router.calls[-1]['attempt'], 2)

    def test_commands_reset_discovery_only_when_source_changes(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                code = ('from pathlib import Path; Path("ledger.py").write_text("changed by command")'
                        if changed else 'print("unchanged")')
                actions = [self.read(), self.read(),
                           {'tool': 'run', 'argv': [sys.executable, '-B', '-c', code]},
                           self.read(), {'tool': 'done'}]
                router = ScriptedRouter(actions)
                result = worker(router, 'Improve ledger', self.root,
                                self.config(max_steps=5, allow_exec=True), lambda *args, **kwargs: None)
                self.assertTrue(result['finished'])
                self.assertEqual('loop_feedback' in self.last(router.calls[-1]), not changed)
                self.assertEqual(router.calls[-1]['attempt'], 0 if changed else 2)

    def test_escalation_uses_only_the_original_policy_route(self):
        config = self.config(max_steps=3,
                             models={name: {'kind': 'command', 'command': [name]} for name in ('local', 'fallback', 'unrelated')},
                             routes={'worker': ['unrelated']},
                             routing={'rules': [{'contains': ['ledger'], 'models': ['local', 'fallback']}],
                                      'default': ['unrelated']})
        router = Router(config, Budget(10, 30), lambda *args, **kwargs: None)
        actions = iter([self.read(), self.read(), {'tool': 'done'}])
        names = []
        def infer(model, *args):
            names.append(model['command'][0])
            return json.dumps(next(actions))
        with patch.object(router, '_call', side_effect=infer):
            result = worker(router, 'Improve ledger', self.root, config, lambda *args, **kwargs: None)
        self.assertTrue(result['finished'])
        self.assertEqual(names, ['local', 'local', 'fallback'])
