"""Exercise the actual ASGI chat, store, static form and protected delivery, no LLM or network."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, store
from app.web import server


@pytest.fixture
def browser(monkeypatch,tmp_path):
    for key,value in dict(COMPACT_LOCAL=True, LLM_PROVIDER='openai', LLM_BASE_URL='http://127.0.0.1:18347/v1',
                          LLM_API_KEY='', TG_TOKEN='', FEED_URL='', WEBIM_TOKEN='', ADMIN_TOKEN='',
                          CHAT_RATE_PER_MIN=0, BRIDGE_DIR=str(tmp_path/'bridge')).items():
        monkeypatch.setattr(config,key,value)
    class NoInference:
        def chat(self,*args,**kwargs): raise AssertionError('unexpected inference')
    monkeypatch.setattr(server.loop,'get_llm',lambda:NoInference())
    return TestClient(server.app)


def ask(browser,text,sid=None):
    r=browser.post('/api/chat',json={'client_id':'synthetic-billing-owner','session_id':sid,'message':text})
    assert r.status_code==200
    frames=[json.loads(s) for s in r.iter_lines() if s]; reply=frames[-1]
    assert reply['type']=='reply' and reply['usage']['llm_calls']==0 and not any(t['kind']=='error' for t in reply['trace'])
    return reply


def original(family):
    cases=json.loads((Path(__file__).resolve().parents[1]/'eval/retail-synthetic-v1.json').read_text())
    return next(c['prompt'] for c in cases if c['family']==family)


def counts():
    return tuple(store.db().execute('select count(*) from '+table).fetchone()[0] for table in ('quotes','invoices','handoffs'))


@pytest.mark.parametrize('phrase',[original('demo_invoice'),'Покажите пустой бланк счёта без заказа, просто для ознакомления.',
                                  'Пришлите пример счета с условными данными.', 'У меня нет КП. Как выглядит форма счёта?'])
def test_public_form_no_order_or_identifiers(browser,phrase):
    r=ask(browser,phrase); assert r['events']==[{'type':'invoice_form_example'}] and counts()==(0,0,0)
    assert 'не для оплаты' in r['reply'].lower()
    url='/invoice-form-example.html'; body=browser.get(url); assert body.status_code==200
    doc=body.text.lower(); assert 'не для оплаты' in doc and 'вымышлен' in doc and '200 ₽' in doc
    assert not any(x in doc for x in ('<form','<script','href=', 'src=', 'инн','кпп','бик','р/с',r['session_id']))
    hist=browser.get('/api/session/'+r['session_id'],params={'client_id':'synthetic-billing-owner'}).json()
    assert hist['messages'][-1]['events']==r['events']
    assert counts()==(0,0,0)


@pytest.mark.parametrize('phrase',[original('actual_commercial_invoice'),'Прошу выставить счёт для оплаты трёх лицензий на квартал.',
                                  'Подготовьте действительный счёт на четыре рабочих места для нашей организации.'])
def test_real_request_creates_one_ticket_and_no_invoice(browser,phrase):
    r=ask(browser,phrase);sid=r['session_id'];ticket=store.open_handoff(sid)
    assert ticket and ticket['reason']=='Оформление счёта на оплату' and phrase in ticket['summary']
    assert [e['type'] for e in r['events']]==['handoff'] and counts()==(0,0,1)
    assert 'ещё не выставлен' in r['reply'] and 'реквизиты' in r['reply']
    repeat=ask(browser,phrase,sid);assert not repeat['events'] and counts()==(0,0,1)
    assert store.open_handoff(sid)['id']==ticket['id']
    assert store.take_handoff(ticket['id'],'Учебный оператор')
    taken=ask(browser,'Уточнение: количество остаётся тем же.',sid)
    assert taken['handoff_status']=='taken' and not taken['events'] and counts()==(0,0,1)


def test_now_real_after_viewing_generic_form(browser):
    r=ask(browser,original('demo_invoice'));sid=r['session_id']
    switched=ask(browser,'Теперь настоящий, пожалуйста.',sid)
    assert [e['type'] for e in switched['events']]==['handoff'] and counts()==(0,0,1)


@pytest.mark.parametrize('phrase',[
    'Если подпишем договор, выставьте настоящий счёт.', 'Не выставляйте настоящий счёт на оплату.',
    'Как выставить настоящий счёт?', 'Я бы хотел узнать, нужен ли реальный счёт.',
    'Прошу выставить настоящий счёт. Отмена, пока не надо.',
])
def test_negative_or_hypothetical_no_write(browser,phrase):
    r=ask(browser,phrase);assert not r['events'] and counts()==(0,0,0)


def test_existing_private_document_restrictions_unchanged(browser):
    sid=store.get_session(None,'synthetic-billing-owner')['id']
    store.update_session(sid,facts={'компания':'Учебная организация','ИНН':'0000000000','КПП':'000001001'})
    store.add_quote(sid,'КП-123','Учебная организация',[{'offer_id':1,'name':'Учебная позиция','qty':2,'price':100,'sum':200,'vat':None}],200,'')
    r=ask(browser,'Пришлите учебный счёт по этому КП в чат.',sid)
    assert 'make_invoice' in [t['name'] for t in r['trace']] and counts()==(1,1,0)
    history=browser.get('/api/session/'+sid,params={'client_id':'synthetic-billing-owner'}).json()
    event,=[e for msg in history['messages'] for e in msg.get('events',[]) if e['type']=='demo_invoice']
    assert browser.get(event['url']).content.startswith(b'%PDF-')
    assert TestClient(server.app).get(event['url']).status_code==404
    assert TestClient(server.app).get('/api/session/'+sid,params={'client_id':'unrelated-owner'}).status_code==404
