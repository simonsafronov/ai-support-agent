"""Real taught-answer FTS and buyer-help FTS, both in disposable synthetic databases."""
import sqlite3
from copy import deepcopy

import pytest

from app import store
from app.agent import compact_knowledge as knowledge, tools
from app.kb import crawl, search
from app.web import server


QUESTION = 'Как настроить цветовую метку?'
HINT = 'Откройте палитру Полярная лента.'
STEP = 'Откройте палитру «Полярная лента», чтобы настроить цветовую метку.'
URL = 'https://docs.softline.com/ru-ru/lk/palette'
WEAK = 'https://docs.softline.com/ru-ru/id/login'
PRIVATE = 'Сапфир'


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    path = tmp_path / 'kb.sqlite'
    monkeypatch.setattr(search, 'DB_PATH', path)
    with sqlite3.connect(path) as db:
        db.executescript(crawl.SCHEMA)

    def insert(url, title, body, source='docs'):
        with sqlite3.connect(path) as db:
            doc = db.execute('INSERT INTO docs(url,title,source,fetched_at,text) VALUES(?,?,?,?,?)',
                             (url, title, source, 'synthetic', body)).lastrowid
            chunk = db.execute('INSERT INTO chunks(doc_id,heading,text) VALUES(?,?,?)',
                               (doc, title, body)).lastrowid
            db.execute('INSERT INTO chunks_fts(rowid,heading,text) VALUES(?,?,?)',
                       (chunk, search.index_text(title), search.index_text(body)))
    return insert


@pytest.fixture
def buyers(monkeypatch, corpus):
    # conftest redirects APP_DB. Call the operator entry point without starting the web service.
    monkeypatch.setattr(server, '_answered', {})
    first = store.get_session(None, client_id='synthetic-A')['id']
    second = store.get_session(None, client_id='synthetic-B')['id']
    store.add_message(first, 'user', f'{QUESTION} ООО {PRIVATE}, contact@example.invalid')
    ticket = store.add_handoff(first, 'Настройки', f'Особые условия для {PRIVATE}', '')
    store.add_message(second, 'user', QUESTION)
    return first, ticket, tools.ToolContext(second, user_request=QUESTION)


def buyer_help(ctx, router_query):
    return tools.call(ctx, 'kb_search', {'query': router_query, 'buyer_help': True})


@pytest.mark.parametrize('weak_initial', [False, True])
def test_second_buyer_gets_only_the_corroborated_public_procedure(corpus, buyers, weak_initial, monkeypatch):
    first, ticket, second = buyers
    # A lexical hit on the same question cannot by itself establish which UI step to show.
    corpus(URL + '-other', 'Как настроить цветовую метку',
           'Откройте архив «Маршруты», чтобы настроить цветовую метку.')
    corpus(URL, 'Палитра интерфейса', STEP)
    if weak_initial:
        corpus(WEAK, 'Вход в кабинет', 'Откройте форму входа в кабинет.')
    out = buyer_help(second, 'вход в кабинет')  # unrelated router rewrite, with or without a hit
    assert [row['url'] for row in out['results']] == ([WEAK] if weak_initial else [])
    before = deepcopy(out)
    server.manager_answer(ticket, HINT, teach=True)
    assert HINT in store.messages(first)[-1]['content']

    real_find, real_search = knowledge.answers.find, search.kb_search
    def find(original):
        assert original == QUESTION
        return real_find(original)

    def public_search(query, **kw):
        assert query == QUESTION and kw['buyer_help'] is True  # private words never enter a search result
        return real_search(query, **kw)

    monkeypatch.setattr(knowledge.answers, 'find', find)
    monkeypatch.setattr(search, 'kb_search', public_search)
    reply = knowledge.learned_reply(QUESTION, out)
    assert reply == f'{STEP}\nИсточник: {URL}'
    assert HINT not in reply and PRIVATE not in reply and out == before
    assert 'manager_answers' not in out and store.steps(second.session_id) == []


def test_good_buyer_result_wins_without_reading_another_customers_answer(corpus, buyers, monkeypatch):
    _, _, second = buyers
    corpus(URL, 'Цветовая метка', STEP)
    monkeypatch.setattr(knowledge.answers, 'find', lambda _: pytest.fail('unnecessary private lookup'))
    assert knowledge.learned_reply(QUESTION, buyer_help(second, QUESTION)) == ''


