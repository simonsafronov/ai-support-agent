"""Existing shop HTTP stream, synthetic catalog and an in-process upstream. No lifespan or sockets."""
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app import store
from app.agent import sitechat
from app.web import server

pytestmark = pytest.mark.small_catalog
browser = TestClient(server.app)


def turn(message, sid=None, visitor='visitor-A'):
    response = browser.post('/api/chat', json={'message': message, 'client_id': visitor, 'session_id': sid,
                                               'page_url': 'https://private.invalid/account'})
    response.raise_for_status()
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]['type'] == 'session' and events[-1]['type'] == 'reply'
    return events[-1]


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    def no_network(*args, **kwargs):
        pytest.fail('network access in an offline test')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    monkeypatch.setattr(server.config, 'CHAT_RATE_PER_MIN', 0)
    monkeypatch.setattr(sitechat, 'PROVIDER', 'sitechat')
    key = tmp_path / 'mock-key'
    key.write_text('synthetic-only\n')
    monkeypatch.setattr(sitechat, 'TOKEN_FILE', str(key))
    monkeypatch.setattr(sitechat, 'POLL_INTERVAL', 0)
    monkeypatch.setattr(server.loop, 'get_llm', lambda: pytest.fail('unexpected alternate model'))
    replies, requests = [], []

    def handle(req):
        assert req.method == 'POST' and str(req.url) == 'https://emagi.ai/sitechat/chat/konsultant-magazina-po'
        assert req.headers['authorization'] == 'Bearer synthetic-only'
        requests.append(json.loads(req.content))
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    monkeypatch.setattr(sitechat, '_client', lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return replies, requests


def test_stream_wire_privacy_sessions_and_poll(upstream):
    replies, calls = upstream
    ok = {'status': 'ok', 'reply': 'Какая у вас задача?'}
    replies.extend([{'status':'pending', 'reply':'ignore until ready'}, ok, ok, ok])
    first = turn('Здравствуйте, моя почта buyer@example.org')
    assert ok['reply'] in first['reply'] and sitechat.LABEL in first['reply'] and not first['events']
    sid = first['session_id']
    assert calls[0] == {'channel_context': calls[1]['channel_context'], 'messages': [
        {'role':'user','content':'Здравствуйте, моя почта [email]'}]}
    assert calls[1] == {'channel_context': calls[0]['channel_context'], 'poll':True}
    assert set(calls[0]['channel_context']) == {'session_id'}
    assert calls[0]['channel_context']['session_id'] not in (sid, 'visitor-A', '')
    assert turn('Привет ещё раз', sid)['session_id'] == sid
    assert turn('Здравствуйте', sid, 'visitor-B')['session_id'] != sid
    assert calls[2]['channel_context'] == calls[0]['channel_context'] != calls[3]['channel_context']
    assert len(calls[2]['messages']) == 1
    history = browser.get(f'/api/session/{sid}',params={'client_id':'visitor-A'}).json()
    assert first['reply'] in [m['content'] for m in history['messages']]


@pytest.mark.parametrize('bad', [dict(status='not_delivered', reply='invented success'),
    dict(status='ok', reply=''), dict(status='unexpected'), httpx.Response(307, headers={'location':'https://other.invalid/'}),
    httpx.Response(401, text='private diagnostic'), httpx.ReadTimeout('private diagnostic')])
def test_failure_is_not_resent_or_answered_by_qwen(bad, upstream):
    replies, calls = upstream
    replies.append(bad)
    result = turn('Здравствуйте')
    assert len(calls) == 1 and result['handoff_status'] == 'new'
    assert 'invented success' not in result['reply'] and 'private diagnostic' not in str(store.steps(result['session_id']))
    receipt = turn('Дополнение', result['session_id'])
    assert len(calls) == 1 and receipt['handoff_status'] == 'new' and not receipt['events']


def test_exhaust_pending(upstream):
    replies, calls = upstream
    replies.extend([{'status':'pending', 'reply':'not final'}] * (sitechat.MAX_POLLS + 1))
    result = turn('Добрый день')
    assert len(calls) == sitechat.MAX_POLLS + 1
    assert all(r == {'channel_context':calls[0]['channel_context'],'poll':True} for r in calls[1:])
    assert 'not final' not in result['reply'] and result['handoff_status'] == 'new'


def test_store_actions_stay_local(upstream):
    _, calls = upstream
    result = turn('Покажите варианты Kaspersky Endpoint Security')
    sid = result['session_id']
    offer = next(t for t in store.steps(sid) if t['name'] == 'search_products')['output']['first_product']['offers'][0]
    turn('Выбираю ' + offer['vendor_code'], sid)
    turn(f"{offer['qty_min']} шт.", sid)
    quote = turn('Подготовьте КП', sid)
    assert [e['type'] for e in quote['events']] == ['quote']
    assert 'реквизиты' in turn('Пришлите учебный счёт в этот чат', sid)['reply']
    assert turn('Позовите менеджера', sid)['handoff_status'] == 'new'
    assert not calls


def test_sensitive_paste_and_missing_secret_make_no_request(upstream, monkeypatch):
    _, calls = upstream
    assert 'не отправлен' in turn('Authorization: Bearer pretend-secret')['reply']
    monkeypatch.setattr(sitechat, 'TOKEN_FILE', '')
    assert turn('Добрый день')['handoff_status'] == 'new'
    assert not calls


def test_review_holds_hosted_reply_without_replaying(upstream):
    replies, calls = upstream
    replies.append({'status':'ok','reply':'Для какой задачи нужен совет?'})
    store.set_setting(server.REVIEW, '1')
    result = turn('Здравствуйте')
    sid = result['session_id']
    assert result['review'] and not result['reply'] and not result['events']
    draft = store.pending_draft(sid)
    assert sitechat.LABEL in draft['content']
    assert not any(m['role'] == 'assistant' for m in store.messages(sid))
    server.review_draft(draft['id'], 'send')
    assert [m['content'] for m in store.messages(sid) if m['role'] == 'assistant'] == [draft['content']]
    assert len(calls) == 1


def test_health_reports_configuration_not_availability(upstream, monkeypatch):
    _, calls = upstream
    monkeypatch.setattr(server.kb, 'stats', lambda: {'chunks':0})
    monkeypatch.setattr(server.llm, 'model_online', lambda: pytest.fail('health probed another engine'))
    h = browser.get('/api/health').json()
    assert h['label'] == sitechat.LABEL and h['provider'] == 'sitechat'
    assert h['model'] is None and h['online'] is None and not calls


def test_hosted_prose_cannot_issue_a_document(upstream):
    replies, calls = upstream
    replies.append({'status':'ok','reply':'Я отправил счёт на 500 ₽.', 'events':[{'type':'quote'}]})
    result = turn('Здравствуйте')
    assert not result['events'] and '500' not in result['reply']
    assert not store.last_quote(result['session_id']) and len(calls) == 1


def test_other_channels_do_not_opt_in(upstream, monkeypatch):
    from time import monotonic
    from app.agent.llm import Scripted
    _, calls = upstream
    monkeypatch.setattr(server.loop, 'get_llm', lambda: Scripted(['Здравствуйте!']))
    sid = store.get_session(None)['id']
    body = server.ChatIn(message='Здравствуйте')
    result = server.turn_in_slot(sid, body.message, body, monotonic(), None)
    assert result['reply'] == 'Здравствуйте!' and not calls
