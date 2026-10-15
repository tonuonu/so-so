import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_core import RepoCase
from soso.delivery import _bound
from soso.models import Budget, ModelUnavailable, Router
from soso.process import git


class QueuedQuotaTests(unittest.TestCase):
    def test_queued_request_observes_new_provider_cooldown(self):
        self.check_queued_requests(selector=False)

    def test_queued_classifier_observes_new_provider_cooldown(self):
        self.check_queued_requests(selector=True)

    def check_queued_requests(self, selector):
        with tempfile.TemporaryDirectory() as folder:
            config = {'delivery': True,
                      'models': {'limited': {'kind': 'command', 'parallel': 1}},
                      'routes': {'worker': ['limited']},
                      '_router_state': str(Path(folder) / 'router.json')}
            if selector:
                config['models']['available'] = {'kind': 'command', 'parallel': 2}
                config['routes']['worker'] = ['available']
                config['routing'] = {'selector': {'model': 'limited',
                                                 'routes': {'normal': ['available']}}}
            router = Router(config, Budget(5, 10), lambda *args, **kwargs: None)
            entered = threading.Event()
            queued = threading.Event()
            release = threading.Event()
            slot = router.slots['limited']

            class ObservedSlot:
                def acquire(self, **kwargs):
                    if threading.current_thread().name == 'queued-request':
                        queued.set()
                    return slot.acquire(**kwargs)

                def release(self):
                    slot.release()

            router.slots['limited'] = ObservedSlot()
            until = time.time() + 86400
            outcomes = []
            limited_calls = []

            def limited(model, *args):
                if model is not router.models['limited']:
                    return '{"tool":"done"}'
                limited_calls.append(True)
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test did not release the provider call')
                raise ModelUnavailable(until)

            def request():
                try:
                    outcomes.append(router.ask('worker', 'request', Path(folder)))
                except Exception as exc:
                    outcomes.append(exc)

            first = threading.Thread(target=request, name='first-request')
            second = threading.Thread(target=request, name='queued-request')
            with patch.object(router, '_call', side_effect=limited) as provider:
                try:
                    first.start()
                    self.assertTrue(entered.wait(5))
                    second.start()
                    self.assertTrue(queued.wait(5))
                finally:
                    release.set()
                    first.join(5)
                    if second.ident is not None:
                        second.join(5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(len(limited_calls), 1)
                self.assertEqual(provider.call_count, 3 if selector else 1)
            self.assertEqual(router.budget.calls, 3 if selector else 1)
            self.assertEqual(len(outcomes), 2)
            if selector:
                self.assertEqual(outcomes, [{'tool': 'done'}, {'tool': 'done'}])
            else:
                self.assertTrue(all(isinstance(exc, ModelUnavailable) for exc in outcomes))
                self.assertTrue(all(exc.retry_at == until for exc in outcomes))
            persisted = json.loads(Path(config['_router_state']).read_text())
            self.assertEqual(persisted['cooldowns']['limited'], until)


class SymlinkSnapshotTests(RepoCase):
    def test_baseline_accepts_ordinary_tracked_internal_symlink(self):
        link = self.root / 'alias.txt'
        link.symlink_to('value.txt')
        git(self.root, 'add', 'alias.txt')
        git(self.root, 'commit', '-m', 'Add internal source alias')
        head = git(self.root, 'rev-parse', 'HEAD')
        result = _bound(self.root, self.config(), Budget(1, 10))
        self.assertTrue(result['valid'])
        self.assertEqual(result['score'], 0)
        self.assertEqual(result['head'], head)
        self.assertEqual(link.readlink(), Path('value.txt'))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')

    def test_evaluator_cannot_retarget_untracked_symlink(self):
        # Git status remains "?? alias.txt" for either target, so the source
        # snapshot must catch this change independently of the status guard.
        link = self.root / 'alias.txt'
        link.symlink_to('value.txt')
        before = git(self.root, 'status', '--porcelain')
        command = [sys.executable, '-c',
                   'from pathlib import Path; p=Path("alias.txt"); p.unlink(); '
                   'p.symlink_to("evaluate.py"); print(\'{"score":0}\')']
        with self.assertRaisesRegex(ValueError, 'evaluation modified source'):
            _bound(self.root, self.config(evaluate=command), Budget(1, 10))
        self.assertEqual(git(self.root, 'status', '--porcelain'), before)
        self.assertEqual(link.readlink(), Path('evaluate.py'))


if __name__ == '__main__':
    unittest.main()
