import json

import pytest

from app import config, store
from app.agent import compact, loop
from app.agent.llm import Scripted


@pytest.fixture(autouse=True)
def local(monkeypatch):
    for k,v in dict(COMPACT_LOCAL=True, LLM_PROVIDER='openai', LLM_BASE_URL='http://127.0.0.1:18347/v1', LLM_API_KEY='').items():
        monkeypatch.setattr(config,k,v)


@pytest.mark.parametrize('request_text', ['Переключите меня на оператора.', 'Хочу поговорить со специалистом.', 'Нужен живой человек.', 'Передайте вопрос менеджеру, пожалуйста.'])
def test_handoff(request_text):
    model=Scripted([])
    r=loop.run_turn(None,request_text+' demo@example.invalid',llm=model)
    assert not model.calls and r['events'][0]['type']=='handoff'
    row=store.open_handoff(r['session_id'])
    assert request_text in row['summary'] and row['contact']=='demo@example.invalid'
    assert 'example.invalid' not in json.dumps(r)
    r2=loop.run_turn(r['session_id'],'Соедините с менеджером',llm=model)
    assert str(row['id']) in r2['reply'] and not r2['events']


@pytest.mark.parametrize('text',['Не передавайте вопрос менеджеру.', 'Если нужен менеджер, что дальше?', 'Менеджер сказал, что нужен антивирус.'])
def test_no_unrequested_write(text):
    r=loop.run_turn(None,text,llm=Scripted(['{"route":"human","query":""}']))
    assert not store.open_handoff(r['session_id']) and r['reply']==compact.CONFIRM


def test_private_input_and_no_model_prose():
    model=Scripted(['{"route":"make_invoice","query":"bad"}'])
    r=loop.run_turn(None,'Отправьте результат на demo@example.invalid',llm=model)
    assert 'demo@example.invalid' not in str(model.calls) and not r['events'] and r['reply']==compact.CLARIFY


def test_offers_keep_conditions():
    reply=compact.catalog_reply({'total':1,'match':'all','products':[{'title':'Учебная лицензия'}], 'first_product':{'offers':[dict(name='Лицензия',price=150,org_type='Академическая',qty_min=5,available=False,price_ambiguous=True)]}})
    assert 'Академическая' in reply and 'от: 5' in reply and 'По фиду: нет в наличии' in reply and '150' not in reply


def test_no_unsourced_policy():
    out={'results':[dict(title='Статья',snippet='Вымышленный срок',url='https://store.softline.ru.bad.invalid/p')]}
    assert 'Вымышленный' not in compact.help_reply(out)
    out['results'][0]['url']='https://store.softline.ru/synthetic'; assert 'Источник:' in compact.help_reply(out)
    assert 'Источник:' not in compact.help_reply(dict(out,note='нет точного совпадения'))


@pytest.mark.small_catalog
def test_total_needs_shown_sku_and_buyer_quantity():
    model=Scripted(['{"route":"catalog","query":"Kaspersky Endpoint Security"}', '{"route":"total","query":"999 fake"}', '{"route":"total","query":"999 fake"}'])
    r=loop.run_turn(None,'Какие лицензии есть?',llm=model)
    offer=r['trace'][0]['output']['first_product']['offers'][0]
    qty=offer['qty_min']; sku=offer['vendor_code']
    r2=loop.run_turn(r['session_id'],f'Рассчитайте {qty} шт. {sku}',llm=model)
    assert r2['trace'][0]['name']=='calc_total' and r2['trace'][0]['output']['total']==qty*offer['price']
    other=loop.run_turn(None,f'Рассчитайте {qty} шт. {sku}',llm=model)
    assert other['usage']['tool_calls']==0 and str(qty*offer['price']) not in other['reply']


def test_refuse_remote(monkeypatch):
    monkeypatch.setattr(config,'LLM_BASE_URL','https://example.invalid/v1')
    with pytest.raises(ValueError): loop.run_turn(None,'Запрос',llm=Scripted([]))
