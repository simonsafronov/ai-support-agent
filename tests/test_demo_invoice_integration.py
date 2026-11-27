"""Local integration: no model, real customers, Telegram transport or external databases."""
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app import config, notify, store
from app.agent.llm import Scripted
from app.quote import invoice
from app.web import server


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'ADMIN_TOKEN', 'local-test-only')
    monkeypatch.setattr(config, 'BRIDGE_DIR', str(tmp_path / 'bridge'))
    monkeypatch.setattr(config, 'TG_CHAT', '-100123')
    monkeypatch.setattr(notify, 'SETTLE', 0)
    calls = []

    def telegram(method, **params):
        calls.append((method, params))
        return {'message_id': len(calls)}

    monkeypatch.setattr(notify, 'call', telegram)
    monkeypatch.setattr(notify, '_invoice_action', server.approve_demo_invoice)
    monkeypatch.setattr(notify, '_invoice_state', server.demo_invoice_state)
    sid = store.get_session(None, 'test-browser-owner')['id']
    store.update_session(sid, facts={'компания': 'ООО Тест', 'ИНН': '7701234567', 'КПП': '770101001'})
    add_quote(sid, 1)
    return TestClient(server.app), sid, calls


def add_quote(sid, n):
    return store.add_quote(sid, f'КП-000{n}', 'ООО Тест', [{'offer_id': 1, 'name': 'Тест', 'qty': n,
                           'price': 50, 'sum': 50 * n, 'vat': None}], 50 * n, '')


ADMIN = {'x-admin-token': 'local-test-only'}
OWNER = {'client_id': 'test-browser-owner'}


INVOICE_REQUEST = 'Пришлите учебный счёт по этому КП в чат.'


@pytest.fixture
def invoice_model(monkeypatch):
    # Replace the model, not the chat endpoint, tool, document store or poll.
    monkeypatch.setattr(server.loop, 'get_llm', lambda: Scripted([
        {'name': 'make_invoice', 'args': {}}, 'Ответ по вашему запросу.'
    ]))
    monkeypatch.setattr(config, 'CHAT_RATE_PER_MIN', 0)


def ask_for_invoice(client, sid):
    response = client.post('/api/chat', json={**OWNER, 'session_id': sid, 'message': INVOICE_REQUEST})
    assert response.status_code == 200
    events = [json.loads(line) for line in response.iter_lines() if line]
    assert any(e['type'] == 'tool' and e['name'] == 'make_invoice' for e in events)
    assert events[-1]['type'] == 'reply' and events[-1]['session_id'] == sid
    assert not events[-1].get('review')


def api(client, sid):
    detail = client.get(f'/api/admin/session/{sid}', headers=ADMIN).json()
    return f'/api/admin/session/{sid}/demo-invoice', {'revision': detail['demo_invoice']['revision']}


