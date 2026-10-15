import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from soso.dashboard import Dashboard, Monitor
from soso.models import Router, Budget

class DashboardTests(unittest.TestCase):
    def fixture(self, root):
        managed=root/'.so-so';managed.mkdir()
        project=managed/'projects/demo/project';project.mkdir(parents=True)
        report=project/'.git/soso/run/report.json';report.parent.mkdir(parents=True)
        (managed/'active.json').write_text(json.dumps({'project':str(project),'report':str(report),'mode':'optimize','status':'running'}))
        report.write_text(json.dumps({'best':90,'baseline':{'score':100},'rounds':[]}))
        return managed,project,report

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
            self.assertEqual(fast['output_tokens'],15);self.assertIsNone(fast['input_tokens'])
            self.assertEqual(state['plan'],['Implement parser']);self.assertIsNone(state['percent']);self.assertIsNone(state['eta_seconds'])

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
            root=Path(tmp);self.fixture(root)
            with Dashboard(root) as d:
                self.assertEqual(d.server.server_address[0],'127.0.0.1')
                with urllib.request.urlopen(d.url+'api') as r:
                    self.assertEqual(json.load(r)['best'],90)
                    self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
                for url in [d.url.replace(d.token,'wrong')+'api',d.url+'asset/../../outside.png',d.url+'../soso.json']:
                    with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(url)
                with urllib.request.urlopen(d.url) as r:self.assertIn(b'Run monitor',r.read())

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
