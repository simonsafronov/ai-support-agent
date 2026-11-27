"""Compact buyer help: private teaching may select a public source, never supply copy."""
import re
import sqlite3

from app.kb import answers, search
from app.agent.buyer_workflow import buyer_procedure, choosing_software, procedure, support_followup


def without_manager_answers(out: dict) -> dict:
    """Apply before storing or returning any compact tool trace (including catalog hits)."""
    return {k: v for k, v in out.items() if k != 'manager_answers'}


# Positive evidence must be a plain, self-contained buyer instruction. Do not strip
# qualifications off a mixed answer or snippet; unknown syntax is a miss, not copy.
_PROSE = re.compile(r'[а-яёa-z\s«»“”"(),:—–-]+[.!]?', re.I)
_WORD = re.compile(r'[а-яёa-z]+', re.I)
_IMPERATIVE = re.compile(r'[а-яё]+(?:йте|ите|ьте)')
_NOT_GENERAL = re.compile(
    r'\b(?:не|нет|нельзя|без|но|если|лишь|только|кроме|вместо|никогда|после согласования|'
    r'продав\w*|продавц\w*|поставщик\w*|вендор\w*|партнер\w*|партнёр\w*|'
    r'api|seller\w*|vendor\w*|скид\w*|отсроч\w*|стоимост\w*|цен[аыуе]|рубл\w*|'
    r'процент\w*|индивидуаль\w*|гарантир\w*|парол\w*|реквизит\w*|инн|кпп|ооо)\b', re.I)
_DOC_URL = re.compile(r'https://docs\.softline\.com/ru-ru/(?:lk|id)(?:/[a-zA-Z0-9_-]+)*/?')
_STORE_URL = re.compile(r'https://store\.softline\.ru/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]*/?')
_GENERIC = {s for s, _ in search.query_terms(
    'найти посмотреть получить скачать открыть перейти сделать доступен раздел кабинет кнопка страница '
    'инструкция меню личный профиль вкладка интерфейс настройка')}


# Words about asking/choosing do not identify *what* the buyer is asking about.
_CONVERSATION = {s for s, _ in search.query_terms(
    'знать знаю понимаю разобраться решить определиться выбор выбрать выбирать выберите подобрать подбор '
    'посоветуйте порекомендуйте предложите помочь помогите помощь нужен нужна лучше лучший всего '
    'подойдет подходящий подходят предпочесть взять посоветуете отличаются отличие разница сравнить '
    'программа продукт софт вариант этих них какой-нибудь что-нибудь затрудняюсь сомневаюсь вопрос ищу')}
_HELP_SCAFFOLD = _CONVERSATION | _GENERIC | {s for s, _ in search.query_terms(
    'настроить включить отключить изменить сменить восстановить лицензию нажать использовать')}


_UI_CONTEXT = {s for s, _ in search.query_terms('личный кабинет страница раздел меню интерфейс')}


def buyer_terms(text):
    return {s for s, _ in search.query_terms(text, fallback=False)} - _CONVERSATION


# Match the subject in heading + body, not every word of the buyer's situation.
# Broad commerce words alone cannot establish which procedure is wanted.
_CONTEXT = _HELP_SCAFFOLD | _UI_CONTEXT | {s for s, _ in search.query_terms(
    'заказ покупка магазин клиент покупатель лицензия учебный помощь вопрос способ оплата программа '
    'получение письмо работа проблема шаг ошибка моего моей моим нашему')}
# The noun in a page title and the action in a question need not have the same Snowball stem.
_ACTIONS = (('включ', 'подключ'), ('отключ', 'выключ'), ('восстанов',), ('актив', 'активац'),
            ('возврат', 'возвращ', 'вернут'), ('продл',), ('удал',), ('установ',), ('настро', 'настрой'))


def _actions(text):
    words = re.findall(r'[а-я]+', search.fold(text))
    return {roots[0] for roots in _ACTIONS if any(w.startswith(roots) for w in words)}


# Reject the whole excerpt; removing a sentence can drop a condition that changes the answer.
_SECRET_REQUEST = re.compile(
    r'\b(?:пришлите|отправьте|сообщите|предоставьте|назовите)\b[^.!?\n]{0,55}'
    r'\b(?:пароль|cvv|cvc|ключ|код (?:из|подтверждения)|номер карты)\b', re.I)
_PERSONAL_CLAIM = re.compile(
    r'\b(?:ваш(?:а|и)? (?:заказ|оплата|деньги) (?:оплачен|подтвержден|зачислен|возвращен|отгружен)'
    r'|гарантиру(?:ем|ется)|возврат одобрен)', re.I)


def _public_copy(item):
    # Metadata and body both reach the visitor, including the step panel on reload.
    from app.agent.loop import for_model
    public = ' '.join(item[k] for k in ('title', 'heading', 'snippet'))
    return (public == for_model(public) and not _SECRET_REQUEST.search(public)
            and not _PERSONAL_CLAIM.search(search.fold(public)))