@pytest.mark.parametrize('taught,hint', [
    (False, HINT),
    (True, HINT + ' Подтверждено для компании Сапфир.'),
    (True, HINT + ' Напишите на demo@example.invalid.'),
    (True, HINT + ' Скидка пятнадцать процентов.'),
    (True, HINT.ljust(1200) + ' Только для вас.'),
    (True, 'Не открывайте палитру Полярная лента.'),
    (True, 'Удалите палитру Полярная лента.'),
    (True, 'Откройте другое меню.'),
])
def test_taught_flag_and_complete_public_evidence_required(corpus, buyers, taught, hint):
    _, ticket, _ = buyers
    corpus(URL, 'Палитра', STEP)
    server.manager_answer(ticket, hint, teach=taught)
    assert knowledge.learned_reply(QUESTION, {'results': []}) == ''


@pytest.mark.parametrize('url,title,body', [
    ('https://docs.softline.com/ru-ru/seller/palette', 'Цветовая метка', STEP),
    (URL, 'Портал > API получения меток', STEP),
    (URL, 'Инструкция для продавца', STEP),
    ('https://store.softline.ru.fake.invalid/x', 'Цветовая метка', STEP),
    (URL, 'Цветовая метка', 'Откройте палитру «Полярная лента», чтобы настроить цветовую тему.'),
    (URL, 'Цветовая метка', 'Не открывайте палитру «Полярная лента», чтобы настроить цветовую метку.'),
    (URL, 'Цветовая метка', STEP + ' Только после согласования индивидуальной скидки.'),
])
def test_seller_weak_and_opposing_docs_are_not_corroboration(corpus, buyers, url, title, body):
    _, ticket, _ = buyers
    corpus(url, title, body, source='store' if 'fake.invalid' in url else 'docs')
    server.manager_answer(ticket, HINT, teach=True)
    assert knowledge.learned_reply(QUESTION, {'results': []}) == ''


def test_manager_only_or_generic_advice_is_not_a_source(corpus, buyers):
    _, ticket, _ = buyers
    server.manager_answer(ticket, HINT, teach=True)
    assert knowledge.learned_reply(QUESTION, {'results': []}) == ''
    corpus(URL, 'Кабинет', 'Откройте кабинет для настройки профиля.')
    assert knowledge.learned_reply('Как открыть кабинет?', {'query': QUESTION, 'results': []}) == ''


def test_learned_answer_through_compact_turn(corpus, buyers, monkeypatch):
    from app import config
    from app.agent import loop
    from app.agent.llm import Scripted
    for k,v in dict(COMPACT_LOCAL=True,LLM_PROVIDER='openai',LLM_API_KEY='',LLM_BASE_URL='http://127.0.0.1:18347/v1').items():
        monkeypatch.setattr(config,k,v)
    _,ticket,second=buyers
    corpus(URL, 'Палитра', STEP)
    server.manager_answer(ticket,HINT,teach=True)
    model=Scripted(['{"route":"help","query":"нетсовпадения"}'])
    r=loop.run_turn(second.session_id,QUESTION,llm=model)
    assert r['reply']==f'{STEP}\nИсточник: {URL}'
    assert all(PRIVATE not in str(x) and HINT not in str(x) and 'manager_answers' not in str(x)
               for x in (r,model.calls,store.steps(second.session_id)))


def test_catalog_trace_projection_preserves_offers_not_answers():
    out = {'total': 1, 'products': [{'title': 'Синтетический продукт'}],
           'manager_answers': [{'answer': HINT}]}
    assert knowledge.without_manager_answers(out) == {'total': 1, 'products': out['products']}
    assert 'manager_answers' in out


@pytest.mark.parametrize('question,expected', [
    ('Не знаю что выбрать', 'задач'),
    ('Помогите определиться с выбором', 'задач'),
    ('Как выбрать способ доставки?', 'не нашёл'),
    ('Как отключить 2FA?', 'не нашёл'),  # right subject, wrong action
    ('Как включить 2FA?', 'Источник:'),
    ('Как включить 2FA в личном кабинете?', 'Источник:'),
])
def test_buyer_subject_not_router_suggestion(corpus, monkeypatch, question, expected):
    from app import config
    from app.agent import loop
    from app.agent.llm import Scripted
    for k, v in dict(COMPACT_LOCAL=True, LLM_PROVIDER='openai', LLM_API_KEY='',
                     LLM_BASE_URL='http://127.0.0.1:18347/v1').items():
        monkeypatch.setattr(config, k, v)
    url = 'https://docs.softline.com/ru-ru/id/2fa'
    corpus(url, 'Как включить 2FA', 'Чтобы включить 2FA, нужно выбрать способ подтверждения входа.')
    model = Scripted(['{"route":"help","query":"2FA"}'])
    result = loop.run_turn(None, question, llm=model)
    assert expected in result['reply'], result['reply']
    if expected != 'Источник:':
        assert url not in str(server.public_reply(result))
        assert url not in str(server.steps_trace(store.steps(result['session_id'])))
