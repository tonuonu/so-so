import http.server
import json
from pathlib import Path
import threading
import unittest
from soso.models import Budget, Router

class HTTPTests(unittest.TestCase):
    def test_openai_and_ollama_protocols(self):
        received=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path,data))
                result=({'message':{'content':'{"tool":"done"}'}} if self.path=='/api/chat'
                        else {'choices':[{'message':{'content':'{"tool":"done"}'}}]})
                payload=json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self,*args): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            for kind in ('openai','ollama'):
                url=f'http://127.0.0.1:{server.server_port}'+('/v1' if kind=='openai' else '')
                config={'models':{'local':{'kind':kind,'url':url,'model':'test'}},'routes':{'worker':['local']}}
                response=Router(config,Budget(2,5),lambda *a,**k:None).ask('worker','test',Path.cwd())
                self.assertEqual(response['tool'],'done')
            self.assertEqual(received[0][0],'/v1/chat/completions')
            self.assertEqual(received[1][0],'/api/chat')
            self.assertEqual(received[1][1]['format'],'json')
            self.assertEqual(received[0][1]['max_tokens'],2048)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
