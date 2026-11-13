import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from soso.dashboard import Dashboard, Monitor, roadmap
from soso.models import Router, Budget

class DashboardTests(unittest.TestCase):
    def fixture(self, root):
        managed=root/'.so-so';managed.mkdir()
        project=managed/'projects/demo/project';project.mkdir(parents=True)
        report=project/'.git/soso/run/report.json';report.parent.mkdir(parents=True)
        (managed/'active.json').write_text(json.dumps({'project':str(project),'report':str(report),'mode':'optimize','status':'running'}))
        report.write_text(json.dumps({'best':90,'baseline':{'score':100},'rounds':[]}))
        return managed,project,report

    def test_setup_calls_are_visible_before_launcher_has_goal_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); managed,project,report=self.fixture(root)
            pool=managed/'pool.json'
            pool.write_text(json.dumps({'models': {'main': {'model': 'Qwen-large', 'url': 'http://192.0.2.1:8091/v1'}}}))
            (managed/'active.json').write_text(json.dumps({'project':str(project), 'status':'running',
                'goal_id':None, 'title':'Create game tetris', 'models_path':str(pool)}))
            folder=project/'.git/soso/goals/20261016-071941-abcdef12';folder.mkdir(parents=True)
            (folder/'state.json').write_text(json.dumps({'source':str(project), 'goal_id':folder.name, 'status':'running'}))
            events=[{'event':'call_started','call_id':'a','role':'architect','model':'main','time':10,'prompt_bytes':369},
                    {'event':'call_finished','call_id':'a','model':'main','time':12,'seconds':2,'status':'returned','input_tokens':124,'output_tokens':343},
                    {'event':'architecture','plan':{'milestones':['Implement game loop']},'time':13},
                    {'event':'model_request','role':'reviewer','model':'main','time':14},
                    {'event':'call_started','call_id':'b','role':'reviewer','model':'main','time':14,'prompt_bytes':800}]
            (folder/'setup-events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
            with patch.object(Monitor,'running',return_value=True), patch('soso.dashboard.time.time',return_value=20):
                state=Monitor(root).snapshot()
            self.assertEqual(state['intent_title'],'Create game tetris')
            self.assertEqual(state['calls'][0]['host'],'192.0.2.1:8091')
            self.assertEqual(state['calls'][0]['elapsed_seconds'],6)
            self.assertEqual(state['calls'][0]['purpose'],'Review acceptance criteria')
            self.assertEqual(state['models'][0]['input_tokens'],124)
            self.assertEqual(state['models'][0]['output_tokens'],343)
            self.assertEqual(state['plan'],['Proposed: Implement game loop'])
            self.assertEqual([s['id'] for s in state['roadmap'] if s['state']=='active'],['plan'])
            self.assertIsNone(state['percent'])
            # Entering an epoch must not resurrect an unfinished setup call.
            entry=json.loads((managed/'active.json').read_text());entry['report']=str(report)
            (managed/'active.json').write_text(json.dumps(entry))
            self.assertEqual(Monitor(root).snapshot()['calls'],[])
            state_path=folder/'state.json'
            goal=json.loads(state_path.read_text());goal['active_epoch']=str(report)
            state_path.write_text(json.dumps(goal))
            entry['report']=str(state_path)
            (managed/'active.json').write_text(json.dumps(entry))
            self.assertEqual(Monitor(root).snapshot()['report'],str(report))

    def test_snapshot_pairs_parallel_calls_and_keeps_unknown_tokens_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            events=[{'event':'call_started','call_id':'a','model':'slow','role':'planner','time':1,'prompt_bytes':100},
                    {'event':'call_started','call_id':'b','model':'fast','role':'worker','time':2,'prompt_bytes':50},
                    {'event':'call_finished','call_id':'b','model':'fast','role':'worker','time':3,'seconds':1,'status':'returned','output_tokens':15},
                    {'event':'plan','tasks':['Implement parser'],'time':4}]
            (report.parent/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n{"partial":')
            state=Monitor(root).snapshot()
            self.assertEqual([c['model'] for c in state['calls']],['slow'])
            fast=next(m for m in state['models'] if m['model']=='fast')
            self.assertEqual(fast['output_tokens_per_second'],15);self.assertEqual(fast['typical_seconds'],1);self.assertEqual(fast['output_tokens'],15);self.assertIsNone(fast['input_tokens'])
            self.assertEqual(state['plan'],['Implement parser']);self.assertIsNone(state['percent']);self.assertIsNone(state['eta_seconds'])

    def test_active_call_has_configured_model_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,report=self.fixture(root)
            pool=managed/'models.json'
            pool.write_text(json.dumps({'models':{'main':{'model':'/models/Qwen.gguf','url':'http://user:secret@192.0.2.1:8091/v1'}}}))
            active=json.loads((managed/'active.json').read_text())
            active['models_path']=str(pool)
            (managed/'active.json').write_text(json.dumps(active))
            (report.parent/'events.jsonl').write_text(json.dumps({'event':'call_started','call_id':'a','model':'main','role':'planner','time':1})+'\n')
            (root/'soso.json').write_text(json.dumps({'models':{'main':{'display_name':'Large planner','host_label':'CPU server','url':'http://wrong.invalid'}}}))
            call=Monitor(root).snapshot()['calls'][0]
            self.assertEqual(call['model_name'],'Qwen.gguf')
            self.assertEqual(call['display_name'],'Large planner')
            self.assertEqual(call['host_label'],'CPU server')
            self.assertEqual(call['host'],'192.0.2.1:8091')

    def test_usage_elapsed_includes_live_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            events=[{'event':'call_started','call_id':'a','model':'main','role':'planner','time':10},
                    {'event':'call_finished','call_id':'a','model':'main','time':12,'seconds':2,'status':'returned','output_tokens':10},
                    {'event':'call_started','call_id':'b','model':'main','role':'planner','time':13}]
            (report.parent/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
            with patch.object(Monitor,'running',return_value=True), patch('soso.dashboard.time.time',return_value=20):
                model=Monitor(root).snapshot()['models'][0]
            self.assertEqual(model['active_seconds'],7)
            self.assertEqual(model['elapsed_seconds'],9)
            self.assertEqual(model['output_tokens_per_second'],5)

    def test_dashboard_uses_configured_binding_and_advertised_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'soso.json').write_text(json.dumps({'dashboard':{'host':'127.0.0.1','public_host':'192.0.2.10'}}))
            with Dashboard(root) as dashboard:
                self.assertEqual(dashboard.server.server_address[0],'127.0.0.1')
                self.assertTrue(dashboard.url.startswith('http://192.0.2.10:'))

    def test_roadmap_distinguishes_baseline_from_candidate_checks(self):
        baseline=[{'event':'activity','phase':'testing','task':'Baseline checks'}]
        stages=roadmap(baseline,{},'running',True,[])
        self.assertEqual([s['id'] for s in stages if s['state']=='active'],['baseline'])
        self.assertEqual(next(s['state'] for s in stages if s['id']=='outcome'),'upcoming')
        stages=roadmap(baseline+[{'event':'activity','phase':'testing','candidate':0}],{},'blocked',False,[])
        self.assertEqual([s['id'] for s in stages if s['state']=='paused'],['verify'])
        self.assertEqual(next(s['state'] for s in stages if s['id']=='outcome'),'upcoming')

    def test_historical_unfinished_call_is_not_live_in_new_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,project,report=self.fixture(root)
            old=report.parent.parent/'old/report.json';old.parent.mkdir();old.write_text('{}')
            (old.parent/'events.jsonl').write_text(json.dumps({'event':'call_started','call_id':'old','model':'main','role':'planner','time':1}))
            (project.parent/'optimization.json').write_text(json.dumps({'epochs':[{'report':str(old)}]}))
            with patch.object(Monitor,'running',return_value=True):
                state=Monitor(root).snapshot()
            self.assertEqual(state['calls'],[])
            self.assertEqual(state['models'][0]['active_seconds'],0)

    def test_stopped_unfinished_call_has_unknown_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            (report.parent/'events.jsonl').write_text(json.dumps({'event':'call_started','call_id':'a','model':'main','role':'planner','time':1}))
            self.assertIsNone(Monitor(root).snapshot()['calls'][0]['elapsed_seconds'])

    def test_rejected_candidate_cannot_complete_milestone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,project,report=self.fixture(root)
            contract=project/'.soso-delivery';contract.mkdir();(contract/'contract.json').write_text(json.dumps({'milestones':['one']}))
            report.write_text(json.dumps({'rounds':[{'accepted':'accepted','experiments':[{'commit':'rejected','review':{'approve':True,'milestone_statuses':[{'index':0,'status':'complete','evidence':'claimed'}]}}]}]}))
            self.assertEqual(Monitor(root).snapshot()['milestones'][0]['status'],'not_reported')

    def test_external_paths_and_symlinks_are_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,_=self.fixture(root)
            secret=root/'outside.json';secret.write_text('{"secret":"hidden"}')
            (managed/'escape.json').symlink_to(secret)
            m=Monitor(root);self.assertEqual(m.read(secret),{});self.assertEqual(m.read(managed/'escape.json'),{})
            (managed/'active.json').write_text(json.dumps({'report':str(secret)}))
            self.assertNotIn('hidden',json.dumps(m.snapshot()))

    def test_http_requires_token_and_serves_no_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,report=self.fixture(root)
            # Every requested target exists, so a 404 reflects the guard rather than a missing file.
            (root/'outside.png').write_bytes(b'outside');(root/'soso.json').write_text('{}')
            (managed/'unlisted.png').write_bytes(b'unlisted')
            result=report.parent/'result';result.mkdir();(result/'screen.png').write_bytes(b'listed image')
            with Dashboard(root) as d:
                self.assertEqual(d.server.server_address[0],'127.0.0.1')
                with urllib.request.urlopen(d.url+'api') as r:
                    self.assertEqual(json.load(r)['best'],90)
                    self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
                for url in [d.url.replace(d.token,'wrong')+'api',d.url+'asset/../outside.png',d.url+'asset/%2e%2e/outside.png',
                            d.url+'../soso.json',d.url+'asset/active.json',d.url+'asset/unlisted.png']:
                    with self.subTest(url=url.replace(d.token,'TOKEN')):
                        with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url)
                        self.assertEqual(caught.exception.code,404);caught.exception.close()
                with urllib.request.urlopen(d.url) as r:self.assertIn(b'Run monitor',r.read())

    def test_listed_result_image_is_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            result=report.parent/'result';result.mkdir();(result/'screen.png').write_bytes(b'listed image')
            with Dashboard(root) as d:
                with urllib.request.urlopen(d.url+'api') as r:
                    images=json.load(r)['images']
                self.assertEqual([x['name'] for x in images],['screen.png'])
                with urllib.request.urlopen(d.url+'asset/'+images[0]['path']) as r:
                    self.assertEqual(r.read(),b'listed image')
                    self.assertEqual(r.headers['Content-Type'],'image/png')

    def test_asset_requests_reuse_recent_image_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,report=self.fixture(root)
            result=report.parent/'result';result.mkdir();(result/'screen.png').write_bytes(b'listed image')
            (managed/'unlisted.png').write_bytes(b'unlisted')
            with Dashboard(root) as d, patch.object(Monitor,'snapshot',autospec=True,side_effect=Monitor.snapshot) as snapshot:
                with urllib.request.urlopen(d.url+'api') as r:path=json.load(r)['images'][0]['path']
                for _ in range(3):
                    with urllib.request.urlopen(d.url+'asset/'+path) as r:self.assertEqual(r.read(),b'listed image')
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(d.url+'asset/.so-so/unlisted.png')
                self.assertEqual(caught.exception.code,404);caught.exception.close()
                self.assertEqual(snapshot.call_count,1)
                with patch.object(Dashboard,'IMAGE_SECONDS',-1):  # an expired inventory is rebuilt
                    with urllib.request.urlopen(d.url+'asset/'+path) as r:self.assertEqual(r.read(),b'listed image')
                self.assertEqual(snapshot.call_count,2)

    def test_listed_image_replaced_by_symlink_is_not_served_from_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,report=self.fixture(root)
            result=report.parent/'result';result.mkdir();(result/'screen.png').write_bytes(b'listed image')
            (managed/'secret.json').write_text('{"token":"x"}')
            with Dashboard(root) as d:
                with urllib.request.urlopen(d.url+'api') as r:path=json.load(r)['images'][0]['path']
                (result/'screen.png').unlink();(result/'screen.png').symlink_to(managed/'secret.json')
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(d.url+'asset/'+path)
                self.assertEqual(caught.exception.code,404);caught.exception.close()

    def test_client_abort_is_not_logged_or_answered(self):
        import contextlib,io
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            with Dashboard(root) as d:
                handler=d.server.RequestHandlerClass
                for route in ('api',''):
                    for error in (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):
                        with self.subTest(route=route,error=error.__name__), contextlib.redirect_stderr(io.StringIO()) as errors, \
                                patch.object(handler,'end_headers',side_effect=error), patch.object(handler,'send_error') as send_error:
                            with self.assertRaises(Exception):urllib.request.urlopen(d.url+route)
                            self.assertEqual(errors.getvalue(),'')
                            send_error.assert_not_called()
                self.assertIsNone(getattr(d,'last_error',None))

    def test_milestone_progress_requires_evidence_from_accepted_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,project,report=self.fixture(root)
            contract=project/'.soso-delivery';contract.mkdir();(contract/'contract.json').write_text(json.dumps({'milestones':['one','two']}))
            report.write_text(json.dumps({'rounds':[{'accepted':'a','experiments':[{'commit':'a','review':{'approve':True,'milestone_statuses':[{'index':0,'status':'complete','evidence':'test_one passed'},{'index':1,'status':'not_started'}]}}]}]}))
            self.assertEqual(Monitor(root).snapshot()['percent'],50)

    def test_intent_metrics_are_planner_defined_and_values_are_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            report.write_text(json.dumps({'best':90,'baseline':{'metrics':{'pages':12}},'rounds':[]}))
            (report.parent/'events.jsonl').write_text(json.dumps({'event':'plan','monitoring':[
                {'label':'Pages indexed','source':'metric:pages','unit':'pages','value':999},
                {'label':'Unknown','source':'metric:missing'},
                {'label':'Forbidden','source':'/etc/passwd'}]})+'\n')
            metrics=Monitor(root).snapshot()['insights']
            self.assertEqual(len(metrics),2);self.assertEqual(metrics[0]['value'],12)
            self.assertEqual(metrics[0]['label'],'Pages indexed');self.assertIsNone(metrics[1]['value'])

    def test_image_inventory_excludes_symlinks_and_active_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            result=report.parent/'result';result.mkdir()
            (result/'screen.png').write_bytes(b'image')
            (result/'script.svg').write_text('<svg/>')
            outside=root/'secret.png';outside.write_bytes(b'secret')
            (result/'link.png').symlink_to(outside)
            self.assertEqual([x['name'] for x in Monitor(root).snapshot()['images']],['screen.png'])

    def test_launcher_lock_distinguishes_saved_running_status(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,_=self.fixture(root)
            with (managed/'launch.lock').open('w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                self.assertTrue(Monitor(root).snapshot()['runner_active'])
            self.assertFalse(Monitor(root).snapshot()['runner_active'])

    def test_malformed_dashboard_settings_never_stop_work(self):
        import contextlib, io
        from soso.dashboard import automatic
        for settings in ({'dashboard': 'x'}, {'dashboard': {'host': 5}}, ['not', 'an', 'object']):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp)/'soso.json').write_text(json.dumps(settings))
                with patch.dict('os.environ', {'SOSO_DASHBOARD': '1'}), contextlib.redirect_stdout(io.StringIO()) as output:
                    with automatic(Path(tmp)) as dashboard:
                        self.assertIsNone(dashboard)
                self.assertIn('Dashboard unavailable', output.getvalue())

    def settings(self, root, dashboard):
        (root/'soso.json').write_text(json.dumps({'dashboard': dashboard}))
        return patch.dict('os.environ', {'SOSO_DASHBOARD_HOST': '', 'SOSO_DASHBOARD_PUBLIC_HOST': ''})

    def test_wildcard_binding_needs_a_public_host_in_every_spelling(self):
        for host, public in (('', None), ('', '192.0.2.10'), ('0', None), ('0x0', None), ('0.0.0.0', None),
                             ('0.0.0.0', '0'), ('::', None), ('::0', None), ('::', '::')):
            with self.subTest(host=host, public=public), tempfile.TemporaryDirectory() as tmp:
                with self.settings(Path(tmp), {'host': host, **({'public_host': public} if public else {})}):
                    with self.assertRaises(Exception) as caught:
                        Dashboard(Path(tmp)).server.server_close()
                    self.assertIsInstance(caught.exception, ValueError)
        with tempfile.TemporaryDirectory() as tmp, self.settings(Path(tmp), {'host': '0', 'public_host': '192.0.2.10'}):
            with Dashboard(Path(tmp)) as dashboard:
                self.assertEqual(dashboard.server.server_address[0], '0.0.0.0')
                self.assertTrue(dashboard.url.startswith('http://192.0.2.10:'))

    def test_ipv6_host_binds_and_is_bracketed_in_the_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            try:
                dashboard=Dashboard(root,host='::1')
            except OSError as exc:
                self.fail(f'IPv6 loopback did not bind: {exc}')
            with dashboard:
                self.assertEqual(dashboard.server.server_address[0],'::1')
                self.assertTrue(dashboard.url.startswith('http://[::1]:'))
                with urllib.request.urlopen(dashboard.url+'api',timeout=10) as r:self.assertEqual(json.load(r)['best'],90)

    def test_attach_mode_reports_bad_settings_in_one_line(self):
        import subprocess,sys
        for text, argv, reason in (('{"dashboard":"lan"}', [], 'must be an object'), ('{', [], 'is not valid JSON'),
                                   ('["dashboard"]', [], 'must be an object'), ('{"dashboard":{"host":5}}', [], 'must be nonempty'),
                                   ('{"dashboard":{"host":"192.0.2.1"}}', [], 'cannot listen on 192.0.2.1'),
                                   ('{}', ['--port', '99999'], 'port')):
            with self.subTest(text=text, argv=argv), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp)/'soso.json').write_text(text)
                env={k:v for k,v in __import__('os').environ.items() if not k.startswith('SOSO_DASHBOARD')}
                result=subprocess.run([sys.executable,'-m','soso.dashboard','--project',tmp,*argv],cwd=Path(__file__).parents[1],
                                      env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,1,result.stderr)
                self.assertNotIn('Traceback',result.stderr)
                self.assertRegex(result.stderr,r'^dashboard: [^\n]+\n$')
                self.assertIn(reason,result.stderr)

    def test_api_tolerates_malformed_records(self):
        start={'event':'call_started','call_id':'a','model':'m','role':'planner','time':1}
        finished={'event':'call_finished','call_id':'a','model':'m','seconds':2,'status':'returned'}
        good={'models':{'m':{'url':'http://192.0.2.1:8091/v1'}}}
        cases={'label not an object':dict(labels={'models':{'m':'label'}}),
               'labels a list':dict(labels={'models':['m']}),
               'labels not an object':dict(labels=['models']),
               'port out of range':dict(pool={'models':{'m':{'url':'http://h:99999/v1'}}}),
               'unclosed IPv6 URL':dict(pool={'models':{'m':{'url':'http://[::1/v1'}}}),
               'URL not a string':dict(pool={'models':{'m':{'url':5}}}),
               'model definition not an object':dict(pool={'models':{'m':'x'}}),
               'pool models a list':dict(pool={'models':['m']}),
               'list call_id':dict(events=[dict(start,call_id=['x'])]),
               'string seconds':dict(events=[start,dict(finished,seconds='x')]),
               'null seconds':dict(events=[start,dict(finished,seconds=None)]),
               'string prompt_bytes':dict(events=[dict(start,prompt_bytes='x')]),
               'string time':dict(events=[dict(start,time='x')]),
               'list model':dict(events=[dict(start,model=['m'])]),
               'list role':dict(events=[dict(start,role=['planner'])]),
               'list tool':dict(events=[{'event':'tool','tool':['read']}]),
               'plan tasks not a list':dict(events=[{'event':'plan','tasks':5}]),
               'architecture plan not an object':dict(events=[{'event':'architecture','plan':5}]),
               'architecture milestones not a list':dict(events=[{'event':'architecture','plan':{'milestones':5}}]),
               'null baseline':dict(report={'baseline':None}),
               'list baseline':dict(report={'baseline':[1]}),
               'rounds not a list':dict(report={'rounds':5}),
               'round not an object':dict(report={'rounds':[5]}),
               'experiments not a list':dict(report={'rounds':[{'accepted':'a','experiments':5}]}),
               'experiment not an object':dict(report={'rounds':[{'accepted':'a','experiments':[5]}]}),
               'evaluation not an object':dict(report={'rounds':[{'accepted':'a','experiments':[{'commit':'a','evaluation':[1]}]}]}),
               'contract not an object':dict(goal={'contract':5}),
               'milestones not a list':dict(goal={'contract':{'milestones':5}}),
               'int active_epoch':dict(goal={'active_epoch':5}),
               'epochs not a list':dict(goal={'active_epoch':None,'epochs':5}),
               'epoch report not a string':dict(goal={'epochs':[{'report':5}]}),
               'intent not a string':dict(goal={'intent':5}),
               'worktree not a string':dict(active={'worktree':5}),
               'project not a string':dict(active={'project':5})}
        for name, case in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);managed,project,report=self.fixture(root)
                active=json.loads((managed/'active.json').read_text())
                (report.parent/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in case.get('events',[start])))
                if 'report' in case:report.write_text(json.dumps(case['report']))
                if 'goal' in case:
                    folder=project/'.git/soso/goals/g1';folder.mkdir(parents=True)
                    (folder/'state.json').write_text(json.dumps({'source':str(project),'goal_id':'g1',**case['goal']}))
                    active['goal_id']='g1'
                (managed/'pool.json').write_text(json.dumps(case.get('pool',good)));active['models_path']=str(managed/'pool.json')
                if 'labels' in case:(root/'soso.json').write_text(json.dumps(case['labels']))
                (managed/'active.json').write_text(json.dumps({**active,**case.get('active',{})}))
                try:
                    state=Monitor(root).snapshot()
                except Exception as exc:
                    self.fail(f'{type(exc).__name__}: {exc}')
                json.dumps(state)
                for key in ('calls','models','plan','milestones','rounds','images','timeline'):
                    self.assertIsInstance(state[key],list,key)
        # A well-formed record next to a bad one keeps its values.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);managed,_,report=self.fixture(root)
            (managed/'pool.json').write_text(json.dumps({'models':{'m':{'url':'http://192.0.2.1:8091/v1'},'n':{'url':'http://h:99999/v1'}}}))
            active=json.loads((managed/'active.json').read_text());active['models_path']=str(managed/'pool.json')
            (managed/'active.json').write_text(json.dumps(active))
            (root/'soso.json').write_text(json.dumps({'models':{'m':{'display_name':'Planner'},'n':'label'}}))
            (report.parent/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in [start,dict(start,call_id='b',model='n'),dict(start,call_id=['x'])]))
            with Dashboard(root) as d, urllib.request.urlopen(d.url+'api',timeout=10) as r:
                calls={c['model']:c for c in json.load(r)['calls']}
            self.assertEqual(set(calls),{'m','n'})
            self.assertEqual((calls['m']['host'],calls['m']['display_name']),('192.0.2.1:8091','Planner'))

    def test_image_inventory_lists_regular_files_only(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            result=report.parent/'result';result.mkdir()
            (result/'screen.png').write_bytes(b'image');os.mkfifo(result/'pipe.png')
            self.assertEqual([x['name'] for x in Monitor(root).snapshot()['images']],['screen.png'])

    def test_listed_image_replaced_by_a_fifo_is_not_waited_on(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,_,report=self.fixture(root)
            result=report.parent/'result';result.mkdir();(result/'screen.png').write_bytes(b'listed image')
            with Dashboard(root) as d:
                with urllib.request.urlopen(d.url+'api',timeout=10) as r:path=json.load(r)['images'][0]['path']
                (result/'screen.png').unlink();os.mkfifo(result/'screen.png')
                # A handler stuck opening the FIFO is released by a writer when the test ends.
                self.addCleanup(lambda:os.close(os.open(result/'screen.png',os.O_WRONLY|os.O_NONBLOCK)) if (result/'screen.png').exists() else None)
                try:
                    urllib.request.urlopen(d.url+'asset/'+path,timeout=5)
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code,404);exc.close()
                except OSError as exc:
                    self.fail(f'the FIFO request did not answer: {exc}')
                else:
                    self.fail('a FIFO was served')

    def test_image_reads_are_bounded_regular_files(self):
        from soso.dashboard import read_image
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'screen.png';image.write_bytes(b'0123456789')
            self.assertEqual(read_image(image,limit=10),b'0123456789')
            with self.assertRaisesRegex(ValueError,'too large'):read_image(image,limit=9)
            (Path(tmp)/'link.png').symlink_to(image)
            with self.assertRaises(OSError):read_image(Path(tmp)/'link.png')

    def test_idle_connections_are_closed(self):
        import socket,time
        with tempfile.TemporaryDirectory() as tmp, patch.object(Dashboard,'REQUEST_SECONDS',0.5):
            root=Path(tmp);self.fixture(root)
            with Dashboard(root) as d, socket.create_connection(d.server.server_address[:2],timeout=10) as idle:
                started=time.monotonic()
                try:
                    self.assertEqual(idle.recv(1),b'')
                except TimeoutError:
                    self.fail('an idle connection was kept open')
                self.assertLess(time.monotonic()-started,5)

class UsageTelemetryTests(unittest.TestCase):
    def test_parallel_usage_is_correlated_and_does_not_log_prompts(self):
        events=[];lock=threading.Lock();barrier=threading.Barrier(2)
        def log(e,**data):
            with lock:events.append({'event':e,**data})
        r=Router({'models':{'m':{'kind':'command','command':['unused']}},'routes':{'worker':['m']}},Budget(10,5),log)
        def invoke(model,prompt,*args):
            r.telemetry.usage={'output_tokens':int(prompt)};barrier.wait();return '{}'
        with patch.object(r,'_call',side_effect=invoke):
            threads=[threading.Thread(target=r._measured_call,args=('worker',str(n),r.models['m'],str(n),Path.cwd(),5)) for n in (3,7)]
            for t in threads:t.start()
            for t in threads:t.join()
        done=[e for e in events if e['event']=='call_finished']
        self.assertEqual({e['model']:e['output_tokens'] for e in done},{'3':3,'7':7})
        self.assertEqual(len({e['call_id'] for e in done}),2)
        self.assertTrue(all('prompt' not in e for e in events))

    def test_failed_call_still_records_duration(self):
        events=[];r=Router({'models':{'m':{'kind':'command','command':['unused']}},'routes':{'worker':['m']}},Budget(2,5),lambda e,**d:events.append({'event':e,**d}))
        with patch.object(r,'_call',side_effect=RuntimeError('secret diagnostic')):
            with self.assertRaises(RuntimeError):r._measured_call('worker','m',r.models['m'],'private prompt',Path.cwd(),5)
        self.assertEqual(events[-1]['status'],'error');self.assertGreaterEqual(events[-1]['seconds'],0)
        self.assertNotIn('secret',json.dumps(events));self.assertNotIn('private prompt',json.dumps(events))
