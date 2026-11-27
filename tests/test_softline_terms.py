"""Правила Softline: проверяем расчёт, передачу и то, что увидит клиент, без сети."""
import json

import pytest

from app import store, webim
from app.agent import loop, tools
from app.agent.llm import Scripted
from app.catalog import search
from eval import quote_check

pytestmark = pytest.mark.small_catalog


@pytest.mark.parametrize('percent,expected', [(0, 123), ('3,5%', 118.70), (5, 116.85), (7, 116.85)])
def test_feed_status_is_independent_of_coupon_and_price(monkeypatch, percent, expected):
    sid = store.get_session(None)['id']
    offer = dict(offer_id=1, name='Пакет', price=123, available=False)
    monkeypatch.setattr(search, 'get_offer', lambda _: offer)
    result = tools.call(tools.ToolContext(sid), 'calc_total', {
        'items': [{'offer_id': 1, 'qty': 2}], 'discount_percent': percent})
    line, = result['lines']
    assert line['available'] is False and line['availability_note'] == 'По фиду: нет в наличии'
    assert result['total'] == expected * 2 and result['price_on_request'] == []
    assert 'цену этой позиции подтвердит' not in result['note']
    card = webim.card_text({'type': 'offer', 'title': 'Пакет', **line})
    assert '123 ₽' in card and 'По фиду: нет в наличии' in card


def test_known_price_quote_without_review_or_coupon():
    product = search.search_products('Kaspersky Endpoint Security', only_available=True)['products'][0]
    offer = next(o for o in search.get_product(product['product_id'])['offers'] if o['price'] and o['available'])
    items = [{'offer_id': offer['offer_id'], 'qty': offer.get('qty_min') or 1}]
    res = loop.run_turn(None, 'Оформите КП со скидкой 5%, купона нет.', llm=Scripted([
        {'name': 'calc_total', 'args': {'items': items, 'discount_percent': 5}},
        {'name': 'make_quote', 'args': {'items': items}}, 'КП готово.']))
    calc, quote = [t['output'] for t in res['trace'] if t['kind'] == 'tool']
    assert calc['discount_percent'] == quote['discount_percent'] == 5
    assert quote['ok'] and calc['total'] == quote['total']
    assert [e['type'] for e in res['events']] == ['quote'] and not store.open_handoff(res['session_id'])
    assert 'проверит менеджер' not in quote['note']


def test_deferral_is_not_a_discount_request():
    request = 'Нужно КП на неделю. Можно отсрочку платежа на 30 дней?'
    result = loop.run_turn(None, request, llm=Scripted([
        {'name': 'calc_total', 'args': {'items': [], 'discount_percent': 5}},
        {'name': 'handoff_to_manager', 'args': {'reason': 'Отсрочка', 'summary': 'Просит отсрочку на 30 дней.'}},
        'Передал вопрос об отсрочке менеджеру. [[facts: discount=5%]]']))
    assert 'отсроч' in result['trace'][0]['output']['error'].lower()
    sid = result['session_id']
    assert 'скидка' not in store.get_session(sid)['facts'] and store.open_handoff(sid)
    assert next(m['content'] for m in store.messages(sid) if m['role'] == 'user') == request


@pytest.mark.parametrize('rate,valid', [(0, True), (5, True), (5.01, False), (-1, False), ('NaN', False)])
def test_independent_quote_validation(rate, valid):
    q = {'items': [], 'total': 0, 'discount_percent': rate}
    assert (not quote_check.arithmetic(q)) is valid


def test_mixed_availability_has_no_shared_delivery():
    rows = [{'name': 'Продукт', 'params_json': json.dumps({'Особенности доставки': 'от 3 раб.дн.'})}] * 2
    offers = [{'name': 'Продукт', 'available': value} for value in (True, False)]
    assert 'delivery' not in search._with_details(rows, offers)
    assert offers[0].get('delivery') and 'delivery' not in offers[1]


@pytest.mark.parametrize('question', ['Можно отсрочку платежа на 30 дней?', 'Работаем только по постоплате'])
def test_direct_deferral_opens_real_ticket_even_if_model_would_promise(question):
    model = Scripted(['Конечно, предоставим отсрочку и скидку 5%.'])
    result = loop.run_turn(None, question, llm=model)
    assert not model.calls and result['trace'][0]['name'] == 'handoff_to_manager'
    assert result['trace'][0]['output']['ok'] and store.open_handoff(result['session_id'])
    assert 'не подтверждены' in result['reply'] and 'скидк' not in result['reply']