def test_admin_approval_private_pdf_poll_reload_and_repeat(flow):
    browser, sid, _ = flow
    foreign = TestClient(server.app)
    url, body = api(browser, sid)
    assert browser.post(url, json=body).status_code == 403
    draft = invoice.prepare(sid)
    assert draft['ok']
    pdf = f"/api/session/{sid}/invoices/{draft['id']}.pdf"
    history = f'/api/session/{sid}'
    assert browser.get(history, params=OWNER).json()['messages'] == []
    assert browser.get(pdf).status_code == 404
    assert browser.get(draft['url'], headers=ADMIN).status_code == 404
    assert browser.get(history + '/answers', params=OWNER).json()['answers'] == []
    assert browser.get(f'/api/admin/session/{sid}').status_code == 404
    assert browser.post(url, json=body, headers=ADMIN).json()['ok']
    browser.post(url, json=body, headers=ADMIN)
    answers = browser.get(history + '/answers', params=OWNER).json()['answers']
    assert len(answers) == 1 and len(answers[0]['events']) == 1
    event = answers[0]['events'][0]
    assert event == {'type': 'demo_invoice', 'url': pdf}
    assert all(x not in str(answers) for x in ('7701234567', 'ООО Тест', 'test-browser-owner'))
    assert browser.get(history, params=OWNER).json()['messages'][0]['events'] == [event]
    assert browser.get(history + '/answers', params={**OWNER, 'after': answers[0]['id']}).json()['answers'] == []
    response = browser.get(pdf)
    assert response.content.startswith(b'%PDF') and response.headers['cache-control'] == 'private, no-store'
    from pypdf import PdfReader
    assert all('НЕ ДЛЯ ОПЛАТЫ' in page.extract_text().upper() for page in PdfReader(BytesIO(response.content)).pages)
    other = store.get_session(None, 'other-owner')['id']
    foreign.get('/api/session/' + other, params={'client_id': 'other-owner'})
    assert foreign.get(pdf).status_code == 404
    assert foreign.get(pdf.replace(sid, other)).status_code == 404
    assert foreign.get(history + '/answers', params={'client_id': 'other-owner'}).status_code == 404
    assert browser.get(draft['url'], params={'k': server.quote_key(draft['number'] + '.pdf')}).status_code == 404


def test_chat_delivers_unavailable_priced_item_without_approval(flow, monkeypatch, invoice_model):
    from pypdf import PdfReader
    from app.quote import kp

    browser, sid, _ = flow
    request = 'Нужно КП на три лицензии; прошу скидку 5%.'
    store.add_message(sid, 'user', request)
    offer = {'name': 'Лицензия А', 'available': False, 'price': '100.10'}
    monkeypatch.setattr(kp, '_offer', lambda _: offer)
    quote = kp.make_quote(sid, 'ООО Тест', [{'offer_id': 1, 'qty': 3}], discount_percent=5)
    assert quote['ok'] and quote['total'] == 285.30
    offer.update(available=True, price='200')  # счет использует снимок КП, а не новое состояние фида
    assert not server.review_on()  # default, no operator settings changed
    ask_for_invoice(browser, sid)
    assert store.open_handoff(sid) is None
    issued = invoice.public(store.last_invoice(sid))
    assert issued['status'] == invoice.ISSUED and issued['items'][0]['available'] is False
    assert issued['items'][0]['price'] == 95.10 and issued['total'] == quote['total']
    assert issued['vat_total'] == quote['vat_total'] == 51.45

    history = f'/api/session/{sid}'
    pdf = f"{history}/invoices/{issued['id']}.pdf"
    event = {'type': 'demo_invoice', 'url': pdf}
    answers = history + '/answers'
    before = browser.get(answers, params=OWNER).json()['answers']
    doc, = [m for m in before if event in m.get('events', [])]
    assert doc.get('from') != 'manager' and not doc.get('reviewed')
    response = browser.get(pdf)  # /answers alone must authorize the first click
    assert response.headers['content-type'] == 'application/pdf'
    messages = browser.get(history, params=OWNER).json()['messages']
    saved, = [m for m in messages if event in m['events']]
    assert (saved['id'], saved['content']) == (doc['id'], doc['content'])
    assert saved['role'] == 'assistant' and saved['from'] != 'manager'
    assert not browser.get(answers, params={**OWNER, 'after': doc['id']}).json()['answers']

    foreign = TestClient(server.app)
    other = store.get_session(None, 'other-owner')['id']
    other_owner = {'client_id': 'other-owner'}
    assert foreign.get(f'/api/session/{other}/answers', params=other_owner).json()['answers'] == []
    assert foreign.get(history, params=other_owner).status_code == 404
    assert foreign.get(answers, params=other_owner).status_code == 404
    assert foreign.get(pdf).status_code == foreign.get(pdf.replace(sid, other)).status_code == 404
    assert browser.get(issued['url'], params={'k': server.quote_key(issued['number'] + '.pdf')}).status_code == 404
    assert response.status_code == 200 and response.headers['cache-control'] == 'private, no-store'
    text = ' '.join(' '.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages).split())
    assert 'По фиду: нет в наличии' in text and 'Заказ не создан' in text
    assert 'поставк' in doc['content'].lower()
    for visible in (doc['content'], text):
        normalized = visible.lower().replace('ё', 'е')
        assert 'заказ подтвержден' not in normalized and 'заказ создан' not in normalized
    assert '1-3' not in text and '285,30' in text and 'НЕ ДЛЯ ОПЛАТЫ' in text
    assert store.messages(sid)[0]['content'] == request


