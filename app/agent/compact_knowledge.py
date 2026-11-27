"""Compact buyer help: private teaching may select a public source, never supply copy."""
import re
import sqlite3

from app.kb import answers, search


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


def fits_buyer(item, original):
    # bm25 ranks OR hits *within* one corpus; it isn't an absolute confidence.
    # Require a specific subject AND coverage of the request (including its action).
    needed = buyer_terms(original) - _UI_CONTEXT
    subject = {s for s in needed - _HELP_SCAFFOLD if not s.isdigit()}
    topic = buyer_terms(item.get('title', '') + ' ' + item.get('heading', ''))
    return (bool(subject & topic) or len(subject) >= 2) and needed <= buyer_terms(item.get('snippet', ''))


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


def learned_reply(query: str, out: dict) -> str:
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
