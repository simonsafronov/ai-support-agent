"""Run the two frozen invoice cases against a temporary, loopback-only ASGI server."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time


def main():
    root=Path(__file__).resolve().parents[1]
    cases=json.loads((root/'eval/retail-synthetic-v1.json').read_text())
    prompts={r['family']:r['prompt'] for r in cases if r['family'] in ('demo_invoice','actual_commercial_invoice')}
    os.umask(0o077)
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp);shutil.copytree(root/'app',d/'app',ignore=shutil.ignore_patterns('__pycache__','*.pyc','.env'))
        os.chdir(d);sys.path.insert(0,str(d));os.environ.clear()
        os.environ.update(PATH='/usr/bin:/bin',HOME=tmp,COMPACT_LOCAL='1',LLM_PROVIDER='openai',LLM_BASE_URL='http://127.0.0.1:18347/v1',LLM_API_KEY='',TG_TOKEN='',FEED_URL='',WEBIM_TOKEN='',PRODUCT_CARDS='0',CHAT_RATE_PER_MIN='0',APP_DB=str(d/'app.sqlite'),CATALOG_DB=str(d/'unused-catalog.sqlite'),KB_DB=str(d/'unused-kb.sqlite'),CARDS_DB=str(d/'cards.sqlite'),QUOTES_DIR=str(d/'quotes'),BRIDGE_DIR=str(d/'bridge'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));sock.listen();address=sock.getsockname()
            def guard(event,args):
                if event=='socket.connect' and args[1]!=address: raise RuntimeError('only the temporary HTTP listener is allowed')
                if event in ('socket.bind','socket.sendto'):raise RuntimeError('unexpected socket operation')
            sys.addaudithook(guard)
            import uvicorn
            from app.web import server
            from app import store
            class NoModel:
                def chat(self,*args,**kwargs): raise AssertionError('unexpected model call')
            server.loop.get_llm=lambda:NoModel()
            srv=uvicorn.Server(uvicorn.Config(server.app,host=address[0],port=address[1],lifespan='off',loop='asyncio',ws='none',log_level='error',access_log=False))
            thread=threading.Thread(target=srv.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                until=time.monotonic()+8
                while not srv.started:
                    if not thread.is_alive() or time.monotonic()>until:raise RuntimeError('temporary server failed to start')
                    time.sleep(.02)
                def http(method,path,body=None):
                    conn=HTTPConnection(*address,timeout=6)
                    try:
                        conn.request(method,path,body=json.dumps(body) if body else None,headers={'Content-Type':'application/json'});r=conn.getresponse();return r.status,r.read()
                    finally:conn.close()
                def chat(text,sid=None):
                    code,body=http('POST','/api/chat',{'client_id':'synthetic-buyer','session_id':sid,'message':text});assert code==200
                    result=json.loads(body.splitlines()[-1]);assert result['type']=='reply' and result['usage']['llm_calls']==0 and not any(t['kind']=='error' for t in result['trace']);return result
                form=chat(prompts['demo_invoice']);sid=form['session_id'];assert form['events']==[{'type':'invoice_form_example'}]
                code,body=http('GET','/invoice-form-example.html');assert code==200 and 'НЕ ДЛЯ ОПЛАТЫ'.encode() in body
                js=http('GET','/app.js')[1];assert b'/invoice-form-example.html' in js
                actual=chat(prompts['actual_commercial_invoice'],sid);ticket=store.open_handoff(sid);assert ticket and prompts['actual_commercial_invoice'] in ticket['summary'] and actual['events'][0]['type']=='handoff' and 'двух годовых мест' in actual['reply']
                again=chat(prompts['actual_commercial_invoice'],sid);assert not again['events'] and store.open_handoff(sid)['id']==ticket['id']
                assert [store.db().execute('select count(*) from '+t).fetchone()[0] for t in ('quotes','invoices','handoffs')]==[0,0,1]
                print('PASS: loopback HTTP, both frozen cases, static form, real billing handoff, repeat; quotes=0 invoices=0 tickets=1 model_calls=0')
            finally:
                srv.should_exit=True;thread.join(8);assert not thread.is_alive()


if __name__=='__main__':main()
