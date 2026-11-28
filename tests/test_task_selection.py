"""Offline buyer flows. Optional assortment gate uses a read-only *copy* via ASSORTMENT_FIXTURES."""
import json
import os
from pathlib import Path

import pytest

from app import config, store
from app.agent import loop, task_selection
from app.catalog import search
from app.kb import search as kb


class NoModel:
    def chat(self,*args,**kwargs):
        raise AssertionError('the shortlist must not need inference')


@pytest.fixture
def buyer(monkeypatch):
    folder=os.getenv('ASSORTMENT_FIXTURES')
    if not folder: pytest.skip('provide read-only catalog and KB copies, not live databases')
    folder=Path(folder); catalog=folder/'catalog.sqlite'; knowledge=folder/'kb.sqlite'
    assert catalog.is_file() and knowledge.is_file()
    for k,v in dict(COMPACT_LOCAL=True,LLM_PROVIDER='openai',LLM_API_KEY='',LLM_BASE_URL='http://127.0.0.1:18347/v1',CATALOG_DB=catalog,KB_DB=knowledge).items():monkeypatch.setattr(config,k,v)
    monkeypatch.setenv('CATALOG_DB',str(catalog));monkeypatch.setattr(kb,'DB_PATH',knowledge)
    return lambda text,sid=None:loop.run_turn(sid,text,client_id='synthetic-shortlist',llm=NoModel())


def test_real_task_to_verified_pdf(buyer):
    r=buyer('Нужен архиватор для Windows. Коммерческая организация, 12 лицензий.');sid=r['session_id']; state=store.find_session(sid)['items'][task_selection.KEY]
    assert [t['name'] for t in r['trace']][:2]==['browse_categories','search_products']
    assert not store.last_quote(sid) and not store.open_handoff(sid)
    rows=[search.get_offer(i) for i in state['page']];o=next(o for o in rows if o['qty_pricing']=='per_unit' and o['price'] and o['license_type']!='Техподдержка');n=rows.index(o)+1
    assert o['name'] in r['reply'] and o['platform']=='Windows' and o['org_type']=='Коммерческая' and o['qty_min']<=12<=o['qty_max']
    assert not buyer('Можно коммерческое предложение?',sid)['events']
    picked=buyer(f'Беру вариант {n}',sid);assert o['name'] in picked['reply'] and not picked['events']
    result=buyer('Можете прислать коммерческое предложение по этому варианту?',sid)
    assert [e['type'] for e in result['events']]==['quote'] and result['usage']['llm_calls']==0
    quote=store.last_quote(sid);line,=json.loads(quote['items_json']);assert (line['offer_id'],line['qty'],line['sum'])==(o['offer_id'],12,o['price']*12)
    assert (config.QUOTES_DIR/(quote['number']+'.pdf')).read_bytes().startswith(b'%PDF-')


@pytest.mark.parametrize('request_text', ['Подберите софт для ретуши фотографий.', 'Ищу программу для управления проектами.', 'Нужна утилита, чтобы читать корпоративную почту.'])
def test_different_tasks_bind_existing_offers(buyer,request_text):
    r=buyer(request_text); state=store.find_session(r['session_id'])['items'][task_selection.KEY]
    assert state['page'] and all(search.get_offer(i)['name'] in r['reply'] for i in state['page'])
    assert 'quote' not in {e['type'] for e in r['events']} and not store.open_handoff(r['session_id'])


def test_refinements_survive_until_document_request(buyer):
    r=buyer('В студию нужна программа для ретуши фотографий.');sid=r['session_id']
    buyer('Для Windows',sid);r=buyer('Коммерческая организация',sid)
    state=store.find_session(sid)['items'][task_selection.KEY];oid=state['page'][0];offer=search.get_offer(oid)
    assert state['filters']=={'platform':'Windows','org_type':'Коммерческая'}
    assert offer['name'] in r['reply'] and offer['price']>0
    assert not buyer('Возьмём первую позицию',sid)['events']
    assert not buyer('2 шт.',sid)['events']
    r=buyer('Подготовьте нам коммерческое предложение',sid);assert [e['type'] for e in r['events']]==['quote']
    line,=json.loads(store.last_quote(sid)['items_json']);assert (line['offer_id'],line['qty'],line['sum'])==(oid,2,offer['price']*2)


def test_estimate_does_not_become_an_order_quantity(buyer):
    r=buyer('Подберите архиватор, примерно 10 лицензий.');sid=r['session_id']
    assert 'units' not in store.find_session(sid)['items'][task_selection.KEY]
    buyer('Беру первый вариант',sid)
    assert not buyer('Составьте КП',sid)['events'] and not store.last_quote(sid)


def test_installation_platform_is_not_an_offline_right():
    row={'offer_id':1,'platform':'Windows','description':'Платформа: Windows. Установка на ПК.'}
    assert not task_selection._fits(row,{'mode':'offline'},None)
    assert task_selection._fits(dict(row,description='Работает без интернета.'),{'mode':'offline'},None)
    assert not task_selection._fits(dict(row,description='Не работает без интернета.'),{'mode':'offline'},None)


def test_number_does_not_guess_buyer_or_package(buyer):
    r=buyer('Подберите почтовый клиент, нам нужны 5 лицензий.');sid=r['session_id'];state=store.find_session(sid)['items'][task_selection.KEY]
    offers=[search.get_offer(i) for i in state['page']]; ix=next(i for i,o in enumerate(offers,1) if 'за комплект' in (o['price_for'] or ''))
    ans=buyer(f'Выбираю {ix}',sid);assert 'комплект' in ans['reply']
    assert not buyer('Пришлите нам КП',sid)['events'] and not store.last_quote(sid)


def test_invalid_ordinal_does_not_authorize_quote(buyer):
    r=buyer('Ищем архиватор для Windows. Коммерческая организация, 12 лицензий.');sid=r['session_id']
    assert not buyer('Выбираю первый вариант',sid)['events']
    assert not buyer('Третий вариант',sid)['events']
    assert not buyer('Пришлите КП',sid)['events'] and not store.last_quote(sid)
