"""Protocol failures consume bounded work without changing routing trust."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from soso.models import Budget, BudgetExceeded, ModelProtocolError, ModelOutputTruncated, Router


class ResponseRepairTests(unittest.TestCase):
    def router(self, calls=10, context=4096):
        self.events = []
        config = {'models': {
            'local': {'kind': 'command', 'command': ['local'], 'context_bytes': context},
            'cloud': {'kind': 'command', 'command': ['cloud']}},
            'routes': {'worker': ['local', 'cloud']}}
        return Router(config, Budget(calls, 30), lambda event, **data: self.events.append((event, data)))

    def test_malformed_then_valid_repairs_same_model_and_counts_calls(self):
        router = self.router()
        with patch.object(router, '_call', side_effect=['{"tool":', '{"tool":"list"}']) as call:
            self.assertEqual(router.ask('worker', 'list files', Path.cwd()), {'tool': 'list'})
        self.assertEqual(router.budget.calls, 2)
        self.assertEqual([c.args[0]['command'] for c in call.call_args_list], [['local'], ['local']])
        self.assertIn('PROTOCOL REPAIR', call.call_args.args[1])
        self.assertEqual(sum(e == 'model_protocol_error' for e, _ in self.events), 1)

    def test_repeated_invalid_json_is_typed_terminal_failure_without_cloud(self):
        router = self.router()
        raw = 'private diagnostic secret-content'
        with patch.object(router, '_call', return_value=raw) as call:
            with self.assertRaisesRegex(ModelProtocolError, 'invalid_json after 3 attempts'):
                router.ask('worker', 'list files', Path.cwd())
        self.assertEqual(router.budget.calls, 3)
        self.assertTrue(all(c.args[0]['command'] == ['local'] for c in call.call_args_list))
        self.assertNotIn(raw, json.dumps(self.events))
        self.assertTrue(all(raw not in c.args[1] for c in call.call_args_list))
        self.assertTrue(router.slots['local'].acquire(blocking=False))

    def test_valid_json_wrong_role_schema_is_repaired_even_outside_delivery(self):
        router = self.router()
        with patch.object(router, '_call', side_effect=['{"tool":"invented"}', '{"tool":"done"}']):
            self.assertEqual(router.ask('worker', 'finish', Path.cwd()), {'tool': 'done'})
        self.assertEqual(router.budget.calls, 2)

    def test_repair_does_not_bypass_call_or_context_budget(self):
        router = self.router(calls=1)
        with patch.object(router, '_call', return_value='broken') as call:
            with self.assertRaises(BudgetExceeded):
                router.ask('worker', 'task', Path.cwd())
        self.assertEqual(call.call_count, 1)
        router = self.router(context=10)
        with patch.object(router, '_call', return_value='broken') as call:
            with self.assertRaisesRegex(ModelProtocolError, 'context budget'):
                router.ask('worker', 'task', Path.cwd())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(router.budget.calls, 1)

    def test_provider_truncation_is_repaired_without_accepting_partial_action(self):
        router = self.router()
        with patch.object(router, '_call', side_effect=[ModelOutputTruncated(), '{"tool":"list"}']):
            self.assertEqual(router.ask('worker', 'task', Path.cwd()), {'tool': 'list'})
        self.assertEqual(router.budget.calls, 2)
        self.assertEqual([d['problem'] for e, d in self.events if e == 'model_protocol_error'], ['output_limit'])

    def test_architect_and_coordinator_roles_validate_their_own_schema(self):
        for role, valid in [('architect', {'milestones': ['deliver'], 'constraints': ['keep tests'], 'acceptance':['tests pass']}),
                            ('coordinator', {'experiments': [{'task': 'small change'}]})]:
            router = self.router()
            with patch.object(router, '_call', side_effect=['{"tool":"done"}', json.dumps(valid)]):
                self.assertEqual(router.ask(role, 'plan', Path.cwd()), valid)
            self.assertEqual(router.budget.calls, 2)

    def test_null_response_is_repaired_and_bounded(self):
        router = self.router()
        with patch.object(router, '_call', side_effect=[None, '{"tool":"done"}']):
            self.assertEqual(router.ask('worker', 'finish', Path.cwd()), {'tool': 'done'})
        self.assertEqual(router.budget.calls, 2)
        with patch.object(router, '_call', return_value=None):
            with self.assertRaises(ModelProtocolError):
                router.ask('worker', 'finish', Path.cwd())
        self.assertEqual(router.budget.calls, 5)

    def test_invalid_experiment_item_is_repaired(self):
        router = self.router()
        valid = {'experiments': [{'task': 'small change'}]}
        with patch.object(router, '_call', side_effect=['{"experiments":[null]}', json.dumps(valid)]):
            self.assertEqual(router.ask('coordinator', 'plan', Path.cwd()), valid)
        self.assertEqual(router.budget.calls, 2)

    def test_milestone_objects_get_precise_safe_repair(self):
        router = self.router()
        invalid = {'milestones': [{'title': 'private project detail'}]}
        valid = {'milestones': ['deliver behavior'], 'evaluator': 'source'}
        with patch.object(router, '_call', side_effect=[json.dumps(invalid), json.dumps(valid)]) as call:
            self.assertEqual(router.ask('coordinator', 'Create acceptance contract', Path.cwd()), valid)
        repair = call.call_args.args[1]
        self.assertIn('milestones must be a nonempty array of strings, not objects', repair)
        self.assertNotIn('private project detail', repair)
        errors = [data for event, data in self.events if event == 'model_protocol_error']
        self.assertIn('not objects', errors[0]['detail'])
        self.assertNotIn('private project detail', json.dumps(errors))
