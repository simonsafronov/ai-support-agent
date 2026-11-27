import pytest

from app import store
from app.agent.tools import BY_NAME, ToolContext
from app.quote import invoice


REQUEST = 'Пришлите учебный счёт по моему КП в этот чат.'
CUSTOMER = {'компания': 'Организация', 'ИНН': '7701234567', 'КПП': '770101001'}
make_invoice = BY_NAME['make_invoice']['handler']


def request_context(sid):
    store.add_message(sid, 'user', REQUEST)
    return ToolContext(sid, facts=store.get_session(sid)['facts'], user_request=REQUEST)


def add_priced_quote(sid):
    store.add_quote(sid, 'КП-123', 'Организация', [{'offer_id': 1, 'name': 'Лицензия', 'qty': 1,
                    'price': 122, 'sum': 122, 'vat': 22}], 122, '')


def test_request_needs_quote_and_customer_details():
    sid = store.get_session(None, 'test-browser-owner')['id']
    ctx = request_context(sid)
    assert not make_invoice(ctx)['ok'] and not ctx.events
    add_priced_quote(sid)
    assert make_invoice(ctx)['customer_missing'] and not ctx.events
    assert store.last_invoice(sid) is None and store.open_handoff(sid) is None
    assert [m['role'] for m in store.messages(sid)] == ['user']


def test_priced_quote_alone_is_not_an_invoice_request():
    sid = store.get_session(None, 'test-browser-owner')['id']
    add_priced_quote(sid)
    store.update_session(sid, facts=CUSTOMER)
    text = 'Покажите позиции моего КП.'
    store.add_message(sid, 'user', text)
    ctx = ToolContext(sid, facts=CUSTOMER.copy(), user_request=text)
    assert not make_invoice(ctx).get('ok')
    assert not ctx.events and store.open_handoff(sid) is None
    assert [m['role'] for m in store.messages(sid)] == ['user']


def test_repeat_request_delivers_one_assistant_document_without_handoff():
    sid = store.get_session(None, 'test-browser-owner')['id']
    add_priced_quote(sid)
    store.update_session(sid, facts=CUSTOMER)
    previous = None
    for _ in range(2):
        ctx = request_context(sid)  # a new turn, not the loop's same-turn tool-call cache
        result = make_invoice(ctx)
        if previous is None:
            assert result['ok']
        assert not {'ticket', 'number', 'url', 'customer', 'items'} & result.keys()
        assert '7701234567' not in str(result)
        assert not any(e['type'] == 'handoff' for e in ctx.events)
        assert store.open_handoff(sid) is None
        issued = store.last_invoice(sid)
        assert issued and issued['status'] == invoice.ISSUED
        doc, = [m for m in store.messages(sid) if m['role'] == 'assistant'
                and any(e['type'] == 'demo_invoice' for e in m['meta'].get('events', []))]
        assert doc['meta'].get('from') != 'manager'
        assert doc['meta']['events'] == [{'type': 'demo_invoice', 'url': f"/api/session/{sid}/invoices/{issued['id']}.pdf"}]
        identity = issued['id'], doc['id']
        if previous is not None:
            assert identity == previous
        previous = identity


@pytest.mark.parametrize('text,issued', [
    ('Покажите учебный счет, не для оплаты.', True),
    ('Пришлите счет в этот чат.', True),
    ('Как получить учебный счет?', False),
    ('Не присылайте учебный счет.', False),
    ('Выставьте настоящий счет на оплату.', False),
])
def test_issue_only_requested_sample(text, issued):
    sid = store.get_session(None, 'private-owner')['id']
    add_priced_quote(sid)
    store.update_session(sid, facts=CUSTOMER)
    store.add_message(sid, 'user', text)
    out = make_invoice(ToolContext(sid))
    assert out['ok'] is issued and bool(store.last_invoice(sid)) is issued


@pytest.mark.parametrize('client', ['', 'demo', 'eval-private', 'webim-test'])
def test_sample_is_not_published_in_a_nonprivate_channel(client):
    sid = store.get_session(None, client)['id']
    add_priced_quote(sid)
    store.update_session(sid, facts=CUSTOMER)
    assert not make_invoice(request_context(sid))['ok'] and not store.last_invoice(sid)
