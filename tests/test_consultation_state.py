"""Additional compact consultation regressions; the acceptance suite stays frozen."""
import pytest

from app.agent import consult, consult_catalog, compact_order


@pytest.mark.parametrize(('message','facts'),[
    ('В штате 31 сотрудник. Пик — 9 одновременных подключений.',{'people':31,'peak':9}),
    ('Не восемь мест, а три, на год.',{'people':3,'months':12}),
    ('Для семи пользователей нужен собственный сервер.',{'people':7,'mode':'server'}),
    ('Четыре автора и девять читателей.',{'authors':4,'readers':9}),
    ('Только Linux; Windows использовать нельзя.',{'os':'linux'}),
])
def test_separate_units_and_latest_correction(message,facts):
    assert consult.parse(message)==facts


@pytest.mark.parametrize(('state','card','expected'),[
    ({'people':15,'authors':5,'readers':10},'оплачиваются только авторы. для читателей доступ бесплатный.',5),
    ({'people':15,'authors':5,'readers':8},'оплачиваются только авторы. для читателей доступ бесплатный.',None),
    ({'people':15,'authors':5,'readers':10},'для читателей доступ по заявке',None),
    ({'people':15,'authors':5,'readers':10},'платят только авторы. чтение бесплатно только если заключен договор',None),
    ({'people':20,'shifts':2},'цена за одновременное подключение',None),
    ({'people':20,'shifts':2,'peak':13},'цена за одновременное подключение',13),
    ({'people':2,'devices':5},'именная лицензия',2),
    ({'people':7},'цена за комплект',None),
])
def test_no_guessing_billing_units(state,card,expected):
    assert consult._qty(state,card)[0]==expected


@pytest.mark.parametrize('raw',['Не делайте КП','Если можно, подготовьте КП','Сделайте КП со скидкой 20%', 'Как сделать КП?','Сделайте КП на другой артикул'])
def test_compound_and_uncertain_writes_not_authorized(raw):
    assert not consult._draft(raw)


def test_normal_search_invalidates_consultation():
    state={'consult':{'pid':'old','people':10},'compact_selected':{'offer_id':22},'compact_lines':[{'offer_id':22,'qty':10}]}
    compact_order.bind_search(state,[],42)
    assert 'consult' not in state and not state.get('compact_lines')


def test_viewer_is_not_editor():
    assert not consult_catalog.supported({'name':'Просмотр PDF','description':'Только просмотр; без редактирования текста PDF.'},'pdf_edit')
