"""Карточка как запасной источник: настоящий диспетчер инструментов, источники синтетические."""
import json
from pathlib import Path

import pytest

from app import config
from app.agent import tools
from app.catalog import cards


URL = 'https://store.softline.ru/demo/mail-pro/'
PAGE = '''<li data-tab="description"><article><p>Почтовый сервер.</p></article></li>
<li data-tab="licensing"><article><p>Только для Pro: на пользователя.</p></article></li>
<li data-tab="system-requirements"><article><p>Для Pro нужно 8 ГБ.</p></article></li>
<li data-tab="reviews"><article>Для Home нужно 2 ГБ.</article></li>'''


@pytest.fixture
def sources(monkeypatch):
    product = {'product_id': '12', 'title': 'Mail Pro', 'vendor': 'Demo', 'url': URL}
    result = {'match': 'all', 'total': 1, 'products': [product]}
    monkeypatch.setattr(config, 'PRODUCT_CARDS', True)
    monkeypatch.setattr(tools.catalog, 'search_products', lambda *a, **kw: result)
    monkeypatch.setattr(tools.catalog, 'get_product', lambda pid, **kw: {'product': dict(product), 'matching': 1})
    monkeypatch.setattr(tools.answers, 'find', lambda q: [])
    monkeypatch.setattr(tools.kb, 'kb_search', lambda *a, **kw: {'results': []})
    fetched = []
    monkeypatch.setattr(cards, '_fetch', lambda url: fetched.append(url) or PAGE)
    return tools.ToolContext('test', user_request='Какие требования у Mail Pro?'), result, fetched


def section(ctx, name='licensing', pid='12'):
    return tools.slim('get_product', tools.call(ctx, 'get_product', {'product_id': pid, 'card_section': name}))


def test_card_requires_both_lookups_and_returns_only_requested_section(sources):
    ctx, _, fetched = sources
    tools.call(ctx, 'search_products', {'query': 'Mail Pro'})
    assert 'error' in section(ctx) and not fetched
    tools.call(ctx, 'kb_search', {'query': 'Mail Pro лицензирование'})
    assert 'error' in section(ctx, pid='99') and not fetched
    assert section(ctx)['licensing'] == 'Только для Pro: на пользователя.'
    assert section(ctx, 'requirements')['requirements'] == 'Для Pro нужно 8 ГБ.'
    assert fetched == [URL]


@pytest.mark.parametrize('match,total,query', [('fuzzy', 1, 'Mail Pro'), ('all', 2, 'Mail Pro'), ('all', 1, 'Mail')])
def test_first_hit_is_not_identity(sources, match, total, query):
    ctx, result, fetched = sources
    result.update(match=match, total=total)
    tools.call(ctx, 'kb_search', {'query': 'лицензирование'})
    tools.call(ctx, 'search_products', {'query': query})
    assert 'error' in section(ctx) and not fetched


def test_model_cannot_confirm_its_own_guessed_title(sources):
    ctx, _, fetched = sources
    ctx.user_request = 'Кажется, называлось Мейл. Какие требования?'
    tools.call(ctx, 'search_products', {'query': 'Mail Pro'})
    tools.call(ctx, 'kb_search', {'query': 'Mail Pro требования'})
    assert 'error' in section(ctx) and not fetched


def test_empty_tab_does_not_borrow_article_from_neighbour(sources, monkeypatch):
    monkeypatch.setattr(cards, '_fetch', lambda url: '<li data-tab="licensing"></li><li data-tab="reviews"><article>На ядро.</article></li>')
    ctx, _, _ = sources
    tools.call(ctx, 'kb_search', {'query': 'Mail Pro лицензирование'})
    tools.call(ctx, 'search_products', {'query': 'Mail Pro'})
    out = section(ctx)
    assert 'licensing' not in out and 'На ядро' not in str(out) and 'note' in out


def test_card_is_not_a_discount_source():
    from app.agent import prices

    known = set()
    prices.collect_percents({'licensing': 'Лимит 25%', 'requirements': 'Свободно 30%',
                             'results': [{'snippet': 'Акция 10%'}]}, known)
    assert known == {10}


@pytest.mark.parametrize('results', [[], [{'snippet': 'Откройте раздел заказов в кабинете.'}]])
def test_buyer_help_does_not_use_answers_to_other_customers(monkeypatch, results):
    def help_only(query, limit, *, buyer_help):
        assert buyer_help is True
        return {'results': results}

    monkeypatch.setattr(tools.kb, 'kb_search', help_only)
    monkeypatch.setattr(tools.answers, 'find', lambda q: pytest.fail('Чужой заказ — не справка'))
    out = tools.call(tools.ToolContext('s'), 'kb_search', {'query': 'получение ключа', 'buyer_help': True})
    assert out['results'] == results and 'manager_answers' not in out
    if not results:
        assert 'менеджеру' in out['note']


_POST_PURCHASE = [json.loads(line) for line in Path(__file__).with_name('post_purchase_cases.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('case', _POST_PURCHASE, ids=lambda case: case['id'])
def test_post_purchase_scripted_handoff_and_help(monkeypatch, case):
    """Контракт каналов: шаг справки не создает заявку; передача сохраняет запрос и историю.
    Модель-сценарий проверяет исполнение решений, но не оценивает выбор живой модели.
    """
    from app import store
    from app.agent import loop
    from app.agent.llm import Scripted

    step = 'Проверьте раздел заказов в личном кабинете.'
    monkeypatch.setattr(tools.kb, 'kb_search', lambda *a, **kw: {'results': [{'snippet': step}]})
    sid = None
    for i, message in enumerate(case['turns']):
        handoff = case['expect']['handoff'] and i == len(case['turns']) - 1
        model = Scripted([{'name': 'handoff_to_manager', 'args': {'reason': 'Помощь после покупки', 'summary': message}},
                          'Передал вопрос менеджеру.'] if handoff else
                         [{'name': 'kb_search', 'args': {'query': message, 'buyer_help': True}}, step])
        result = loop.run_turn(sid, message, llm=model)
        sid = result['session_id']
        assert bool(store.open_handoff(sid)) == handoff
        if not handoff:
            assert result['trace'][0]['output']['results'][0]['snippet'] == result['reply']
    assert [m['content'] for m in store.messages(sid) if m['role'] == 'user'] == case['turns']
