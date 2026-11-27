"""Independent offline retail paraphrases against the unchanged synthetic recheck databases.
Set NAMED_FIXTURES to the directory containing the two read-only SQLite copies.
"""
import json
import os
from pathlib import Path

import pytest
from app import config, store
from app.agent import loop, consult_named
from app.kb import search as kb


class NoModel:
    def chat(self,*args,**kwargs):
        pytest.fail('named consultation unexpectedly needed inference')


@pytest.fixture
def turn(monkeypatch):
    folder=os.environ.get('NAMED_FIXTURES')
    if not folder: pytest.skip('requires isolated synthetic catalog and KB snapshot')
    db=Path(folder)/'catalog.sqlite'; knowledge=Path(folder)/'kb.sqlite'
    assert db.is_file() and knowledge.is_file()
    monkeypatch.setenv('CATALOG_DB',str(db));monkeypatch.setattr(config,'CATALOG_DB',db)
    monkeypatch.setattr(kb,'DB_PATH',knowledge)
    for key,value in dict(COMPACT_LOCAL=True,LLM_PROVIDER='openai',LLM_BASE_URL='http://127.0.0.1:18347/v1',LLM_API_KEY='').items():monkeypatch.setattr(config,key,value)
    return lambda text,sid=None: loop.run_turn(sid,text,client_id='synthetic-offline',llm=NoModel())


@pytest.mark.parametrize(('phrase','present'),[
    ('Для запуска «Кадроряда» хватит ОЗУ 8 ГБ?', ['12 ГБ','не соответствует','версии','Источник:']),
    ('Совместим ли «Листограф 4» с чертежами прежней версии: останутся слои?', ['сло','копии файла']),
    ('Сравните «Перьевую мастерскую» и «Листограф» для печати.', ['Перьевая мастерская','Листограф 4','цветоделени']),
    ('Продлевается ли «Нотная полка» на прежнее число пользователей?', ['продлен','номер подписки или заказа']),
    ('Можно расширить «Нотную полку» без сдвига конца подписки?', ['дополнительных местах','дат окончания','подписки или заказа']),
    ('Перечислите тарифы «Схемника» для работы с библиотекой шаблонов.', ['Схемник Старт','Схемник Команда','шаблон']),
    ('Есть ли в наличии физический носитель «Палитра двора»?', ['Электронная','наличие не подтверждено']),
    ('Сколько стоит «Схемник Старт» на год на одного пользователя?', ['1500 ₽','1 шт.']),
    ('Посчитайте стоимость «Листографа» для группы.', ['Листограф 4','сколько мест','срок']),
])
def test_named_facts(turn,phrase,present):
    r=turn(phrase);assert all(s.lower() in r['reply'].lower() for s in present),r['reply']
    assert r['usage']['llm_calls']==0 and not r['events']


def test_natural_quote_and_separate_correction(turn):
    made=turn('Подготовьте КП на четыре годовые лицензии «Букволита».')
    assert [e['type'] for e in made['events']]==['quote'],made['reply']
    line,=json.loads(store.last_quote(made['session_id'])['items_json'])
    assert (line['vendor_code'],line['qty'],line['sum'])==('БУК-1',4,8400)
    changed=turn('Не четыре места, а два. Посчитайте стоимость на год.',made['session_id'])
    assert '4200 ₽' in changed['reply'] and not changed['events']
    assert json.loads(store.last_quote(made['session_id'])['items_json'])[0]['qty']==4


@pytest.mark.parametrize(('confirmation','created'),[('Да, сформируйте.',True),('Не надо.',False)])
def test_quote_confirmation_only_for_this_dialogue(turn,confirmation,created):
    r=turn('Для согласования нужно КП на пять годовых мест «Букволита».')
    assert '5 шт.' in r['reply'] and not r['events'] and not store.last_quote(r['session_id'])
    reply=turn(confirmation,r['session_id'])
    assert bool(store.last_quote(r['session_id']))==created
    assert bool(reply['events'])==created


def test_unknown_named_product_cannot_use_previous_quote(turn):
    r=turn('Подготовьте КП на три годовые лицензии «Букволита».')
    second=turn('Сделайте КП на три лицензии «Несуществующее издание».',r['session_id'])
    assert 'артикул' in second['reply'] and not second['events']