@pytest.mark.parametrize('price, ambiguous, reason', [(None, False, 'по запросу'), (45, True, 'подтвердит')])
def test_price_questions_still_prevent_demo_invoice(flow, monkeypatch, invoice_model, price, ambiguous, reason):
    from app.quote import kp
    client, sid, _ = flow
    monkeypatch.setattr(kp, '_offer', lambda _: {'name': 'Вариант', 'available': False, 'price': price,
                                                'price_ambiguous': ambiguous})
    quote = kp.make_quote(sid, 'ООО Тест', [{'offer_id': 1, 'qty': 1}])
    assert quote['ok'] and quote['has_price_on_request'] is (price is None)
    assert bool(quote['price_to_confirm']) is ambiguous
    assert reason in invoice.prepare(sid)['error']
    url, body = api(client, sid)
    assert not client.post(url, json=body, headers=ADMIN).json()['ok']
    assert store.last_invoice(sid) is None and not store.messages(sid)
    ask_for_invoice(client, sid)
    history = f'/api/session/{sid}'
    assert store.last_invoice(sid) is None
    for messages in (client.get(history, params=OWNER).json()['messages'],
                     client.get(history + '/answers', params=OWNER).json()['answers']):
        assert not any(e['type'] == 'demo_invoice' for m in messages for e in m.get('events', []))


def test_global_reply_review_is_separate_and_opt_in(flow, monkeypatch):
    client, sid, _ = flow
    assert not server.review_on()
    store.set_setting(server.REVIEW, '1')
    monkeypatch.setattr(server.loop, 'get_llm', lambda: Scripted(['Ответ на проверке.']))
    monkeypatch.setattr(config, 'CHAT_RATE_PER_MIN', 0)
    response = client.post('/api/chat', json={**OWNER, 'session_id': sid, 'message': 'Спасибо за КП.'})
    assert response.status_code == 200
    reply = json.loads(response.text.splitlines()[-1])
    assert reply['type'] == 'reply' and reply['review'] and not reply['reply']
    history = client.get(f'/api/session/{sid}', params=OWNER).json()
    assert history['review'] and [m['role'] for m in history['messages']] == ['user']
    assert client.get(f'/api/session/{sid}/answers', params=OWNER).json()['answers'] == []


def test_trusted_current_telegram_card_only(flow):
    browser, sid, calls = flow
    hid = store.add_handoff(sid, 'демо-счёт', '', '')
    notify.sync()
    def button():
        params = next(p for method, p in reversed(calls) if method in ('sendMessage', 'editMessageText'))
        return next(b['callback_data'] for row in params['reply_markup']['inline_keyboard'] for b in row
                    if b.get('callback_data', '').startswith('inv:'))
    def click(data, msg=1, chat=-100123):
        return notify.handle({'callback_query': {'id': 'local', 'data': data, 'from': {'id': 7},
                              'message': {'message_id': msg, 'chat': {'id': chat}}}})
    first = button()
    assert click(first, chat=111) == 'ignored'
    assert click(first, msg=22) == 'ignored'
    add_quote(sid, 2)
    assert click(first) == 'stale'
    assert store.last_invoice(sid) is None
    notify.sync()
    current = button()
    assert current != first and click(current) == 'invoice'
    assert click(current) == 'stale'
    assert store.last_invoice(sid)['data']['quote_number'] == 'КП-0002'
    assert len(browser.get(f'/api/session/{sid}/answers', params=OWNER).json()['answers']) == 1
    assert all(method != 'sendDocument' for method, _ in calls)