def fits_buyer(item, original):
    if (not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('heading', 'title', 'snippet'))
            or not search.buyer_source(item.get('url'), item.get('source'), item['heading']) or not _public_copy(item)):
        return False
    if choosing_software(original):
        return False
    title = item['title'] + ' ' + item['heading']
    body = item['snippet']
    asked = buyer_terms(original)
    subject = {t for t in asked - _CONTEXT if not t.isdigit()}
    heading, excerpt = buyer_terms(title), buyer_terms(body)
    anchor = subject & heading
    actions = _actions(original)
    if not body.strip() or actions - _actions(title + ' ' + body):
        return False
    # An unknown named technology must not borrow another product's instructions.
    identifiers = {t for t in subject if re.search('[a-z]', t)}
    if identifiers - heading - excerpt:
        return False
    workflow = procedure(original)
    if workflow:
        # A general procedure may answer about an unknown product, but licensing terms
        # on a named product page are not transferable to a different subscription.
        if workflow.applies('продление'):
            names = re.findall(r'[«"]([^«»"\n]{2,80})[»"]', original)
            if names and not all(buyer_terms(name) <= heading | excerpt for name in names):
                return False
        return workflow.supports(body)
    if not subject:
        return False
    if not anchor and len(subject & excerpt) >= 2 and subject <= excerpt and actions:
        return True
    if anchor and subject <= excerpt:
        return True
    # Independent evidence: focused heading plus a matching step/detail in the excerpt.
    return bool(anchor and subject & excerpt and (len(subject & (heading | excerpt)) >= 2
                or (subject <= heading and subject <= excerpt)))


def _terms(text: str) -> set[str]:
    return {s for s, _ in search.query_terms(text)} - _GENERIC


