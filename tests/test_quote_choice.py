"""Fresh fictional assortment, no inference. Exercise pending quotes through run_turn."""
import json
import sqlite3
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from app import config, store
from app.agent import loop, quote_choice
from app.catalog import ingest


@pytest.fixture
def shop(tmp_path,monkeypatch):
    feed=ET.parse(Path(__file__).parent/'data/feed_small.xml');offers=feed.find('.//offers');offers.clear()
    for ident,sku,months,version,price in [(71,'PV-AN',12,'Основная',740),(72,'PV-MO',1,'Основная',85)]:
        row=ET.SubElement(offers,'offer',id=str(ident),group_id='910',available='true')
        for k,v in dict(name='Синтетический планер',vendor='Учебный вендор',vendorCode=sku,price=str(price),currencyId='RUR',url='https://shop.example/test/'+sku,categoryId='31',group_id='910',description='Именная лицензия на одного пользователя.').items():ET.SubElement(row,k).text=v
        for k,v in [('Версия',version),('Тип лицензии','Новая'),('Срок действия',str(months)+' мес.')]:ET.SubElement(row,'param',name=k).text=v
    xml=tmp_path/'fixture.xml';feed.write(xml,encoding='utf-8');db=tmp_path/'catalog.sqlite';ingest.build(xml,db,verbose=False)
    monkeypatch.setenv('CATALOG_DB',str(db));monkeypatch.setattr(config,'CATALOG_DB',db)
    for k,v in dict(COMPACT_LOCAL=True,LLM_PROVIDER='openai',LLM_BASE_URL='http://127.0.0.1:18347/v1',LLM_API_KEY='',TG_TOKEN='',FEED_URL='',PRODUCT_CARDS=False).items():monkeypatch.setattr(config,k,v)
    class Never:
        def chat(self,*args,**kwargs): raise AssertionError('unexpected inference')
    def turn(text,sid=None):return loop.run_turn(sid,text,client_id='synthetic-quote',llm=Never())
    return turn,db


def assert_line(sid,sku,qty,total):
    q,=store.db().execute('select items_json from quotes where session_id=?',(sid,)).fetchall(); line,=json.loads(q[0]);assert (line['vendor_code'],line['qty'],line['sum'])==(sku,qty,total)
    assert len(list(config.QUOTES_DIR.glob('*.pdf')))==1 and not store.open_handoff(sid)


def test_year_chosen_on_followup(shop):
    turn,_=shop
    r=turn('Пришлите коммерческое предложение на семь именных мест «Синтетический планер» на год.');sid=r['session_id']
    assert 'PV-AN' in r['reply'] and 'PV-MO' not in r['reply'] and not r['events']
    state=store.find_session(sid)['items'][quote_choice.KEY];assert (state['qty'],state['months'])==(7,12)
    assert [e['type'] for e in turn('Да, годовую редакцию.',sid)['events']]==['quote']; assert_line(sid,'PV-AN',7,5180)


def test_two_annual_skus_need_disambiguation(shop):
    turn,db=shop
    with sqlite3.connect(db) as c:
        cols=[x[1] for x in c.execute('pragma table_info(offers)')];row=list(c.execute('select * from offers where id=71').fetchone());row[cols.index('id')]=73;row[cols.index('vendor_code')]='PV-PLUS';row[cols.index('version')]='Расширенная';row[cols.index('price')]=1000
        c.execute('insert into offers values('+','.join('?'*len(row))+')',row)
    r=turn('Нам нужно КП: Синтетический планер, 7 мест на год');sid=r['session_id']
    for msg in ['Годовую','Да']:
        r=turn(msg,sid);assert not r['events'] and 'PV-PLUS' in r['reply'] and 'PV-AN' in r['reply'] and not store.last_quote(sid)
    r=turn('Подтверждаю PV-PLUS',sid);assert r['events'][0]['type']=='quote';assert_line(sid,'PV-PLUS',7,7000)


def test_unknown_product_then_named_and_confirmed(shop):
    turn,_=shop;r=turn('Нам нужно коммерческое на семь мест на год');sid=r['session_id']
    assert not r['events'] and 'артикул' in r['reply']
    shown=turn('Синтетический планер',sid);assert 'PV-AN' in shown['reply'] and 'PV-MO' not in shown['reply'] and not shown['events']
    assert turn('Годовая подходит',sid)['events'][0]['type']=='quote';assert_line(sid,'PV-AN',7,5180)


def test_changed_price_needs_fresh_confirmation(shop):
    turn,db=shop;r=turn('Просим КП по артикулу PV-AN на семь мест на год');sid=r['session_id']
    with sqlite3.connect(db) as c:c.execute('update offers set price=810 where id=71')
    r=turn('Подтверждаю',sid);assert '810 ₽' in r['reply'] and not r['events']
    assert turn('Подтверждаю',sid)['events'][0]['type']=='quote';assert_line(sid,'PV-AN',7,5670)


def test_catalog_sku_before_help(shop):
    turn,_=shop;r=turn('Просим КП на артикул PV-MO на 3 места на месяц.');sid=r['session_id'];assert 'PV-MO' in r['reply'] and not r['events'];assert not turn('Подтверждаю',sid)['usage']['llm_calls'];assert_line(sid,'PV-MO',3,255)


@pytest.mark.parametrize('amount',['от 5 до 7','3 или 7','около 7','1,7','-7','0'])
def test_dubious_quantity_no_write(shop,amount):
    turn,_=shop;r=turn(f'Нужен КП на {amount} мест Синтетический планер на год');assert not r['events'] and not store.last_quote(r['session_id'])


def test_quantity_after_explicit_sku_selection(shop):
    turn,_=shop;r=turn('Запрашиваем КП по артикулу PV-AN');sid=r['session_id']
    assert not turn('Выбираю PV-AN',sid)['events']
    assert turn('9 шт.',sid)['events'][0]['type']=='quote';assert_line(sid,'PV-AN',9,6660)


def test_subject_change_expires_pending_request(shop):
    turn,_=shop;r=turn('Прошу КП: PV-AN, 4 места на год');sid=r['session_id']
    next_turn=turn('Как оформить заказ на сайте?',sid)
    assert quote_choice.KEY not in store.find_session(sid)['items'] and not next_turn['events'] and not store.last_quote(sid)


def test_terms_from_immediately_preceding_same_named_product(shop):
    turn,_=shop;sid=store.get_session(None,client_id='synthetic-quote')['id']
    store.add_message(sid,'user','Рассматриваем «Синтетический планер» на год, семь мест.')
    r=turn('Прошу КП по «Синтетическому планеру».',sid)
    state=store.find_session(sid)['items'][quote_choice.KEY]
    assert state['qty']==7 and state['months']==12 and 'PV-MO' not in r['reply']
    assert turn('Годовую',sid)['events'][0]['type']=='quote';assert_line(sid,'PV-AN',7,5180)


def test_cancel_and_actual_bill_do_not_issue_quote(shop):
    turn,_=shop;r=turn('Просим КП: PV-AN, 7 мест');sid=r['session_id'];cancel=turn('Отмена',sid);assert not cancel['events'] and quote_choice.KEY not in store.find_session(sid)['items']
    r=turn('Выставьте настоящий счёт на оплату семи мест.',sid);assert [e['type'] for e in r['events']]==['handoff'] and not store.last_quote(sid)