def test_stale_admin_view_and_missing_requisites_do_not_publish(flow):
    client, sid, _ = flow
    url, body = api(client, sid)
    add_quote(sid, 2)
    assert not client.post(url, json=body, headers=ADMIN).json()['ok']
    store.update_session(sid, facts={})
    url, body = api(client, sid)
    assert not client.post(url, json=body, headers=ADMIN).json()['ok']
    assert store.last_invoice(sid) is None and not store.messages(sid)


def test_simultaneous_manager_clicks_publish_once(flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, sid, _ = flow
    url, body = api(client, sid)
    start = Barrier(2)
    def click():
        start.wait(timeout=10)
        return TestClient(server.app).post(url, json=body, headers=ADMIN).json()['ok']
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: click(), range(2))) == [False, True]
    assert len(store.messages(sid)) == 1
    assert store.db().execute('SELECT count(*) FROM invoices').fetchone()[0] == 1


@pytest.mark.small_catalog
def test_browser_requests_invoice_then_opens_polled_pdf_and_reloads(flow, monkeypatch, invoice_model):
    """Optional UI smoke: loopback, synthetic data and fresh profiles only. # BROWSER-OK"""
    import shutil
    import socket
    import threading
    import time
    import uvicorn
    pw = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium')
    if not chromium:
        pytest.skip('local Chromium not installed')
    _, sid, _ = flow
    monkeypatch.setattr(config, 'TG_TOKEN', '')
    monkeypatch.setattr(server.kb, 'stats', lambda: {'chunks': 0})
    monkeypatch.setattr(server.llm, 'model_online', lambda: True)
    monkeypatch.setattr(server.catalog_update, 'prices_at', lambda: None)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        local = uvicorn.Server(uvicorn.Config(server.app, lifespan='off', access_log=False, log_level='error'))
        thread = threading.Thread(target=local.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if local.started:
                    break
                time.sleep(.01)
            assert local.started
            with pw.sync_playwright() as runner:
                browser = runner.chromium.launch(executable_path=chromium, args=['--disable-background-networking'])
                customer = browser.new_context()
                customer.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
                chat = customer.new_page()
                chat.add_init_script(f"localStorage.setItem('session','{sid}');localStorage.setItem('client','test-browser-owner')")
                polls = []
                poll_url = f'{base}/api/session/{sid}/answers?'
                chat.on('response', lambda r: polls.append(r) if r.url.startswith(poll_url) else None)
                chat.goto(base)
                pdf = chat.get_by_role('link', name='Открыть PDF')
                assert pdf.count() == 0
                chat.get_by_role('textbox', name='Сообщение').fill(INVOICE_REQUEST)
                with chat.expect_response(lambda r: r.url == base + '/api/chat') as sent:
                    chat.get_by_role('button', name='Отправить').click()
                assert sent.value.status == 200
                pdf.wait_for(timeout=15000)  # first chat stays open; /answers grants the document cookie
                href = base + pdf.get_attribute('href')
                assert href.startswith(f'{base}/api/session/{sid}/invoices/')
                assert chat.locator('#log .mgr').count() == 0
                assert store.open_handoff(sid) is None
                with customer.expect_event('response', predicate=lambda r: r.url == href) as opened:
                    pdf.click()
                assert opened.value.status == 200 and opened.value.headers['content-type'] == 'application/pdf'
                assert any(base + e.get('url', '') == href for poll in polls
                           for m in poll.json()['answers'] for e in m.get('events', []))
                foreign = browser.new_context()
                assert foreign.request.get(href).status == 404
                chat.reload()
                pdf.wait_for(timeout=10000)
                assert pdf.count() == 1 and base + pdf.get_attribute('href') == href
                assert chat.locator('#log .mgr').count() == 0
                browser.close()
        finally:
            local.should_exit = True
            thread.join(timeout=5)
