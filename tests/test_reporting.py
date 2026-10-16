import json
from pathlib import Path
import tempfile
import unittest
from soso.reporting import summary_lines

class SummaryTests(unittest.TestCase):
    def test_protocol_failure_explains_no_evaluation_and_next_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'report.json'
            report.write_text(json.dumps({'baseline':{'score':10},'best':10,'rounds':[
                {'round':1,'accepted':None,'experiments':[{'task':'Simplify parser','error':'local: malformed JSON after 3 attempts','evaluation':{'valid':False}}]}]}))
            lines='\n'.join(summary_lines({'status':'blocked','report':str(report)}))
            self.assertIn('0 valid evaluations; 0 accepted changes; 1 execution failures',lines)
            self.assertIn('malformed JSON after 3 attempts',lines)
            self.assertIn('Next action:',lines)
            self.assertNotIn('plateau',lines)

    def test_accepted_candidate_is_identified(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'report.json'
            report.write_text(json.dumps({'rounds':[{'round':1,'accepted':'abc','experiments':[{'task':'Implement parser','commit':'abc','evaluation':{'valid':True}}]}]}))
            lines='\n'.join(summary_lines({'status':'complete','report':str(report)}))
            self.assertIn('1 accepted changes',lines)
            self.assertIn('    accepted',lines)

    def test_delivery_state_uses_latest_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            epoch=Path(tmp)/'epoch.json'
            epoch.write_text(json.dumps({'rounds':[{'round':1,'accepted':None,'experiments':[{'task':'x','error':'worker failed'}]}]}))
            state=Path(tmp)/'state.json'
            state.write_text(json.dumps({'epochs':[{'report':str(epoch)}]}))
            lines='\n'.join(summary_lines({'status':'blocked','report':str(state)}))
            self.assertIn('1 attempts;',lines)
            self.assertIn('worker failed',lines)
