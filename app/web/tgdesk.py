"""A single manager ticket. Telegram credentials never authorize any /admin route."""
from __future__ import annotations

import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Callable, Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app import config, store
from app.quote import invoice

SHELL_CSP = ("default-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
             "frame-ancestors https://web.telegram.org")
MAX_AGE = 300
ID = Annotated[int, PathParam(ge=1, le=2**63 - 1)]
PRIVATE = {'Cache-Control': 'private, no-store'}


def _signed_user(raw: str, token: str, now: int) -> int:
    if not token or not raw or len(raw) > 8192:
        raise ValueError
    pairs = parse_qsl(raw, strict_parsing=True, keep_blank_values=True, errors='strict', max_num_fields=64)
    fields = dict(pairs)
    if len(fields) != len(pairs) or any(not re.fullmatch(r'[a-zA-Z0-9_]+', k) or '\n' in v or '\r' in v
                                       for k, v in pairs):
        raise ValueError
    tag = fields.pop('hash')
    check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields))
    secret = hmac.digest(b'WebAppData', token.encode(), 'sha256')
    if not hmac.compare_digest(hmac.digest(secret, check.encode(), 'sha256').hex(), tag):
        raise ValueError
    date = fields['auth_date']
    if not re.fullmatch(r'[0-9]{1,12}', date) or not 0 <= now - int(date) <= MAX_AGE:
        raise ValueError
    uid = json.loads(fields['user'])['id']
    if type(uid) is not int or uid <= 0:
        raise ValueError
    return uid


def manager(request: Request) -> None:
    scheme, _, raw = request.headers.get('Authorization', '').partition(' ')
    try:
        uid = _signed_user(raw if scheme.lower() == 'tma' else '', config.TG_TOKEN, int(time.time()))
    except (ValueError, TypeError, KeyError, RecursionError):
        raise HTTPException(401, 'Откройте карточку заново из Telegram.') from None
    ids = [s.strip() for s in os.getenv('TG_MANAGER_IDS', '').split(',')]
    if not all(re.fullmatch(r'[1-9][0-9]{0,18}', s) for s in ids) or str(uid) not in ids:
        raise HTTPException(403, 'Этому аккаунту доступ не предоставлен.')


def _ticket(hid: int):
    row = store.db().execute('SELECT h.*, s.client_id FROM handoffs h JOIN sessions s ON h.session_id=s.id '
                             f'WHERE h.id=? AND NOT ({store.RUN_CLIENTS})', (hid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Обращение не найдено.')
    return row


def _request_at_handoff(h) -> str:
    # Messages have no handoff_id. Anchor the initiating utterance between this handoff and its predecessor.
    row = store.db().execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND ts<=? "
                             'AND ts>COALESCE((SELECT max(ts) FROM handoffs WHERE session_id=? AND id<?),0) '
                             'ORDER BY ts DESC,id DESC LIMIT 1', (h['session_id'], h['ts'], h['session_id'], h['id'])).fetchone()
    return row['content'] if row else ''


def _documents(sid: str):
    return [dict(row) for row in store.db().execute(
        "SELECT 'quote' AS kind,id,number,ts FROM quotes WHERE session_id=? UNION ALL "
        "SELECT 'invoice',id,number,ts FROM invoices WHERE session_id=? AND status=? ORDER BY ts DESC,id DESC",
        (sid, sid, invoice.ISSUED))]


def _can_answer(h) -> bool:
    # Webim handoffs are transferred to its own operators; manager_answer delivers to the website inbox.
    return h['status'] in ('new', 'taken') and not h['client_id'].lower().startswith('webim-')


class Reply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answer: str = Field(min_length=1, max_length=4000, pattern=r'\S')


def build_router(answer: Callable, session_lock: Callable) -> APIRouter:
    router = APIRouter()
    api = APIRouter(prefix='/api/tgdesk/handoffs', dependencies=[Depends(manager)])

    @router.get('/tgdesk', include_in_schema=False)
    def shell():
        return FileResponse(Path(__file__).with_name('static') / 'tgdesk.html')

    @api.get('/{handoff_id}')
    def card(handoff_id: ID):
        h = _ticket(handoff_id)
        messages = [{'role': 'manager' if m['role'] == 'assistant' and m['meta'].get('from') == 'manager' else m['role'],
                     'text': m['content'], 'ts': m['ts']} for m in store.messages(h['session_id'])
                    if m['role'] in ('user', 'assistant')]
        return {**{k: h[k] for k in ('id', 'status', 'reason', 'summary', 'contact', 'ts')},
                'request': _request_at_handoff(h), 'can_answer': _can_answer(h), 'messages': messages,
                'documents': _documents(h['session_id'])}

    @api.get('/{handoff_id}/documents/{kind}/{doc_id}')
    def document(handoff_id: ID, kind: Literal['quote', 'invoice'], doc_id: ID):
        sid = _ticket(handoff_id)['session_id']
        doc = next((d for d in _documents(sid) if d['kind'] == kind and d['id'] == doc_id), None)
        if doc and re.fullmatch(r'[\w-]{1,120}', doc['number']):
            path = config.QUOTES_DIR / (doc['number'] + '.pdf')
            if not path.is_symlink() and path.is_file():
                return FileResponse(path, media_type='application/pdf', filename=path.name, headers=PRIVATE)
        raise HTTPException(404, 'Документ не найден.')

    @api.post('/{handoff_id}/answer')
    def reply(handoff_id: ID, body: Reply):
        with session_lock(_ticket(handoff_id)['session_id']):
            if not _can_answer(_ticket(handoff_id)):
                raise HTTPException(409, 'Ответ из этой карточки недоступен.')
            if answer(handoff_id, body.answer.strip()) is None:
                raise HTTPException(404, 'Обращение не найдено.')
        return {'ok': True}

    router.include_router(api)
    return router
