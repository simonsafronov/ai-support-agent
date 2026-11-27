"""Выдача учебного PDF в приватный диалог. Это не заказ и не подтверждение поставки."""
import hashlib
import json
import re
import time

from app import store
from app.quote import invoice


def state(sid):
    session = store.find_session(sid)
    client = ((session or {}).get('client_id') or '').lower()
    if not client or client == 'demo' or client.startswith(('eval-', 'webim-')):
        return None
    quote = store.last_quote(sid)
    if not quote:
        return None
    buyer = [session['facts'].get(k) for k in ('компания', 'ИНН', 'КПП')]
    revision = hashlib.sha256(json.dumps([sid, quote, buyer], sort_keys=True).encode()).hexdigest()[:24]
    sent = store.db().execute("SELECT 1 FROM messages WHERE session_id=? AND "
                              "json_extract(meta_json,'$.invoice_revision')=? LIMIT 1", (sid, revision)).fetchone()
    return {'revision': revision, 'quote_number': quote['number'], 'sent': bool(sent)}


_SAMPLE = re.compile(r'\b(?:демо\w*|учебн\w*|образ[её]ц|пример|тестов\w*)\b', re.I)
_BILL = re.compile(r'\b(?:демо[- ]?)?сч[её]т(?:а|ом)?\b', re.I)
_IN_CHAT = re.compile(r"(?:в (?:этот |этот же )?чат|сюда)", re.I)
_INFO = re.compile(r"(?:^|[.!?]\s*)\s*(?:как|зачем|где|почему|чем|что такое)\b|(?:для оплаты|на оплату|настоящий|плат[её]жный)\b", re.I)
_REQUEST = re.compile(r'выстав|пришл|пришли|отправ|покаж|дайте|выда|сформир|сдела|подготов|оформ|хочу|нужен|нужно|прошу|можно (?:ли )?(?:получить|посмотреть)', re.I)
_NO = re.compile(r'\b(?:без (?:демо|учебн)|не надо|не нужно|не нужен|не хочу|не просил|не прошу|не (?:присыл|отправ|показыв|дела|формир|оформ)|отмен\w*)', re.I)


def requested(sid):
    """Принимаем просьбу в текущем сообщении либо продолжение сбора реквизитов для неё."""
    history = store.messages(sid)
    users = [m for m in history if m['role'] == 'user']
    if not users:
        return False
    last = users[-1]['content']
    def explicit(text):
        if not _BILL.search(text) or _NO.search(text) or _INFO.search(re.sub(r"не для оплаты", "", text, flags=re.I)):
            return False
        if not (_SAMPLE.search(text) or _IN_CHAT.search(text)):
            return False
        return bool(_REQUEST.search(text) or re.fullmatch(r'(?:демо[- ]?|учебный |демонстрационный |образец )сч[её]та?[.! ]*', text.strip(), re.I))
    if explicit(last):
        return True
    if _NO.search(last) or not re.search(r'\b(?:ИНН|КПП|ООО|АО|ИП|\d{9,12})\b', last, re.I):
        return False
    for step in reversed(store.steps(sid)):
        if step['name'] == 'make_invoice' and step['kind'] == 'tool':
            turn = step['turn']
            return bool(step['output'].get('customer_missing') and turn == len(users) - 1
                        and any(explicit(m['content']) for m in users[:turn]))
    return False


def publish(sid, who, *, revision=None):
    """Вызывается под замком хода в web; повтор выдачи не добавляет второе вложение."""
    current = state(sid)
    if not current or (revision is not None and revision != current['revision']):
        return {'ok': False, 'detail': 'КП изменилось или недоступно в этом чате.'}
    if current['sent']:
        return {'ok': False, 'already_sent': True, 'detail': 'Учебный счёт уже в этом чате.'}
    draft = invoice.prepare(sid)
    if not draft['ok']:
        return {'ok': False, 'detail': draft['error'], **({'customer_missing': draft['customer_missing']}
                if draft.get('customer_missing') else {})}
    result = invoice.issue(draft['id'], who)
    if not result or result['status'] != invoice.ISSUED:
        return {'ok': False, 'detail': 'Учебный счёт не выдан.'}
    iid = draft['id']
    event = {'type': 'demo_invoice', 'url': f'/api/session/{sid}/invoices/{iid}.pdf'}
    meta = {'from': 'agent' if revision is None else 'manager', 'invoice_id': iid,
            'invoice_revision': current['revision'], 'events': [event]}
    text = 'Учебный счёт готов. Не для оплаты. Заказ не создан.'
    if any(item.get('available') is False for item in draft['items']):
        text += ' По фиду есть позиции не в наличии. Фактическую поставку подтверждает менеджер; срок не указан.'
    with store.db() as con:
        con.execute("INSERT INTO messages(session_id,ts,role,content,meta_json) "
                    "SELECT ?,?,'assistant',?,? WHERE NOT EXISTS (SELECT 1 FROM messages WHERE session_id=? "
                    "AND json_extract(meta_json,'$.invoice_id')=?)", (sid, time.time(), text,
                    json.dumps(meta, ensure_ascii=False), sid, iid))
    return {'ok': True, 'detail': 'Учебный счёт доступен в чате клиента.'}
