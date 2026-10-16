import json
from pathlib import Path
import tempfile
import unittest

from soso.architecture import planning_reply


class FakeRouter:
    def __init__(self):
        self.calls = []
        self.escalate = False
        self.on_coordinator = None

    def log(self, *args, **kwargs):
        pass

    def ask(self, role, prompt, cwd, task=None):
        self.calls.append(role)
        if role == 'architect':
            return {'milestones': ['Build core behavior'], 'constraints': ['Keep fixed checks'],
                    'acceptance': ['Demonstrate behavior']}
        if self.on_coordinator:
            self.on_coordinator()
        return {'experiments': [{'task': 'Implement small step'}],
                **({'escalation': {'reason': 'Two evaluated approaches contradict the plan'}} if self.escalate else {})}


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.router = FakeRouter()
        self.config = {'models': {'main': {'kind': 'command'}, 'fast': {'kind': 'command'}},
                       'routes': {'architect': ['main'], 'coordinator': ['fast'], 'planner': ['fast']}}

    def ask(self, feedback=()):
        return planning_reply(self.router, self.config, 'Build useful software', self.root,
                              self.root, 'Return experiments JSON', feedback)

    def test_architect_once_across_independent_calls_and_epochs(self):
        self.ask()
        self.router = FakeRouter()
        self.ask()
        self.assertEqual(self.router.calls, ['coordinator'])

    def test_legacy_routes_do_not_create_artifact(self):
        del self.config['routes']['architect']
        self.ask()
        self.assertEqual(self.router.calls, ['planner'])
        self.assertFalse((self.root / 'architecture').exists())

    def test_requires_explicit_coordinator(self):
        del self.config['routes']['coordinator']
        with self.assertRaisesRegex(ValueError, 'coordinator'):
            self.ask()

    def test_malformed_worker_output_does_not_escalate(self):
        self.router.escalate = True
        self.ask([{'accepted': None, 'experiments': [{'error': 'JSONDecodeError'}]}] * 5)
        self.assertEqual(self.router.calls, ['architect', 'coordinator'])

    def test_explicit_revision_is_bounded_and_durable(self):
        feedback = [{'accepted': None, 'experiments': [{'evaluation': {'valid': True, 'score': 1}}]}] * 2
        self.router.escalate = True
        self.ask(feedback)
        self.assertEqual(self.router.calls, ['architect', 'coordinator', 'architect', 'coordinator'])
        self.ask(feedback)
        self.assertEqual(self.router.calls.count('architect'), 2)

    def test_delivery_epoch_feedback_allows_one_revision(self):
        self.router.escalate = True
        prior = {'accepted': None, 'experiments': [{'evaluation': {'valid': True}}]}
        self.ask([{'feedback': [prior]}, prior])
        self.assertEqual(self.router.calls.count('architect'), 2)

    def test_missing_evaluation_does_not_escalate(self):
        self.router.escalate = True
        self.ask([{'accepted': None, 'experiments': [{'evaluation': None}]}] * 2)
        self.assertEqual(self.router.calls.count('architect'), 1)

    def test_successful_round_does_not_support_escalation(self):
        self.router.escalate = True
        self.ask([{'accepted': 'candidate', 'experiments': [{'evaluation': {'valid': True}}]}] * 2)
        self.assertEqual(self.router.calls.count('architect'), 1)

    def test_policy_changes_get_new_architecture(self):
        self.ask()
        self.config['models']['main']['model'] = 'another-model'
        self.ask()
        self.assertEqual(self.router.calls.count('architect'), 2)

    def test_tampered_binding_is_rejected(self):
        self.ask()
        path = next((self.root / 'architecture').glob('*.json'))
        record = json.loads(path.read_text())
        record['binding'] = 'wrong'
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'binding'):
            self.ask()

    def test_coordinator_cannot_modify_artifact(self):
        self.ask()
        path = next((self.root / 'architecture').glob('*.json'))
        self.router.on_coordinator = lambda: path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'modified'):
            self.ask()

    def test_unbounded_strategy_is_not_saved(self):
        self.router.ask = lambda *args, **kwargs: {'milestones': ['x' * 10000], 'constraints': [], 'acceptance': ['test']}
        with self.assertRaisesRegex(ValueError, 'short strings'):
            self.ask()
        self.assertEqual(list((self.root / 'architecture').glob('*.json')), [])

    def test_symlink_artifact_is_rejected(self):
        self.ask()
        path = next((self.root / 'architecture').glob('*.json'))
        other = self.root / 'other.json'
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.ask()
