import json
import sys
from unittest.mock import patch

from test_core import RepoCase
from test_worker_progress import ScriptedRouter
from soso.models import Budget, Router
from soso.workspace import worker


class ReadContextTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.source = '\n'.join(f'SOURCE_OLD_{i:03}: ' + 'x' * 20 for i in range(1, 301))
        (self.root / 'ledger.py').write_text(self.source)

    @staticmethod
    def read(start=1, path='ledger.py'):
        return {'tool': 'read', 'path': path, 'start': start, 'lines': 60}

    @staticmethod
    def prior(prompt):
        if '\nPRIOR READS:\n' not in prompt:
            return []
        return json.loads(prompt.split('\nPRIOR READS:\n')[1].split('\nRECENT ACTIONS:')[0])

    def run_actions(self, actions, before=None, **changes):
        router = ScriptedRouter(actions + [{'tool': 'done'}], before)
        result = worker(router, 'Improve ledger', self.root,
                        self.config(max_steps=len(actions) + 1, **changes), lambda *a, **k: None)
        self.assertTrue(result['finished'])
        return router

    def test_exact_pages_survive_list_and_search_without_model_notes(self):
        router = self.run_actions([self.read(1), self.read(61), self.read(121),
                                   {'tool': 'list'}, {'tool': 'search', 'text': 'SOURCE_OLD_001'}])
        prior = self.prior(router.calls[-1]['prompt'])
        lines = self.source.splitlines()
        self.assertEqual([(p['start'], p['end']) for p in prior], [(1, 60), (61, 120), (121, 180)])
        self.assertEqual([p['content'] for p in prior],
                         ['\n'.join(lines[start:start + 60]) for start in (0, 60, 120)])
        # The current read result is not included a second time in prior pages.
        self.assertEqual([p['start'] for p in self.prior(router.calls[3]['prompt'])], [1, 61])

    def test_utf8_cache_is_bounded_and_evicted_page_can_be_read_again(self):
        (self.root / 'ledger.py').write_text('\n'.join('長' * 20 + str(i) for i in range(300)))
        router = self.run_actions([self.read(start) for start in (1, 61, 121, 181, 241)] +
                                  [{'tool': 'list'}, self.read(1)])
        for call in router.calls:
            serialized = json.dumps(self.prior(call['prompt']), ensure_ascii=False, separators=(',', ':'))
            self.assertLessEqual(len(serialized.encode()), 12 * 1024)
        self.assertNotIn(1, [p['start'] for p in self.prior(router.calls[-2]['prompt'])])
        last = json.loads(router.calls[-1]['prompt'].split('LAST RESULT:\n')[1])
        self.assertNotIn('loop_feedback', last)
        self.assertTrue(all(call['attempt'] == 0 for call in router.calls))

    def test_reread_refreshes_lru_position(self):
        (self.root / 'ledger.py').write_text('\n'.join('長' * 20 + str(i) for i in range(300)))
        router = self.run_actions([self.read(start) for start in (1, 61, 121, 1, 181)] + [{'tool': 'list'}])
        self.assertEqual([p['start'] for p in self.prior(router.calls[-1]['prompt'])], [121, 1, 181])
        self.assertEqual(router.calls[4]['attempt'], 1)  # Repeating a visible page remains a stall.

    def test_mutations_remove_only_stale_file_pages_and_excerpts(self):
        for action in ({'tool': 'write', 'path': 'ledger.py', 'content': 'updated'},
                       {'tool': 'replace', 'path': 'ledger.py', 'old': 'SOURCE_OLD_001', 'new': 'updated'},
                       {'tool': 'delete', 'path': 'ledger.py'}):
            with self.subTest(tool=action['tool']):
                (self.root / 'ledger.py').write_text(self.source)
                router = self.run_actions([self.read(), self.read(path='value.txt'), action, {'tool': 'list'}])
                prior = self.prior(router.calls[-1]['prompt'])
                self.assertEqual([p['path'] for p in prior], ['value.txt'])
                self.assertNotIn('SOURCE_OLD_001', router.calls[-1]['prompt'])

    def test_commands_and_checks_invalidate_changed_pages(self):
        for operation in ('run', 'check'):
            with self.subTest(operation=operation):
                (self.root / 'ledger.py').write_text(self.source)
                command = [sys.executable, '-B', '-c',
                           'from pathlib import Path; Path("ledger.py").write_text("updated by command")']
                action = {'tool': 'run', 'argv': command} if operation == 'run' else {'tool': 'check'}
                router = self.run_actions([self.read(), action, {'tool': 'list'}],
                                          allow_exec=True, checks=[command] if operation == 'check' else [])
                self.assertEqual(self.prior(router.calls[-1]['prompt']), [])
                self.assertNotIn('SOURCE_OLD_001', router.calls[-1]['prompt'])

    def test_external_ignored_and_symlink_target_changes_invalidate_pages(self):
        (self.root / '.gitignore').write_text('__pycache__/\nledger.py\n')
        (self.root / 'alias.py').symlink_to('ledger.py')
        for target, delete in (('ledger.py', False), ('alias.py', False), ('alias.py', True)):
            with self.subTest(path=target, delete=delete):
                (self.root / 'ledger.py').write_text(self.source)
                def change(step, root):
                    if step == 2:
                        if delete:
                            (root / 'ledger.py').unlink()
                        else:
                            (root / 'ledger.py').write_text('external edit')
                router = self.run_actions([self.read(path=target), {'tool': 'list'}], before=change)
                self.assertEqual(self.prior(router.calls[-1]['prompt']), [])
                self.assertNotIn('SOURCE_OLD_001', router.calls[-1]['prompt'])

    def test_external_change_does_not_leave_stale_last_read_result(self):
        router = ScriptedRouter([self.read(), {'tool': 'done'}])
        def changed_after_tool(event, **data):
            if event == 'tool':
                (self.root / 'ledger.py').write_text('external edit')
        worker(router, 'Improve ledger', self.root, self.config(max_steps=2), changed_after_tool)
        self.assertNotIn('SOURCE_OLD_001', router.calls[-1]['prompt'])
        self.assertEqual(self.prior(router.calls[-1]['prompt']), [])
        last = json.loads(router.calls[-1]['prompt'].split('LAST RESULT:\n')[1])
        self.assertTrue(last['source_changed'])
        self.assertNotIn('content', last)

    def test_cache_preserves_cheap_worker_eligibility_excluding_classifier_and_planner(self):
        config = self.config(max_steps=6,
            models={name: {'kind': 'command', 'command': [name], 'context_bytes': limit}
                    for name, limit in (('cheap', 8000), ('fallback', 30000), ('triage', 512), ('planner', 256))},
            routes={'worker': ['cheap', 'fallback'], 'planner': ['planner'], 'reviewer': ['planner']},
            routing={'rules': [{'role': 'planner', 'models': ['planner']},
                               {'role': 'reviewer', 'models': ['planner']}],
                     'selector': {'model': 'triage', 'routes': {'code': ['cheap', 'fallback']}},
                     'default': ['cheap', 'fallback']})
        router = Router(config, Budget(20, 30), lambda *a, **k: None)
        actions = iter([self.read(1), self.read(61), self.read(121), {'tool': 'list'}, self.read(1), {'tool': 'done'}])
        calls = []
        def infer(model, prompt, *args):
            name = model['command'][0]
            calls.append((name, prompt))
            return '{"route":"code"}' if name == 'triage' else json.dumps(next(actions))
        with patch.object(router, '_call', side_effect=infer):
            result = worker(router, 'Improve ledger', self.root, config, lambda *a, **k: None)
        self.assertTrue(result['finished'])
        self.assertEqual([name for name, _ in calls], ['triage'] + ['cheap'] * 6)
        prompts = [prompt for name, prompt in calls if name == 'cheap']
        self.assertTrue(any(self.prior(prompt) for prompt in prompts))
        self.assertTrue(all(len(prompt.encode()) <= 8000 for prompt in prompts))
        self.assertNotIn(1, [p['start'] for p in self.prior(prompts[-2])])
        self.assertNotIn('loop_feedback', json.loads(prompts[-1].split('LAST RESULT:\n')[1]))

    def test_oversized_utf8_page_is_not_truncated_or_retained_as_seen(self):
        (self.root / 'large.txt').write_text('長' * 6000)
        router = self.run_actions([self.read(path='value.txt'), self.read(path='large.txt'),
                                   {'tool': 'list'}, self.read(path='large.txt')])
        self.assertEqual([p['path'] for p in self.prior(router.calls[-2]['prompt'])], ['value.txt'])
        last = json.loads(router.calls[-1]['prompt'].split('LAST RESULT:\n')[1])
        self.assertEqual(last['content'], '長' * 6000)
        self.assertNotIn('loop_feedback', last)
        self.assertTrue(all(call['attempt'] == 0 for call in router.calls))

    def test_no_cache_added_when_base_prompt_already_exceeds_worker_limit(self):
        router = self.run_actions([self.read(), {'tool': 'list'}],
                                  models={'test': {'kind': 'command', 'command': ['false'], 'context_bytes': 1}})
        self.assertTrue(all('\nPRIOR READS:\n' not in call['prompt'] for call in router.calls))
