"""Isolated storefront + operator delivery; no model, sockets or production databases."""
import json

import pytest
from fastapi.testclient import TestClient
from app import config, notify, store, webim
from app.web import server, tgdesk


@pytest.fixture
def site(monkeypatch):
    for k,v in dict(COMPACT_LOCAL=True, LLM_PROVIDER='openai', LLM_BASE_URL='http://127.0.0.1:18347/v1', LLM_API_KEY='', ADMIN_TOKEN='synthetic-admin', TG_TOKEN='', FEED_URL='', CHAT_RATE_PER_MIN=0).items(): monkeypatch.setattr(config,k,v)
    class Empty:
        def chat(self,*args,**kw):
            from app.agent.llm import LLMResult
            return LLMResult(text='{"route":"clarify","query":""}')
    monkeypatch.setattr(server.loop,'get_llm',lambda:Empty())
    monkeypatch.setattr(notify,'SETTLE',0)
    monkeypatch.setattr(server,'_answered',{})  # every test has its own DB with ticket IDs starting at 1
    return TestClient(server.app)


def chat(client,text,sid=None):
    response=client.post('/api/chat',json={'client_id':'synthetic-store-visitor','session_id':sid,'message':text})
    assert response.status_code==200
    result=json.loads(response.content.splitlines()[-1]); assert result['type']=='reply' and not any(x['kind']=='error' for x in result.get('trace',[]));return result


def internal_telegram(monkeypatch):
    calls=[]
    monkeypatch.setattr(config,'TG_CHAT','-100000000001')
    def spy(method,**kwargs):
        assert kwargs['chat_id']==config.TG_CHAT
        calls.append((method,kwargs));return {'message_id':22}
    monkeypatch.setattr(notify,'call',spy)
    return calls


def test_store_stock_to_one_internal_ticket_and_same_chat(site,monkeypatch):
    one=chat(site,'У вас нет в наличии программы «Учебный продукт». У партнёра она есть в наличии.')
    sid=one['session_id'];ticket=store.open_handoff(sid); assert ticket,one['reply']
    assert [x['type'] for x in one['events']]==['handoff'] and 'не подтверждено' in one['reply']
    assert 'Учебный продукт' in ticket['summary'] and one['usage']['llm_calls']==0
    assert not chat(site,'Напоминаю: у партнёра есть в наличии.',sid)['events']
    assert store.db().execute('select count(*) from handoffs').fetchone()[0]==1
    calls=internal_telegram(monkeypatch)
    assert notify.sync()==1
    assert [(m,k['chat_id']) for m,k in calls]==[('sendMessage',config.TG_CHAT)]
    assert 'Учебный продукт' in calls[0][1]['text']
    url=f"/api/tgdesk/handoffs/{ticket['id']}"
    from test_tgdesk import signed, TOKEN, MANAGER
    monkeypatch.setattr(config,'TG_TOKEN',TOKEN);monkeypatch.setenv('TG_MANAGER_IDS',str(MANAGER))
    auth={'Authorization':'tma '+signed()}
    assert site.get(url,headers=auth).json()['can_answer']
    assert site.get(url,headers={'Authorization':'tma '+signed(user=42)}).status_code==403
    assert site.post(url+'/answer',headers=auth,json={'answer':'Учебный ответ для вашего чата.'}).json()['ok']
    url=f'/api/session/{sid}/answers'; response=site.get(url,params={'client_id':'synthetic-store-visitor'}).json();assert any('Учебный ответ' in a['content'] for a in response['answers'])
    assert site.get(url,params={'client_id':'foreign'}).status_code==404


@pytest.mark.parametrize('follow,should_open',[
 ('Зато у вашего партнёра эта позиция есть.',True),('Нашёл её у партнёра в наличии.',True),
 ('У партнёра этой позиции тоже нет.',False),('А если у партнёра есть, что тогда?',False),
 ('Есть ли у партнёра?',False),('У партнёра есть, но не передавайте менеджеру.',False),
 ('У партнёра есть другой продукт.',False),('У партнёра есть «Совсем другой товар».',False),
])
def test_immediate_product_context(site,follow,should_open):
    sid=store.get_session(None,'synthetic-store-visitor')['id']; store.add_message(sid,'user','Ищу «Пример редактора».');store.add_message(sid,'assistant','«Пример редактора». По фиду: нет в наличии.')
    result=chat(site,follow,sid);assert bool(store.open_handoff(sid)) is should_open
    if should_open: assert result['usage']['llm_calls']==0 and 'Пример редактора' in store.open_handoff(sid)['summary']


def test_partner_stock_alone_or_unavailable_price_not_automatic(site):
    assert not chat(site,'У партнёра есть в наличии программа «Учебная».')['events']
    assert not chat(site,'У вас нет в наличии программы, хотя цена известна.')['events']
    assert store.db().execute('select count(*) from handoffs').fetchone()[0]==0


