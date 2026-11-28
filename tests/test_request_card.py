"""Offline A/B of the fallback contract. All replies are scripted, all buyers fictional."""
import json

import pytest

from app import config, store
from app.agent import compact_spec, loop, request_card as rc
from app.agent.llm import LLMResult
from test_quote_choice import shop  # small synthetic catalog + temp application DB


class Router:
    def __init__(self,*plans):
        self.plans=iter(plans);self.requests=[]
    def chat(self,messages,tools,**opts):
        assert tools==[]
        self.requests.append((json.loads(messages[-1]['content']),opts))
        return LLMResult(text=json.dumps(next(self.plans)),model='offline-structured-reply')


def plan(ask='platform',goal='',**kw):
    return dict(route='clarify',query='',goal_evidence=goal,product_evidence='',ask=ask,**kw)


@pytest.fixture(autouse=True)
def local(monkeypatch):
    for k,v in dict(COMPACT_LOCAL=True,COMPACT_REQUEST_CARD=True,LLM_PROVIDER='openai',LLM_BASE_URL='http://127.0.0.1:18347/v1',LLM_API_KEY='',FEED_URL='',TG_TOKEN='').items():monkeypatch.setattr(config,k,v)


def test_offline_ab_same_input(monkeypatch):
    text='Требуется сверка макетов, 4 места на год.'
    for enabled in (False,True):
        monkeypatch.setattr(config,'COMPACT_REQUEST_CARD',enabled)
        r=Router(plan(goal='сверка макетов'))
        out=loop.run_turn(None,text,llm=r);body,options=r.requests[0]
        assert ('fields' in body)==enabled and ('previous' in body)!=enabled
        assert (options['response_format']==rc.FORMAT)==enabled
        assert ('операционной системе' in out['reply'])==enabled
        assert (rc.KEY in store.find_session(out['session_id'])['items'])==enabled
        assert not out['events']


def test_correct_one_field_keep_source_turn_and_sanitized_history():
    r=Router(plan(goal='сверка макетов'),plan(ask='months'),plan(ask='months'),plan(ask='platform'))
    def say(sid,text): return loop.run_turn(sid,text,llm=r)
    sid=say(None,'Нужна сверка макетов, четыре места на год. Тестовая почта contact@example.invalid.')['session_id']
    say(sid,'Рабочая среда Windows.')
    correction=say(sid,'Уточнение: не четыре места, а два.')
    assert 'срок' not in correction['reply'].lower()
    say(sid,'Эм, шурум-бурум.')
    body,_=r.requests[-1];fields=body['fields']
    assert fields['seats']=={'value':2,'turn':3} and fields['months']=={'value':12,'turn':1}
    assert fields['goal']=={'value':'сверка макетов','turn':1} and fields['platform']=={'value':'windows','turn':2}
    assert fields['units'] is None and len(body['recent'])==3
    assert 'contact@example.invalid' not in str(r.requests)
    assert not store.last_quote(sid) and not store.open_handoff(sid)


def test_invalid_plan_does_not_change_card_or_write():
    r=Router(plan(goal='не сказано'),plan(goal='пересмотр плакатов',make_quote=True))
    first=loop.run_turn(None,'Нужен пересмотр плакатов.',llm=r);sid=first['session_id']
    second=loop.run_turn(sid,'Нужен пересмотр плакатов.',llm=r)
    assert not store.find_session(sid)['items'][rc.KEY]['fields'].get('goal')
    assert not second['events'] and not store.last_quote(sid) and not store.open_handoff(sid)


def test_cancel_resets_even_pending_multiline_and_bounds_history():
    r=Router(plan(goal='разметка изображений'),plan())
    a=loop.run_turn(None,'Хочу разметка изображений, 6 мест.',llm=r);sid=a['session_id']
    items=store.find_session(sid)['items'];items[compact_spec._STATE]={'rows':[{'qty':6}]};store.update_session(sid,items=items)
    cleared=loop.run_turn(sid,'Отмените заявку.',llm=r)
    assert not cleared['events'] and rc.KEY not in store.find_session(sid)['items'] and compact_spec._STATE not in store.find_session(sid)['items']
    loop.run_turn(sid,'Иная задача для отдела.',llm=r)
    assert r.requests[-1][0]['recent']==[] and all(x is None for x in r.requests[-1][0]['fields'].values())


def test_bare_number_only_answers_the_immediately_asked_dimension():
    items={};card=rc.prepare(items,'Нужна программа',1);rc.question(card,'seats',1)
    assert rc.prepare(items,'8',2)['fields']['seats']=={'value':8,'turn':2}
    assert rc.prepare(items,'22',3)['fields']['seats']=={'value':8,'turn':2}


def test_read_only_catalog_refinement_does_not_guess_unknown_offer_dimensions():
    items={};card=rc.prepare(items,'Работаем в Linux, срок 6 месяцев, 7 пользователей.',1)
    seen=[]
    def tool(name,args):
        seen.append((name,args));return {'offers':[{'offer_id':9,'term_months':6}, {'offer_id':15,'platform':'Linux','term_months':6}]}
    out={'match':'all','total':1,'products':[{'product_id':'catalog-item'}]}
    result=rc.narrow(card,out,tool)
    assert seen==[('get_product',{'product_id':'catalog-item','term_months':6,'platform':'Linux'})]
    assert [o['offer_id'] for o in result['first_product']['offers']]==[15]
    assert 'compact_lines' not in items


def test_saved_terms_only_prefill_quote_for_explicit_same_product(shop):
    turn,_=shop; sid=store.get_session(None,client_id='synthetic-quote')['id']
    # A different component has already collected current buyer facts. No model
    # call, write or invented offer IDs follow from the presence of the card.
    items=store.find_session(sid)['items'];rc.prepare(items,'Нужна программа «Синтетический планер», на 7 мест на год.',1)
    store.update_session(sid,items=items)
    store.add_message(sid,'user','Первое учебное сообщение.');store.add_message(sid,'user','Между ходами уточняли детали.')
    r=turn('Прошу КП по «Синтетический планер».',sid)
    assert not r['events'] and not store.last_quote(sid) and '7 шт.' in r['reply'] and '12 мес.' in r['reply']
    r=turn('Подтверждаю PV-AN',sid)
    assert [e['type'] for e in r['events']]==['quote']
    line,=json.loads(store.last_quote(sid)['items_json']);assert (line['vendor_code'],line['qty'],line['sum'])==('PV-AN',7,5180)


def test_fabricated_search_and_quantity_never_authorize_action():
    r=Router(dict(route='catalog',query='Выдуманный SKU 7654',goal_evidence='',product_evidence='',ask='units'))
    out=loop.run_turn(None,'Нужны инструменты для кружка.',llm=r)
    assert not out['trace'] and not out['events'] and 'Выдуманный' not in out['reply']
    items={};rc.prepare(items,'11 лицензий',1);card=rc.prepare(items,'Около 8 лицензий',2)
    assert 'units' not in card['fields']
