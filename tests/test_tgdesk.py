"""Only synthetic Telegram launch data; TestClient without application lifespan/pollers."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import config, store
from app.web.server import app

TOKEN = '123456789:only-local-test-token'
MANAGER = 700000001
ROOT = '/api/tgdesk/handoffs'
client = TestClient(app)


def signed(user=MANAGER, age=0, token=TOKEN, **extra):
    fields = {'auth_date': str(int(time.time()) - age), 'query_id': 'abc+123=',
              'user': json.dumps({'id': user, 'first_name': 'Тест'}, ensure_ascii=False),
              'signature': 'test_signature', **extra}
    check = '\n'.join('='.join(item) for item in sorted(fields.items()))
    key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth(raw=None):
    return {'Authorization': 'tma ' + (signed() if raw is None else raw)}


@pytest.fixture(autouse=True)
def local_config(monkeypatch):
    monkeypatch.setattr(config, 'TG_TOKEN', TOKEN)
    monkeypatch.setenv('TG_MANAGER_IDS', f'700000002, {MANAGER}')


@pytest.fixture
def case():
    def create(sid, text, number):
        store.get_session(sid, 'test-browser-' + sid)
        store.add_message(sid, 'user', text)
        hid = store.add_handoff(sid, 'Согласовать условия', 'Клиент запросил скидку', 'person@example.invalid')
        config.QUOTES_DIR.mkdir(exist_ok=True)
        path = config.QUOTES_DIR / (number + '.pdf')
        path.write_bytes(b'%PDF-test')
        qid = store.add_quote(sid, number, 'Тест', [], 100, str(path))
        return hid, qid
    return create


@pytest.mark.parametrize('raw', [lambda: '', lambda: signed(age=301), lambda: signed(age=-60),
    lambda: signed(token='wrong'), lambda: signed().replace('abc%2B','def%2B'),
    lambda: signed() + '&auth_date=123', lambda: signed(user=str(MANAGER)), lambda: signed(user=True),
    lambda: signed(user=42, chat=json.dumps({'id': MANAGER}), username=str(MANAGER))])
def test_no_card_for_invalid_or_unlisted_launch(raw):
    res = client.get(ROOT+'/1', headers=auth(raw()))
    assert res.status_code in (401, 403)
    assert 'no-store' in res.headers['Cache-Control']


@pytest.mark.parametrize('ids', ['', '*,700000001', '700000001x', '1700000001'])
def test_no_fallback_to_chat_or_admin(monkeypatch, ids):
    monkeypatch.setenv('TG_MANAGER_IDS', ids)
    monkeypatch.setattr(config, 'TG_CHAT', str(MANAGER))
    assert client.get(ROOT+'/1', headers=auth()).status_code == 403


def test_token_required_and_url_is_not_auth(monkeypatch):
    assert client.get(ROOT+'/1', params={'initData': signed()}).status_code == 401
    monkeypatch.setattr(config, 'TG_TOKEN', '')
    assert client.get(ROOT+'/1', headers=auth()).status_code == 401


def test_privacy_scope_and_standard_reply(case):
    hid, doc = case('local-a', 'Текст первого клиента', 'КП-20261127-0042')
    other, other_doc = case('local-b', 'Посторонняя сессия', 'КП-20261127-0043')
    url = f'{ROOT}/{hid}'
    store.add_message('local-a', 'system', 'Внутренняя инструкция')
    res = client.get(url, headers=auth())
    assert res.status_code == 200 and res.json()['request'] == 'Текст первого клиента'
    assert 'Посторонняя' not in res.text and 'Внутренняя' not in res.text and 'test-browser' not in res.text
    assert 'no-store' in res.headers['Cache-Control']
    def pdf(qid):
        return f'{url}/documents/quote/{qid}'
    assert client.get(pdf(doc), headers=auth()).content == b'%PDF-test'
    assert client.get(pdf(other_doc), headers=auth()).status_code == 404
    assert client.get(pdf(doc)).status_code == 401
    answer = {'answer': 'Готово'}
    assert client.post(url+'/answer', json=answer).status_code == 401
    assert client.post(url+'/answer', headers=auth(signed(user=123)), json=answer).status_code == 403
    assert client.post(url+'/answer', headers=auth(), json={**answer, 'session_id':'local-b'}).status_code == 422
    assert client.post(url+'/answer', headers=auth(), json=answer).status_code == 200
    assert store.messages('local-a')[-1]['meta'] == {'from':'manager'}
    poll = client.get('/api/session/local-a/answers', params={'client_id':'test-browser-local-a'})
    assert [a['content'] for a in poll.json()['answers']] == ['Ответ менеджера: Готово']
    assert client.get('/api/session/local-b/answers', params={'client_id':'test-browser-local-b'}).json()['answers'] == []
    assert len(store.messages('local-b')) == 1 and store.open_handoff('local-b')['id'] == other
    assert client.post(url+'/answer', headers=auth(), json={'answer':'Повтор'}).status_code == 409
    store.add_message('local-a', 'user', 'Запрос второго обращения')
    newer = store.add_handoff('local-a', 'Новое', '', '')
    assert client.get(url, headers=auth()).json()['request'] == 'Текст первого клиента'
    assert client.get(f'{ROOT}/{newer}', headers=auth()).json()['request'] == 'Запрос второго обращения'


def test_invoice_scope_and_filesystem_boundary(case, tmp_path):
    hid, qid = case('ticket-a', 'Нужен счёт', 'КП-1')
    other, _ = case('ticket-b', 'Другое обращение', 'КП-2')
    iid = store.add_invoice('ticket-a', 'СЧ-1', {})
    doc = f'/documents/invoice/{iid}'
    path = config.QUOTES_DIR / 'СЧ-1.pdf'; path.write_bytes(b'%PDF-invoice')
    assert client.get(f'{ROOT}/{hid}'+doc, headers=auth()).status_code == 404
    store.issue_invoice(iid, 'тест')
    assert client.get(f'{ROOT}/{hid}'+doc, headers=auth()).content == b'%PDF-invoice'
    assert client.get(f'{ROOT}/{other}'+doc, headers=auth()).status_code == 404
    outside = tmp_path/'not-a-quote.pdf'; outside.write_bytes(b'private')
    path.unlink(); path.symlink_to(outside)
    assert client.get(f'{ROOT}/{hid}'+doc, headers=auth()).status_code == 404


def test_launch_shell_is_public_but_not_an_admin_credential(case, monkeypatch):
    hid, _ = case('ticket', 'Закрытый текст', 'КП-1')
    monkeypatch.setattr(config, 'ADMIN_TOKEN', 'local-admin-only')
    shell = client.get('/tgdesk', params={'handoff':hid})
    assert shell.status_code == 200 and 'web.telegram.org' in shell.headers['Content-Security-Policy']
    assert 'Закрытый текст' not in shell.text and 'example.invalid' not in shell.text
    assert client.post(f'/api/admin/handoff/{hid}/answer', json={'answer':'Нет'}, headers=auth()).status_code == 403
    assert client.get(f'{ROOT}/{hid}', headers={'X-Admin-Token':'local-admin-only'}).status_code == 401
    assert client.get(f'{ROOT}/{hid}', headers=auth()).json()['contact'] == 'person@example.invalid'


def test_no_reply_to_returned_ticket_or_webim(case):
    hid, _ = case('website', 'Первое обращение', 'КП-1')
    store.return_handoff(hid)
    newer = store.add_handoff('website', 'Новое без сообщения', '', '')
    assert client.get(f'{ROOT}/{newer}', headers=auth()).json()['request'] == ''
    wid, _ = case('external', 'Уже передано в Webim', 'КП-2')
    store.update_session('external', client_id='webim-fake')
    for n in (hid, wid):
        assert not client.get(f'{ROOT}/{n}', headers=auth()).json()['can_answer']
        assert client.post(f'{ROOT}/{n}/answer', headers=auth(), json={'answer':'Поздно'}).status_code == 409