def test_priced_but_unavailable_offer_can_still_make_a_draft(site,small_catalog_db,tmp_path,monkeypatch):
    import shutil,sqlite3
    from app.catalog.search import get_offer
    db=tmp_path/'priced-unavailable.sqlite';shutil.copyfile(small_catalog_db,db)
    with sqlite3.connect(db) as c:c.execute('update offers set available=0 where id=1001')
    monkeypatch.setenv('CATALOG_DB',str(db));monkeypatch.setattr(config,'CATALOG_DB',db)
    o=get_offer(1001);assert o['available'] is False and o['price']>0
    qty=o['qty_min'] or 1;sid=store.get_session(None,'synthetic-store-visitor')['id']
    store.update_session(sid,items={'compact_selected':o,'compact_qty':qty,'compact_lines':[{'offer_id':o['offer_id'],'qty':qty}]})
    r=chat(site,'Подготовьте КП',sid);assert [e['type'] for e in r['events']]==['quote']
    line,=json.loads(store.last_quote(sid)['items_json']);assert line['available'] is False and line['sum']==qty*o['price']
    assert not store.open_handoff(sid) and 'нет в наличии' in r['reply']


def test_partner_first_then_confirmation_in_same_chat(site):
    first=chat(site,'Видел у партнёра в наличии «Тестовая лицензия».');sid=first['session_id']
    assert not first['events'] and 'Уточните' in first['reply']
    yes=chat(site,'Да, той же позиции у вас нет.',sid)
    assert yes['events'][0]['type']=='handoff' and 'Тестовая лицензия' in store.open_handoff(sid)['summary']


def test_unrelated_chat_and_topic_do_not_supply_evidence(site):
    sid=store.get_session(None,'synthetic-store-visitor')['id']
    store.add_message(sid,'user','Только что выяснили, что у вас нет лицензии.')
    store.add_message(sid,'assistant','По фиду: нет в наличии.')
    store.add_message(sid,'user','Как войти в личный кабинет?')
    store.add_message(sid,'assistant','Через страницу входа.')
    assert not chat(site,'У партнёра есть в наличии.',sid)['events']
    assert not chat(site,'У партнёра есть в наличии.')['events']


def test_followup_question_after_assertion_is_not_a_hypothesis(site):
    r=chat(site,'У вас товара нет, у партнёра есть в наличии. Что делать дальше?')
    assert [e['type'] for e in r['events']]==['handoff']


def test_existing_ticket_stays_internal_when_partner_is_mentioned(site,monkeypatch):
    sid=store.get_session(None,'synthetic-store-visitor')['id'];ticket=store.add_handoff(sid,'первичный запрос','условия покупки','');store.take_handoff(ticket,'Учебный оператор')
    # Same ticket and same configured manager chat even after a partner is mentioned.
    calls=internal_telegram(monkeypatch)
    con=notify._db();con.execute('insert into tg_cards(handoff_id,chat_id,message_id,text,ts) values(?,?,?,?,?)',(ticket,config.TG_CHAT,22,'old',__import__('time').time()));con.commit()
    r=chat(site,'У вас нет в наличии редактора, но у партнёра он есть.',sid)
    assert r['handoff_status']=='taken' and not r['events'];assert store.open_handoff(sid)['id']==ticket
    assert notify.sync()==1 and [m for m,_ in calls]==['editMessageText']
    assert tgdesk._ticket(ticket)['id']==ticket


@pytest.mark.parametrize('department,accept',[('internal-sales',True),('internal-sales',False),('',False)])
def test_webim_private_manager_reply_only_to_origin(site,monkeypatch,department,accept):
    import time
    monkeypatch.setattr(config,'WEBIM_TOKEN','local-test-webim-token')
    monkeypatch.setattr(config,'WEBIM_DEP_KEY',department)
    buyer=431; sid=webim.session_id(buyer); calls=[]
    def spy(method,payload):
        calls.append((method,payload));assert payload['chat_id']==buyer
        if method=='redirect_chat':
            assert department and payload['dep_key']==department
            return accept
        assert method=='send_message';return True
    monkeypatch.setattr(webim,'call',spy)
    webim.answer(buyer,'У вас нет в наличии редактора. У партнёра он есть.',time.monotonic())
    ticket=store.open_handoff(sid);assert ticket
    assert [m for m,_ in calls]==(['send_message','redirect_chat'] if department else ['send_message'])
    calls.clear(); internal_telegram(monkeypatch);assert notify.sync()==1
    response=site.post(f"/api/admin/handoff/{ticket['id']}/answer",headers={'x-admin-token':'synthetic-admin'},json={'answer':'Ответ отдела по вашему запросу.'})
    assert response.json()['webim_delivered'] is True
    assert calls==[('send_message',{'chat_id':buyer,'message':{'kind':'operator','text':'Ответ отдела по вашему запросу.'}})]
    assert any(m['meta'].get('from')=='manager' for m in store.messages(sid))
    duplicate=site.post(f"/api/admin/handoff/{ticket['id']}/answer",headers={'x-admin-token':'synthetic-admin'},json={'answer':'Ответ отдела по вашему запросу.'})
    assert duplicate.status_code==200 and len(calls)==1
    assert site.get(f'/api/session/{sid}',params={'client_id':'foreign'}).status_code==404
