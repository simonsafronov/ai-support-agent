"""Short independent answers to a pending consultation question. No model/network."""
import pytest
from app import store
from app.agent import selection_dialog, task_selection
from test_task_selection import buyer


def start(buyer):
    r=buyer('Пока не понимаю, что выбрать.');sid=r['session_id']
    assert '?' in r['reply'] and store.find_session(sid)['items'][selection_dialog.KEY]['field']=='purpose'
    return sid,r['reply']


def test_short_purpose_then_noise_then_scope(buyer):
    sid,first=start(buyer)
    second=buyer('для защиты компа на Windows',sid)
    assert second['reply']!=first and 'личного' in second['reply'] and not second['events']
    state=store.find_session(sid)['items'][selection_dialog.KEY];assert state['field']=='protection' and state['filters']=={'platform':'Windows'}
    noise=buyer('ыы, бля, фыва',sid)
    assert 'Уточнение не разобрал' in noise['reply'] and not noise['trace'] and not noise['events']
    alt=buyer('Для домашнего ноутбука',sid);assert 'Антивирусы для дома' in alt['reply']
    state=store.find_session(sid)['items'][task_selection.KEY];assert state['filters']=={'platform':'Windows'}
    assert not store.last_quote(sid) and not store.open_handoff(sid)


@pytest.mark.parametrize('short,category',[
    ('Нужно защитить личный ноутбук','Антивирусы для дома'),('Бэкапы','резервного копирования'),('Защита рабочих компьютеров в офисе','Антивирусы для организаций')])
def test_independent_short_answers(buyer,short,category):
    sid,_=start(buyer);r=buyer(short,sid)
    assert category in r['reply'] and [t['name'] for t in r['trace']][:2]==['browse_categories','search_products']
    assert not r['events'] and not store.open_handoff(sid)


@pytest.mark.parametrize(('answer','expected'),[('Для себя','Антивирусы для дома'),('Для рабочих мест','Антивирусы для организаций'),('Нет, лучше резервные копии','резервного копирования')])
def test_two_continuations_after_identical_noisy_prefix(buyer,answer,expected):
    sid,_=start(buyer);buyer('Для безопасности моего ПК, Windows',sid)
    r=buyer('фыва, блни',sid); assert 'Windows' in r['reply'] and not r['events']
    r=buyer(answer,sid);assert expected in r['reply'] and not store.last_quote(sid) and not store.open_handoff(sid)


def test_switching_goals_not_an_approval(buyer):
    sid,_=start(buyer);p=buyer('PDF',sid);assert 'изменить существующий текст' in p['reply']
    n=buyer('Не документы, а бэкапы',sid);assert 'резервного копирования' in n['reply'] and not n['events']
    state=store.find_session(sid)['items'][task_selection.KEY];page=state['page']
    noise=buyer('ёпрст, блрр',sid);assert 'не распознал как выбор' in noise['reply'] and not noise['events']
    assert store.find_session(sid)['items'][task_selection.KEY]['page']==page
    new=buyer('Не бэкапы, теперь PDF',sid);assert 'изменить существующий текст' in new['reply']
    assert task_selection.KEY not in store.find_session(sid)['items'] and not store.last_quote(sid)


def test_denial_is_not_positive_goal(buyer):
    sid,_=start(buyer);r=buyer('Мне не нужны бэкапы',sid);assert 'исключаю' in r['reply'] and not r['trace']
    assert task_selection.KEY not in store.find_session(sid)['items']
