"""Server-only, single-submit client for the shop's hosted conversational agent."""
import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from app import config
from app.agent import compact_order, prices

URL = 'https://emagi.ai/sitechat/chat/konsultant-magazina-po'
LABEL = 'Внешний консультант EmAGI'
PROVIDER = config.env('SHOP_CHAT_PROVIDER', 'local')
TOKEN_FILE = config.env('SITECHAT_TOKEN_FILE', '')
if PROVIDER not in ('local', 'sitechat'):
    raise ValueError('SHOP_CHAT_PROVIDER must be local or sitechat')
MAX_POLLS, POLL_INTERVAL, MAX_SECONDS = 12, 1.5, 55
MAX_BYTES = 64 * 1024


class SitechatError(RuntimeError):
    """Only fixed diagnostics: failed_turn persists the exception text."""


def _token():
    try:
        with Path(TOKEN_FILE).open('rb') as f:
            data = f.read(4097)
        if len(data) <= 4096 and re.fullmatch(rb'[\x21-\x7e]+', data.strip()):
            return data.strip().decode('ascii')
    except (OSError, ValueError):
        pass
    raise SitechatError('credential unavailable')


def _client():
    return httpx.Client(trust_env=False, follow_redirects=False)


def _remaining(until):
    left = until - time.monotonic()
    if left <= 0:
        raise SitechatError('deadline')
    return left


def _post(client, headers, payload, until):
    with client.stream('POST', URL, headers=headers, json=payload, timeout=min(20, _remaining(until))) as r:
        if r.status_code != 200:
            raise SitechatError('upstream HTTP error')
        data = bytearray()
        for chunk in r.iter_bytes():
            _remaining(until)
            if len(data) + len(chunk) > MAX_BYTES:
                raise SitechatError('invalid response')
            data.extend(chunk)
    result = json.loads(data.decode('utf-8'))
    if not isinstance(result, dict):
        raise SitechatError('invalid response')
    return result


def answer(sid, message, until):
    """An ambiguous submission is never retried. Only a pending receipt authorizes a poll."""
    until = min(until or float('inf'), time.monotonic() + MAX_SECONDS)
    context = {'session_id': hashlib.sha256(('shop-sitechat:' + sid).encode()).hexdigest()}
    headers = {'Authorization': 'Bearer ' + _token()}
    payload = {'channel_context': context, 'messages': [{'role': 'user', 'content': message}]}
    try:
        with _client() as client:
            for attempt in range(MAX_POLLS + 1):
                result = _post(client, headers, payload, until)
                status, reply = result.get('status'), result.get('reply')
                if status == 'ok' and isinstance(reply, str) and 0 < len(reply.strip()) <= 8000:
                    return reply.strip()
                if status == 'not_delivered':
                    raise SitechatError('not_delivered')
                if status != 'pending':
                    raise SitechatError('invalid response')
                if attempt == MAX_POLLS or _remaining(until) <= POLL_INTERVAL:
                    break
                time.sleep(POLL_INTERVAL)
                payload = {'channel_context': context, 'poll': True}
    except (httpx.HTTPError, ValueError, RecursionError):
        raise SitechatError('upstream unavailable or invalid response') from None
    raise SitechatError('pending: no final reply')


# A conversational API is not a channel for pasted credentials, URLs or diagnostic dumps.
_PRIVATE = re.compile(
    r'https?://|www\.|authorization|bearer\b|api[_ -]?key|password|парол[ьия]|токен|token|secret|cookie|'
    r'private key|traceback|(?:^|\n)\s*(?:DEBUG|ERROR|WARN(?:ING)?|INFO)\b|\b[\w/-]{32,}\b', re.I)
_SEARCH = re.compile(r'^\s*(?:покажите(?: варианты)?|найдите|ищу|артикул)\s+', re.I)
_UNVERIFIED = re.compile(
    r'[₽€$%]|\bруб(?:\.|л\w*\b)|\[\[|\b(?:в наличии|оплатил|оплачен|выставил|выставлен|оформил|'
    r'оформлен|создан|создал|передал|передан|отправил|отправлен|заброниров|гарантиру|скидк|'
    r'кабинет\w*\s+EmAGI|General\b|Qwen\b|Виктор\b)\w*', re.I)


def shop_reply(hosted, sid, message, tool, items, render_catalog):
    """After deterministic shop handlers. Only the sanitized message crosses the boundary."""
    if not message or _PRIVATE.search(message):
        return 'Текст не отправлен внешнему консультанту. Задайте короткий вопрос без ключей, ссылок и журналов.'
    query = _SEARCH.sub('', message).strip(' ?!.')
    if query and len(query) <= 160:
        out = tool('search_products', {'query': query})
        if (out.get('match') == 'all' and out.get('products')) or _SEARCH.match(message):
            exact = (out.get('first_product') or {}) if out.get('match') == 'all' and out.get('total') == 1 else {}
            compact_order.bind_search(items, exact.get('offers', []), exact.get('product_id'))
            items['compact_products'] = [p['title'] for p in out.get('products', [])[:4]] if out.get('match') == 'all' else []
            return render_catalog(out)
    reply = hosted(sid, message)
    # Never parse hosted text as shop tools, documents, links, events or verified terms.
    if _PRIVATE.search(reply) or _UNVERIFIED.search(reply) or prices.unknown(reply, set()):
        return 'Ответ внешнего консультанта не подтверждён магазином. Для проверки укажите название продукта или позовите менеджера в этом чате.'
    return f'{LABEL}: {reply}'
