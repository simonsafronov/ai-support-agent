"""Offline regression: all selections are taken from the synthetic catalog, not invented IDs."""
import json

import pytest

from app import config, store
from app.agent import compact, compact_order, loop
from app.agent.llm import Scripted

pytestmark = pytest.mark.small_catalog


@pytest.fixture(autouse=True)
def local(monkeypatch):
    for k, v in dict(COMPACT_LOCAL=True, LLM_PROVIDER='openai', LLM_BASE_URL='http://127.0.0.1:18347/v1', LLM_API_KEY='').items():
        monkeypatch.setattr(config,k,v)


def search():
    model = Scripted(['{"route":"catalog","query":"Kaspersky Endpoint Security"}'])
    res = loop.run_turn(None, 'Покажите варианты Kaspersky Endpoint Security', client_id='browser-fixture',llm=model)
    offer = res['trace'][0]['output']['first_product']['offers'][0]
    return model, res['session_id'], offer


def test_quote_invoice_multi_turn_and_privacy():
    model,sid,offer = search()
    def turn(text): return loop.run_turn(sid,text,llm=model)
    assert not turn('Подготовьте КП')['events']
    assert 'Сколько' in turn('Выбираю '+offer['vendor_code'])['reply']
    quote = turn(f"{offer['qty_min']} шт.")
    assert quote['events'][0]['type']=='quote'
    assert json.loads(store.last_quote(sid)['items_json'])[0]['offer_id']==offer['offer_id']
    assert json.loads(store.last_quote(sid)['items_json'])[0]['qty']==offer['qty_min']
    assert 'реквизиты' in turn('Пришлите учебный счёт в этот чат')['reply']
    out=turn('Покупатель: ООО «Тестовый покупатель», ИНН 7701234560 КПП 770101001')
    assert out['trace'][0]['name']=='make_invoice' and store.last_invoice(sid)['status']=='issued'
    assert store.find_session(sid)['facts']['компания']=='ООО «Тестовый покупатель»'
    turn('Пришлите учебный счёт в этот чат')
    assert len([m for m in store.messages(sid) if m['meta'].get('invoice_id')])==1
    assert len(model.calls)==1
    foreign=loop.run_turn(None,'Подготовьте КП',client_id='browser-other',llm=model)
    assert not store.last_quote(foreign['session_id'])


def test_quantity_first_and_other_sku():
    model,sid,_ = search()
    filtered=loop.run_turn(sid,'10 шт.',llm=model)
    assert filtered['trace'][0]['name']=='get_product'
    offer=filtered['trace'][0]['output']['offers'][0]
    loop.run_turn(sid,'Выбираю '+offer['vendor_code'],llm=model)
    r=loop.run_turn(sid,'Сформируйте КП',llm=model)
    assert r['events'][0]['type']=='quote' and json.loads(store.last_quote(sid)['items_json'])[0]['qty']==10
    loop.run_turn(sid,'Выбираю НЕИЗВЕСТНЫЙ',llm=model)
    assert not loop.run_turn(sid,'Подготовьте КП',llm=model)['events']


def test_unavailable_not_priceless():
    offer=dict(name='Учебная позиция',price=420,available=False,vendor_code='DEMO',offer_id=1)
    def tool(name,args):
        assert name=='calc_total'
        return {'lines':[dict(name=offer['name'],qty=2,sum=840,available=False)]}
    r=compact.total_reply('Рассчитайте 2 шт. DEMO',[offer],tool)
    assert '840 ₽' in r and compact_order.UNAVAILABLE in r
    offer['price']=None
    r=compact.total_reply('Рассчитайте 2 шт. DEMO',[offer],tool)
    assert '840' not in r