def _official(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    pattern = {'docs': _DOC_URL, 'store': _STORE_URL}.get(item.get('source'))
    return (pattern is not None and isinstance(item.get('url'), str) and bool(pattern.fullmatch(item['url']))
            and all(isinstance(item.get(k), str) for k in ('title', 'heading', 'snippet'))
            and not any(_NOT_GENERAL.search(item[k]) for k in ('url', 'title', 'heading', 'snippet')))


def _words(text: str) -> tuple[str, ...]:
    # No private input to the search stemmer (it has a process-wide word cache).
    return tuple(_WORD.findall(search.fold(text)))


def _instruction(text, limit: int) -> tuple[str, ...]:
    if not isinstance(text, str) or len(text) >= limit or not _PROSE.fullmatch(text.strip()) or _NOT_GENERAL.search(text):
        return ()
    words = _words(text)
    return words if len(words) >= 3 else ()


def _hints(query: str) -> list[tuple[tuple[str, ...], str]]:
    # Only teach=True questions enter answers.find's FTS. Never put these rows in
    # `out`, a tool argument, a search result, a model prompt, an exception or a log.
    return [(hint, search.fold(row['answer'].strip())) for row in answers.find(query)
            if (hint := _instruction(row.get('answer'), 1200))]  # find truncates at 1200


def _relevant(item, terms: set[str]) -> bool:
    # FTS uses OR and may rank an incidental hit in a title first. Require all
    # original subject anchors in the body itself, not scattered among documents.
    return _official(item) and terms <= _terms(item['snippet'])


def _corroborated(query: str, terms: set[str], hints) -> str:
    # Only the BUYER's question goes to the local read-only KB. A manager may
    # break a ranking tie, not widen the audience (seller/API) or change topic.
    found = search.kb_search(query, limit=24, buyer_help=True)
    if found.get('note') or found.get('error'):
        return ''
    for item in found['results']:
        if not _relevant(item, terms):
            continue
        snippet = item['snippet']
        step = _instruction(snippet, search.SNIPPET_MAX)
        # Match the complete hint in order, including verbs/prepositions. Bag-of-
        # words similarity can turn a removal into an addition or reverse a transfer.
        # Require additional public context; never replay a manager's whole reply.
        for hint, literal in hints:
            if len(step) < len(hint) or step[:len(hint)] != hint:
                continue
            # Teaching must contribute a distinguishing public detail, not just
            # repeat the question or a generic instruction to open the account.
            if _terms(' '.join(step[1:len(hint)])) - terms:
                return f"{snippet}\nИсточник: {item['url']}"
    return ''


def _taught_reply(query: str, out: dict) -> str:
    """Original sanitized request + buyer_help output -> corroborated KB excerpt/URL, or ''.

    Keep a relevant initial buyer result. An empty *or weak* result may be rescued
    only by official buyer help independently indexed for the original request.
    """
    if (not isinstance(out, dict) or not isinstance(out.get('results'), list) or 'error' in out
            or 'manager_answers' in out or not isinstance(query, str) or len(query) > 800
            or not _PROSE.fullmatch(query.strip().removesuffix('?')) or _NOT_GENERAL.search(query)):
        return ''
    terms = _terms(query)
    if len(terms) < 2 or (not out.get('note') and out['results'] and _relevant(out['results'][0], terms)):
        return ''
    try:
        hints = _hints(query)
        return _corroborated(query, terms, hints) if hints else ''
    except (FileNotFoundError, sqlite3.Error):
        return ''  # No verified local evidence; keep the caller's ordinary fallback.


# These are limitations of this application, not assertions about an actual transaction.
# No payment, fulfillment or authentication status is available in the catalog/KB.
_ORDER_STATE = re.compile(r'\b(?:статус|этап|состоян|обработк|отслед)\w*', re.I)
_ORDER = re.compile(r'\bзаказ\w*', re.I)
_PAYMENT_CHECK = re.compile(r'\b(?:оплатил[аи]?|заплатил[аи]?|списал\w*|списан\w*|резерв\w*|отклонил\w*)\b', re.I)


def _transaction_note(query):
    if _PAYMENT_CHECK.search(query) and (re.search(r'оплат|заплат|плат[её]ж|деньг|средств|сумм', query, re.I)):
        return ('Я не вижу банковскую операцию и не могу подтвердить оплату. Проверьте операцию в банке '
                'и статус заказа в магазине, прежде чем платить повторно. Данные карты присылать не нужно.')
    if _ORDER.search(query) and _ORDER_STATE.search(query):
        return ('Я не вижу ваш заказ и не могу проверить его текущий статус. Сверьте номер заказа с '
                'подтверждением покупки. Если статус неясен, по этому номеру можно запросить проверку у менеджера.')
    return ''


def learned_reply(query: str, out: dict) -> str:
    """Compact's optional reply before the ordinary public-excerpt renderer.

    Don't convert a general help article into evidence about this customer's order.
    Teaching has its own stricter rules and cannot supply private text to this path.
    """
    if (not isinstance(query, str) or not isinstance(out, dict) or not isinstance(out.get('results'), list)
            or 'manager_answers' in out or 'error' in out):
        return ''
    if choosing_software(query):
        return 'Что нужно сделать с помощью программы? Расскажите, какой результат вы хотите получить.'
    if not out.get('query'):
        return _taught_reply(query, out)  # standalone teaching contract
    note = _transaction_note(query)
    workflow = procedure(query) if not note else None
    items = [item for item in out['results'] if not out.get('note') and fits_buyer(item, query)]
    if not items:
        # A router rewrite is a retrieval hint, not the buyer's intent. Retry using the buyer's
        # own sanitized words, never a private answer or a generated product name.
        taught = _taught_reply(query, out)
        if taught:
            return taught
        if workflow and out.get('query') != query:
            try:
                direct = search.kb_search(query, limit=12, buyer_help=True)
            except (FileNotFoundError, sqlite3.Error):
                direct = {}
            items = [r for r in direct.get('results', []) if not direct.get('note') and fits_buyer(r, query)]
    if items:
        item = items[0]
        if not workflow and not note and item in out['results'] and _relevant(item, _terms(query)):
            return ''  # The ordinary renderer already has this verified public result.
        caption = 'Учебный пример, не подтверждение условий вашего заказа.' if item.get('example') else ''
        # Preserve the complete paragraph including exceptions and negatives.
        return '\n'.join(x for x in (caption, note, item['snippet'], workflow.next_step if workflow else '',
                                      'Источник: ' + item['url']) if x)
    return note or _after_purchase_miss(query) or (workflow.next_step if workflow else '')


def _after_purchase_miss(query):
    # Diagnostic next steps, not a guessed shop policy, account change or refund decision.
    q = search.fold(query)
    if re.search(r'активац|активиров', q):
        return ('Не нашёл подтверждённой инструкции для этой ошибки. Сверьте название и редакцию в заказе '
                'с установленной программой. Если они совпадают, напишите текст ошибки без ключа лицензии.')
    if re.search(r'\bключ\w*', q) and re.search(r'письм|заказ|покупк|лиценз', q):
        return ('Я не вижу выдачу ключа по вашей покупке. Проверьте письмо с подтверждением заказа '
                'и папку «Спам». Если инструкции нет, для проверки нужен номер заказа, а не ключ лицензии.')
    if re.search(r'\bупд\b|закрывающ\w* документ|бухгалтер\w* документ', q):
        return ('Я не вижу документы вашей поставки. Для проверки у менеджера достаточно номера заказа '
                'и названия нужного документа. Реквизиты в чат отправлять не нужно.')
    if re.search(r'возврат|вернуть', q) and re.search(r'куп|заказ|лиценз|деньг|оплат', q):
        return ('Чтобы запросить проверку возврата, подготовьте номер заказа и укажите, какая позиция '
                'куплена по ошибке или не подошла. Я не могу подтвердить право на возврат или его срок.')
    if re.search(r'вход|войти|доступ', q) and re.search(r'фактор|2fa|аутентификац', q):
        return ('Не нашёл подтверждённой инструкции для этого случая. Используйте восстановление доступа '
                'через официальный экран входа или поддержку магазина. Пароль и коды подтверждения не присылайте.')
    return ''
