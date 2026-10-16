import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from soso.models import Router, Budget, ModelUnavailable, ModelOutputTruncated


def config():
    return {'models': {n: {'kind': 'command', 'command': [n]} for n in ('qwen', 'fable', 'x', 'triage')},
            'routes': {'worker': ['qwen', 'x']},
            'routing': {'rules': [
                {'contains': ['design', 'branding'], 'not_contains': ['database'], 'models': ['fable']},
                {'role': 'worker', 'regex': r'\b(code|implement)\b', 'models': ['qwen', 'x']}],
                'default': ['x']}}


class RoutingPolicyTests(unittest.TestCase):
    def test_worker_batch_returns_only_first_action_with_explicit_deferral(self):
        cfg = config(); cfg['delivery'] = True
        router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
        response = {'actions': [{'tool': 'write', 'path': 'server.py', 'content': 'code'},
                                {'tool': 'run', 'argv': ['must-not-run']}], 'note': 'Next add tests.'}
        with patch.object(router, '_call', return_value=json.dumps(response)):
            action = router.ask('worker', 'packet', Path.cwd(), task='implement')
        self.assertEqual(action, {'tool': 'write', 'path': 'server.py', 'content': 'code',
                                  'note': 'Next add tests.', '_unexecuted_actions': 1})
        self.assertNotIn('actions', action)

    def test_worker_batch_cannot_hide_pending_work_behind_done(self):
        cfg = config(); cfg['delivery'] = True
        router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
        response = {'actions': [{'tool': 'done'}, {'tool': 'write', 'path': 'later', 'content': 'x'}]}
        with patch.object(router, '_call', side_effect=[json.dumps(response), '{"tool":"list"}']) as call:
            self.assertEqual(router.ask('worker', 'packet', Path.cwd(), task='implement'), {'tool': 'list'})
        self.assertEqual(call.call_count, 2)

    def test_direct_worker_cannot_forge_batch_metadata(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        with patch.object(router, '_call', return_value='{"tool":"list","_unexecuted_actions":999}'):
            self.assertEqual(router.ask('worker', 'packet', Path.cwd(), task='implement'), {'tool': 'list'})

    def test_mixed_direct_and_batch_actions_are_rejected(self):
        cfg = config(); cfg['delivery'] = True
        for tool, wrapped in (('done', False), ('done', True), ('write', False), ('write', True)):
            router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
            response = {'tool': tool, 'actions': [{'tool': 'run', 'argv': ['must-not-run']}]}
            if wrapped:
                response = {'actions': [response]}
            with patch.object(router, '_call', side_effect=[json.dumps(response), '{"tool":"list"}']) as call:
                self.assertEqual(router.ask('worker', 'packet', Path.cwd(), task='implement'), {'tool': 'list'})
                self.assertEqual(call.call_count, 2)

    def test_rules_match_task_not_protocol_and_default_is_explicit(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        calls = []
        def call(model, *args):
            calls.append(model['command'][0]); return '{"tool":"done"}'
        with patch.object(router, '_call', side_effect=call):
            for task in ['branding for a cafe', 'implement a parser', 'database design', 'unknown task']:
                router.ask('worker', 'protocol mentions branding and design', Path.cwd(), task=task)
        self.assertEqual(calls, ['fable', 'qwen', 'x', 'x'])

    def test_classifier_selects_named_route_and_invalid_output_uses_default(self):
        cfg = config()
        cfg['routing']['selector'] = {'model': 'triage', 'prompt': 'Visual tasks use visual.',
                                      'routes': {'visual': ['fable'], 'coding': ['qwen']}}
        router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
        calls = []
        def call(model, *args):
            name = model['command'][0]; calls.append(name)
            return '{"route":"visual"}' if name == 'triage' else '{"tool":"done"}'
        with patch.object(router, '_call', side_effect=call):
            router.ask('worker', 'packet', Path.cwd(), task='Make the screen look elegant')
        self.assertEqual(calls, ['triage', 'fable'])
        with patch.object(router, '_call', side_effect=['{"route":"invented"}', '{"tool":"done"}']) as mocked:
            router.ask('worker', 'packet', Path.cwd(), task='unclear')
        self.assertEqual(mocked.call_args_list[-1].args[0]['command'], ['x'])

    def test_ollama_classifier_constrains_labels_without_mutating_worker(self):
        cfg = config()
        cfg['models']['triage'] = {'kind': 'ollama', 'model': 'small', 'url': 'http://localhost:11434'}
        cfg['routing'] = {'selector': {'model': 'triage', 'routes': {'code': ['qwen'], 'hard': ['x']}}, 'default': ['x']}
        router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
        with patch.object(router, '_call', return_value='{"route":"code"}') as call:
            self.assertEqual(router._select('worker', 'Implement parser', Path.cwd()), ['qwen'])
        self.assertIs(call.call_args.args[0]['think'], False)
        self.assertEqual(call.call_args.args[0]['max_tokens'], 256)
        schema = call.call_args.args[0]['response_schema']
        self.assertEqual(schema['properties']['route']['enum'], ['code', 'hard'])
        self.assertNotIn('response_schema', router.models['triage'])

    def test_temporary_http_failures_are_retryable(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        model = {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small'}
        for status in (0, 408, 425, 429, 500, 502, 503, 504):
            response = {'returncode': 0, 'timed_out': False, 'truncated': False,
                        'output': json.dumps({'status': status})}
            with self.subTest(status=status), patch('soso.models.execute', return_value=response):
                with self.assertRaises(ModelUnavailable) as error:
                    router._call(model, 'test', Path.cwd(), 10)
                self.assertIn('inference', str(error.exception))
        response = {'returncode': 1, 'timed_out': True, 'truncated': False, 'output': ''}
        with patch('soso.models.execute', return_value=response):
            with self.assertRaisesRegex(ModelUnavailable, 'timed out after 10s'):
                router._call(model, 'test', Path.cwd(), 10)

    def test_ollama_transmits_schema_and_thinking_preference(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        schema = {'type': 'object', 'properties': {'route': {'enum': ['code']}}}
        response = {'returncode': 0, 'timed_out': False, 'truncated': False,
                    'output': json.dumps({'status': 200, 'data': {'message': {'content': '{"route":"code"}'}}})}
        with patch('soso.models.execute', return_value=response) as execute:
            router._call({'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'small',
                          'think': False, 'response_schema': schema}, 'classify', Path.cwd(), 10)
        body = json.loads(execute.call_args.kwargs['stdin'])['body']
        self.assertEqual(body['format'], schema)
        self.assertIs(body['think'], False)

    def test_ollama_defaults_thinking_off_preserves_opt_in_and_logs_only_lengths(self):
        thinking = 'private reasoning sentinel'
        content = '{"tool":"done"}'
        response = {'returncode': 0, 'timed_out': False, 'truncated': False,
                    'output': json.dumps({'status': 200, 'data': {'done_reason': 'stop', 'eval_count': 12,
                                         'message': {'thinking': thinking, 'content': content}}})}
        for preference in (None, True, 'high'):
            with self.subTest(think=preference):
                events = []
                router = Router(config(), Budget(10, 30), lambda event, **data: events.append((event, data)))
                model = {'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'qwen'}
                if preference is not None:
                    model['think'] = preference
                with patch('soso.models.execute', return_value=response) as execute:
                    self.assertEqual(router._call(model, 'code', Path.cwd(), 10), content)
                body = json.loads(execute.call_args.kwargs['stdin'])['body']
                self.assertEqual(body['think'], False if preference is None else preference)
                if preference is None:
                    self.assertNotIn('think', model)
                self.assertEqual(events, [('model_response', {'kind': 'ollama', 'output_tokens': 12,
                    'finish_reason': 'stop', 'thinking_chars': len(thinking), 'content_chars': len(content)})])
                self.assertNotIn(thinking, json.dumps(events))
                self.assertNotIn(content, json.dumps(events))

    def test_ollama_truncation_is_reported_before_json_parsing(self):
        events = []
        router = Router(config(), Budget(10, 30), lambda event, **data: events.append((event, data)))
        response = {'returncode': 0, 'timed_out': False, 'truncated': False,
                    'output': json.dumps({'status': 200, 'data': {'done_reason': 'length',
                              'eval_count': 8192, 'message': {'thinking': 'private', 'content': '{"unfinished":'}}})}
        with patch('soso.models.execute', return_value=response) as execute:
            with self.assertRaises(ModelOutputTruncated):
                router._call({'kind': 'ollama', 'url': 'http://localhost:11434', 'model': 'qwen',
                              'context_tokens': 32768}, 'code', Path.cwd(), 10)
        self.assertEqual(json.loads(execute.call_args.kwargs['stdin'])['body']['options']['num_predict'], 8192)
        self.assertEqual(events[0][1]['thinking_chars'], 7)
        self.assertEqual(events[0][1]['content_chars'], len('{"unfinished":'))
        self.assertNotIn('private', json.dumps(events))

    def test_quota_cooldown_persists_and_other_model_keeps_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(); cfg['routing'] = {'default': ['qwen', 'x']}
            cfg['_router_state'] = str(Path(tmp) / 'routing.json')
            until = time.time() + 3600
            router = Router(cfg, Budget(10, 30), lambda *a, **k: None)
            calls = []
            def call(model, *args):
                name = model['command'][0]; calls.append(name)
                if name == 'qwen': raise ModelUnavailable(until)
                return '{"tool":"done"}'
            with patch.object(router, '_call', side_effect=call):
                router.ask('worker', 'p', Path.cwd())
                router.ask('worker', 'p', Path.cwd())
            self.assertEqual(calls, ['qwen', 'x', 'x'])
            restarted = Router(cfg, Budget(10, 30), lambda *a, **k: None)
            with patch.object(restarted, '_call', return_value='{"tool":"done"}') as mocked:
                restarted.ask('worker', 'p', Path.cwd())
            self.assertEqual(mocked.call_args.args[0]['command'], ['x'])
            self.assertEqual(restarted.cooldowns['qwen'], until)

    def test_all_routes_wait_until_earliest_reset_without_dispatch(self):
        router = Router(config(), Budget(10, 30), lambda *a, **k: None)
        router.cooldowns['x'] = time.time() + 60
        with patch.object(router, '_call') as mocked:
            with self.assertRaises(ModelUnavailable) as caught:
                router.ask('worker', 'unknown', Path.cwd())
        mocked.assert_not_called()
        self.assertEqual(caught.exception.retry_at, router.cooldowns['x'])

    def test_unknown_destination_is_rejected(self):
        cfg = config(); cfg['routing']['default'] = ['missing']
        with self.assertRaises(ValueError): Router(cfg, Budget(10, 30), lambda *a, **k: None)

    def test_classifier_cannot_be_hidden_by_catch_all_rules(self):
        cfg = config()
        cfg['routing'] = {'rules': [{'role': role, 'models': ['x']} for role in ('planner', 'worker', 'reviewer')],
                          'selector': {'model': 'triage', 'routes': {'code': ['qwen']}}}
        with self.assertRaisesRegex(ValueError, 'unreachable'):
            Router(cfg, Budget(10, 30), lambda *a, **k: None)


class AiluminateRoutingTests(unittest.TestCase):
    def policy(self):
        return {'models': {n: {'kind': 'ollama', 'model': n} for n in ('triage', 'gemma', 'qwen', 'main')},
                'routes': {'worker': ['gemma'], 'planner': ['main'], 'reviewer': ['main']},
                'routing': {'rules': [{'role': r, 'models': ['main']} for r in ('planner', 'reviewer')],
                            'selector': {'model': 'triage', 'taxonomy': 'ailuminate',
                                         'routes': {'ordinary': ['gemma', 'qwen'], 'sensitive': ['qwen'], 'uncertain': ['qwen']}},
                            'default': ['gemma', 'qwen']}}

    def test_each_class_selects_declared_destination_and_only_calls_classifier(self):
        from unittest.mock import patch
        import json
        for label, expected in [('ordinary', ['gemma', 'qwen']), ('sensitive', ['qwen']), ('uncertain', ['qwen'])]:
            router = Router(self.policy(), Budget(5, 30), lambda *a, **k: None)
            with patch.object(router, '_call', return_value=json.dumps({'route': label})) as call:
                self.assertEqual(router._select('worker', 'A sample task', Path.cwd()), expected)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[0]['model'], 'triage')
                self.assertIn('not a verdict of harmful', call.call_args.args[1])
                self.assertEqual(set(call.call_args.args[0]['response_schema']['properties']['route']['enum']), {'ordinary', 'sensitive', 'uncertain'})

    def test_main_model_roles_bypass_worker_classifier(self):
        from unittest.mock import patch
        router = Router(self.policy(), Budget(5, 30), lambda *a, **k: None)
        with patch.object(router, '_call') as call:
            for role in ('planner', 'reviewer'):
                self.assertEqual(router._select(role, 'A sample task', Path.cwd()), ['main'])
            call.assert_not_called()

    def test_invalid_label_uses_explicit_default(self):
        from unittest.mock import patch
        router = Router(self.policy(), Budget(5, 30), lambda *a, **k: None)
        with patch.object(router, '_call', return_value='{"route":"invented"}'):
            self.assertEqual(router._select('worker', 'sample', Path.cwd()), ['gemma', 'qwen'])

    def test_unknown_taxonomy_or_missing_category_rejected(self):
        for change in ('unknown', 'missing'):
            cfg = self.policy()
            if change == 'unknown': cfg['routing']['selector']['taxonomy'] = 'invented'
            else: del cfg['routing']['selector']['routes']['uncertain']
            with self.assertRaises(ValueError): Router(cfg, Budget(5, 30), lambda *a, **k: None)

class BusyOverflowTests(unittest.TestCase):
    def router(self):
        return Router({'models':{'local':{'kind':'command','command':['local']},
            'cloud':{'kind':'command','command':['cloud']},
            'guard':{'kind':'ollama','model':'llama-guard3:1b','context_bytes':1000}},
            'routes':{'worker':['local'],'planner':['local']},
            'routing':{'busy_overflow':{'primary':'local','model':'cloud','guard':'guard'}}},
            Budget(20,3),lambda *a,**k:None)

    def test_idle_primary_skips_guard_and_cloud_even_on_retry(self):
        r=self.router()
        with patch.object(r,'_call',return_value='{"tool":"done"}') as call:
            r.ask('worker','packet',Path.cwd(),attempt=9)
        self.assertEqual(call.call_args.args[0]['command'],['local'])
        self.assertEqual(call.call_count,1)
        self.assertTrue(all(c.args[0]['command'] == ['local'] for c in call.call_args_list))
        self.assertTrue(r.slots['local'].acquire(False))

    def test_busy_safe_allows_cloud_and_checks_whole_packet(self):
        r=self.router();r.slots['local'].acquire()
        with patch.object(r,'_call',side_effect=['safe','{"tool":"done"}']) as call:
            r.ask('worker','full request',Path.cwd(),task='assignment')
        self.assertEqual(call.call_args_list[1].args[0]['command'],['cloud'])
        self.assertIn('assignment',call.call_args_list[0].args[1])
        self.assertIn('full request',call.call_args_list[0].args[1])
        self.assertFalse(call.call_args_list[0].args[0]['json_mode'])
        r.slots['local'].release()

    def test_non_safe_or_failed_guard_waits_for_local(self):
        for verdict in ['unsafe\nS2','SAFE','safe\nS2','',RuntimeError('offline')]:
            with self.subTest(verdict=verdict):
                r=self.router();r.slots['local'].acquire();calls=[]
                def call(model,*args):
                    if model['kind']=='ollama':
                        calls.append('guard');r.slots['local'].release()
                        if isinstance(verdict,Exception):raise verdict
                        return verdict
                    calls.append(model['command'][0]);return '{"tool":"done"}'
                with patch.object(r,'_call',side_effect=call):r.ask('worker','packet',Path.cwd())
                self.assertEqual(calls,['guard','local'])

    def test_oversized_input_does_not_classify_prefix_or_use_cloud(self):
        r=self.router();r.slots['local'].acquire()
        timer=threading.Timer(.03,r.slots['local'].release);timer.start()
        try:
            with patch.object(r,'_call',return_value='{"tool":"done"}') as call:
                r.ask('worker','x'*1001,Path.cwd())
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0]['command'],['local'])
        finally:timer.join()

    def test_cloud_unavailable_returns_to_local(self):
        r=self.router();r.slots['local'].acquire();calls=[]
        def call(model,*args):
            if model['kind']=='ollama':calls.append('guard');return 'safe'
            n=model['command'][0];calls.append(n)
            if n=='cloud':r.slots['local'].release();raise ModelUnavailable()
            return '{"tool":"done"}'
        with patch.object(r,'_call',side_effect=call):r.ask('worker','packet',Path.cwd())
        self.assertEqual(calls,['guard','cloud','local'])

    def test_local_error_never_triggers_cloud(self):
        r=self.router()
        with patch.object(r,'_call',side_effect=ValueError('bad')) as call:
            with self.assertRaises(RuntimeError):r.ask('worker','packet',Path.cwd())
        self.assertEqual(call.call_count,3)
        self.assertTrue(all(c.args[0]['command'] == ['local'] for c in call.call_args_list))
        self.assertTrue(r.slots['local'].acquire(False))

    def test_native_guard_transport_omits_json_and_thinking(self):
        r=self.router()
        reply={'returncode':0,'timed_out':False,'truncated':False,'output':json.dumps({'status':200,'data':{'message':{'content':'safe'}}})}
        with patch('soso.models.execute',return_value=reply) as call:
            r._call({'kind':'ollama','url':'http://localhost:11434','model':'llama-guard3:1b','json_mode':False,'send_think':False},'packet',Path.cwd(),1)
        body=json.loads(call.call_args.kwargs['stdin'])['body']
        self.assertNotIn('format',body);self.assertNotIn('think',body)