def test_unspecified_choice_asks_task_without_sale(turn):
    r=turn('Не понимаю, какую программу выбрать для повседневной работы.')
    assert 'результат' in r['reply'] and not r['events'] and not r['trace']


@pytest.mark.parametrize(('next_message','fragment'), [('Проверил другую редакцию, всё заработало.','больше ничего'), ('После проверки всё равно ошибка.','не устранил')])
def test_support_update_before_product_search(turn,next_message,fragment):
    sid=store.get_session(None,client_id='synthetic-offline')['id']
    store.add_message(sid,'user','Возникла ошибка при активации, нужна помощь.')
    r=turn(next_message,sid)
    assert fragment in r['reply'] and not r['trace'] and not r['events']


def test_checkout_with_named_product_is_buyer_help(turn):
    prompt='Хочу оформить заказ на одну лицензию «Букволита». Какие следующие шаги?'
    r=turn(prompt); tool,=r['trace']
    assert tool['name']=='kb_search' and tool['input']=={'query':prompt,'buyer_help':True}
    assert 'корзин' in r['reply'].lower() and 'заказ не создаёт' in r['reply'] and not r['events']


@pytest.mark.parametrize('case_id',['T1','T2','T3','B1','B2','B3','E1','E2'])
def test_accepted_consultation_dialogues_remain_grounded(turn,case_id):
    from app.catalog import search
    from app.agent import compact_order
    base=Path(os.environ['NAMED_FIXTURES']); specs=json.loads((base/'snapshot/eval/retail-team11-v1.json').read_text())['cases']
    spec=next(c for c in specs if c['id']==case_id)
    sid=store.get_session(None,client_id='synthetic-offline')['id']
    notes={'T2':'Мой продукт — Нотная полка.','B1':'Мы рассматриваем продукт Страницы Автор.','B2':'Рассматриваем продукты Компас.','B3':'Нас интересует Чертёж Конкурентный.','E2':'Мы выбрали годовой артикул БУК-1 продукта Букволит.'}
    if case_id in notes:store.add_message(sid,'user',notes[case_id])
    if case_id=='E2':
        offer=search.get_offer(8009);state={};compact_order.bind_search(state,[offer],offer['product_id']);state['compact_selected']=offer;store.update_session(sid,items=state)
    results=[turn(q,sid) for q in spec['turns']]; answers=[r['reply'] for r in results]
    if case_id in ('B1','B2','B3'):
        sku,qty,total={'B1':('СТР-А',4,12000),'B2':('КОМ-С',8,11200),'B3':('ЧЕР-К',12,30000)}[case_id]
        line,=json.loads(store.last_quote(sid)['items_json']);assert (line['vendor_code'],line['qty'],line['sum'])==(sku,qty,total)
    if case_id=='B1':assert 'только читают' in answers[0] and 'бесплат' in answers[1]
    if case_id=='B2':assert 'КОМ-О' in answers[0] and 'КОМ-С' in answers[1] and 'не подтверждена' in answers[2]
    if case_id=='B3':assert 'одновременно' in answers[0] and '12 шт.' in answers[1] and 'не включена' in answers[2]
    if case_id=='T1':assert '?' in answers[0] and 'сохранить' in answers[1] and 'не нужно' in answers[2] and not any(r['events'] for r in results)
    if case_id=='T2':assert '1 шт.' in answers[0] and 'два устройства' in answers[0] and '2 шт.' in answers[1]
    if case_id=='T3':assert 'ГОЛ-WIN' in answers[0] and 'нет позиции' in answers[1] and 'macos' in answers[1]
    if case_id=='E1':assert 'PDF-EDIT' in answers[0] and 'PDF-VIEW' not in answers[0]
    if case_id=='E2':assert '6300 ₽' in answers[0] and '3 шт.' in answers[0]


def test_property_source_does_not_match_unrelated_edition_or_host():
    item=dict(url='https://docs.softline.com/ru-ru/lk/sample',title='Продукт Дом',heading='',snippet='Оперативная память 2 ГБ.')
    assert not consult_named._official(item,'Продукт Про','память')
    assert not consult_named._official(dict(item,url='https://other.invalid'), 'Продукт Дом','память')
