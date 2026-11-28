"""Disposable synthetic feed, APP_DB and PDF directory from conftest; no model/socket."""
import json

import pytest

from app import store
from app.agent import compact_spec, tools

pytestmark = pytest.mark.small_catalog

MIXED = '''Прошу КП по списку:
1. EES-NEW-5 ESET PROTECT Entry — 2 шт.
2. KL4863RADFS Kaspersky Endpoint Security — 25 шт.
3. OS1800X86О0TWS Astra Linux Special Edition 1.8 — 1 шт.
4. CB-SRV-1 Кибер Бэкап Стандартный для сервера — 3 шт.
5. Astra Linux Special Edition 1.8 — 1 шт.
6. Вымышленная Саламандра 2029 — 4 шт.'''

@pytest.fixture
def visit():
    sid = store.get_session(None, 'synthetic-list')['id']
    def send(text):
        session = store.get_session(sid)
        items = session['items']
        ctx = tools.ToolContext(sid, user_request=text)
        store.add_message(sid, 'user', text)
        result = compact_spec.handle(text, items, lambda name, args: tools.call(ctx, name, args))
        store.update_session(sid, items=items)
        return result, ctx.events
    return sid, send


def test_mixed_list_followup_and_only_complete_quote(visit):
    sid, send = visit
    reply, events = send(MIXED)
    assert not events and not store.last_quote(sid)
    for i in range(1, 7):
        assert f'{i}.' in reply
    assert 'несколько' in reply or 'вариант' in reply
    assert 'нет в каталоге' in reply and 'EES-NEW-5' in reply
    first, _ = send('Строка 5: OS1800X86В1TWS Astra Linux Special Edition 1.8 — 1 шт.')
    assert 'EES-NEW-5' in first and 'Вымышленная Саламандра' in first
    assert not store.last_quote(sid)
    blocked, _ = send('Сформируйте КП по проверенному списку')
    assert 'не сформировано' in blocked and not store.last_quote(sid)
    final, _ = send('Строка 6: MO-STD-1 — 4 шт.')
    assert '6.' in final and not store.last_quote(sid)
    issued, events = send('Сформируйте КП по проверенному списку')
    assert [e['type'] for e in events] == ['quote'] and 'Заказ не создан' in issued
    quote = store.last_quote(sid)
    lines = json.loads(quote['items_json'])
    assert len(lines) == 6 and [r['qty'] for r in lines] == [2,25,1,3,1,4]
    assert not send('Сформируйте КП по проверенному списку')[1]


def test_versions_skus_and_seats_are_not_purchase_quantities(visit):
    sid, send = visit
    reply, _ = send('КП по списку:\n1. MS-OFF-1 Microsoft Office 2024\n2. MO-STD-10 МойОфис на 10 пользователей')
    assert reply.count('количество') >= 2
    assert not store.last_quote(sid)


def test_conflicting_sku_and_edition_never_creates_quote(visit):
    sid, send = visit
    reply, _ = send('Прошу КП по списку:\n1. OS1800X86О0TWS Astra Linux Воронеж — 1 шт.\n2. MS-OFF-2 — 1 шт.')
    assert 'не совпада' in reply and not store.last_quote(sid)
    assert not send('Сформируйте КП по проверенному списку')[1]


def test_ordinary_order_not_intercepted(visit):
    _, send = visit
    assert send('КП на MS-OFF-1, 2 шт.') == (None, [])
    assert send('Строка 2: MS-OFF-1, 1 шт.') == (None, [])



def test_over_limit_or_unparseable_rows_are_not_silently_dropped(visit):
    sid, send = visit
    reply, _ = send('КП по списку:\n1. MO-STD-1 — 1 шт.\n2. 12345')
    assert '2. 12345 → не разобрано' in reply
    assert not send('Сформируйте КП по проверенному списку')[1]
    reply, _ = send('КП по списку:\n'+'MO-STD-1 — 1 шт.\n'*31)
    assert 'до 30' in reply and not store.last_quote(sid)
    assert send('Сформируйте КП по проверенному списку') == (None, [])


def test_preview_cannot_treat_duplicate_sku_as_two_prices(visit):
    sid, send = visit
    reply, _ = send('КП по списку:\n1. MO-STD-1 — 1 шт.\n2. MO-STD-1 — 2 шт.')
    assert 'повторяется' in reply and not store.last_quote(sid)
    assert not send('Сформируйте КП по проверенному списку')[1]


@pytest.mark.parametrize('literal', ['x', 'х', '×'])
def test_single_line_quote_equivalent_not_an_action(visit, literal):
    sid, send = visit
    lines = MIXED.splitlines()
    import re
    compact = lines[0] + ' ' + '; '.join(re.sub(r'— (\d+) шт\.', lambda m: literal + m[1], r) for r in lines[1:])
    answer, events = send(compact)
    assert '5.' in answer and '6.' in answer and 'нет в каталоге' in answer
    assert len(store.get_session(sid)['items']['compact_quote_list']['rows']) == 6
    assert [r['qty'] for r in store.get_session(sid)['items']['compact_quote_list']['rows']] == [2,25,1,3,1,4]
    assert not events and not store.last_quote(sid)


def test_sku_embedded_x_number_is_not_a_quantity(visit):
    sid, send = visit
    answer,_ = send('Прошу КП по списку: Product-X28; Система 1.8')
    assert answer.count('уточните количество') == 2 and not store.last_quote(sid)


def test_parent_name_and_unavailable_priced_offer(tmp_path, small_catalog_db, monkeypatch, visit):
    import shutil
    import sqlite3
    from app import config
    # Modify only a private copy of the synthetic database, never the shared fixture.
    copy = tmp_path/'separate.sqlite'
    shutil.copyfile(small_catalog_db, copy)
    with sqlite3.connect(copy) as db:
        db.execute("UPDATE offers SET name='Редакция Орион',available=0 WHERE vendor_code='MO-STD-1'")
        db.execute("UPDATE products SET title='Пакет Бухгалтера' WHERE product_id=(select product_id from offers where vendor_code='MO-STD-1')")
    monkeypatch.setenv('CATALOG_DB',str(copy)); monkeypatch.setattr(config,'CATALOG_DB',copy)
    sid, send = visit
    reply,_ = send('КП по списку: MO-STD-1 Пакет Бухгалтера Орион x4; EES-NEW-5 x2')
    assert 'Все строки проверены' in reply and 'Поставк'.lower() in reply.lower() and not store.last_quote(sid)
    wrong,_ = send('Строка 1: MO-STD-1 Пакет Бухгалтера Сириус x4')
    assert 'не совпадают' in wrong
    send('Строка 1: MO-STD-1 Пакет Бухгалтера Орион x4')
    quote, events = send('Сформируйте КП по проверенному списку')
    assert [e['type'] for e in events] == ['quote'] and 'поставку нужно подтвердить' in quote
    lines = json.loads(store.last_quote(sid)['items_json'])
    assert not lines[0]['available'] and lines[0]['price'] > 0 and lines[0]['qty'] == 4
