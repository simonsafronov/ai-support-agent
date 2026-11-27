"""Buyer help through the actual FTS and compact turn, no network or customer data."""
import json
import sqlite3

import pytest

from app import config
from app.agent import compact_knowledge as knowledge, loop
from app.agent.llm import Scripted
from app.kb import crawl, search

ROOT = 'https://docs.softline.com/ru-ru/lk/'
ARTICLES = {
    'activation': ('Ошибка активации', 'Сверьте редакцию в заказе с установленной редакцией. Не отправляйте ключ.'),
    'status': ('Статус заказа', 'Откройте карточку заказа, чтобы проверить его статус.'),
    'factor': ('Включение 2FA', 'Чтобы включить 2FA, выберите способ подтверждения.'),
    'label': ('Цветовые метки', 'Чтобы настроить цветовую метку, откройте палитру профиля.'),
}

@pytest.fixture
def turn(tmp_path, monkeypatch):
    for k,v in dict(COMPACT_LOCAL=True, LLM_PROVIDER='openai', LLM_API_KEY='',
                    LLM_BASE_URL='http://127.0.0.1:18347/v1').items():
        monkeypatch.setattr(config, k, v)
    db_path = tmp_path / 'kb.sqlite'
    monkeypatch.setattr(search, 'DB_PATH', db_path)
    with sqlite3.connect(db_path) as db:
        db.executescript(crawl.SCHEMA)
        for slug,(title,body) in ARTICLES.items():
            d = db.execute('INSERT INTO docs(url,title,source,fetched_at,text) VALUES(?,?,?,?,?)',
                           (ROOT+slug, title, 'docs', 'fixture', body)).lastrowid
            c = db.execute('INSERT INTO chunks(doc_id,heading,text) VALUES(?,?,?)', (d,title,body)).lastrowid
            db.execute('INSERT INTO chunks_fts(rowid,heading,text) VALUES(?,?,?)',
                       (c, search.index_text(title), search.index_text(body)))
    def ask(question, rewrite):
        return loop.run_turn(None, question, llm=Scripted([json.dumps(dict(route='help',query=rewrite))]))
    return ask

@pytest.mark.parametrize('question,slug', [
    ('Ключ уже пришёл. При активации «Букволит» пишет «не та редакция». Как поступить?', 'activation'),
    ('Поставил версию для дома вместо рабочей: при активации ошибка редакции. Что проверить?', 'activation'),
    ('Какой статус у моего учебного заказа?', 'status'),
    ('Как настроить цветовую метку в учебном профиле?', 'label'),
])
def test_extra_context_not_required_verbatim(turn, question, slug):
    reply = turn(question, ARTICLES[slug][0])['reply']
    assert ROOT+slug in reply and ARTICLES[slug][1] in reply
    if slug == 'status':
        assert 'не вижу' in reply.lower()

@pytest.mark.parametrize('question', ['Как отключить 2FA?', 'Как получить лицензионный ключ?',
                                      'Как выбрать способ доставки?', 'Как вернуть деньги за подписку?',
                                      'После покупки SableFox не синхронизирует аудиодорожки. Что делать?'])
def test_unrelated_topic_or_opposite_action_is_not_a_source(turn, question):
    r = turn(question, 'Включение 2FA')
    assert ROOT not in str(r)

@pytest.mark.parametrize('url,source', [('https://fixtures.invalid/status','store'),
                                        (ROOT+'status','store'), ('https://docs.softline.com/ru-ru/seller/status','docs')])
def test_source_provenance_independent_of_content(url, source):
    title,body = ARTICLES['status']
    assert not knowledge.fits_buyer(dict(title=title, heading=title, snippet=body, url=url, source=source), 'Статус заказа')

def test_unconfirmed_payment_is_not_a_second_payment_instruction(turn):
    result = turn('Но я уже оплатил его. Что теперь делать?', 'нетсовпадения')
    assert 'повтор' in result['reply'].lower() and 'не вижу' in result['reply'].lower() and ROOT not in result['reply']


@pytest.mark.parametrize('body', ['Статус заказа: ваш заказ оплачен.',
                                  'Статус заказа: пришлите пароль от кабинета.',
                                  'Статус заказа: пишите client@example.invalid.'])
def test_shared_topic_not_enough_for_personal_claim_or_private_copy(body):
    item = dict(title='Статус заказа', heading='Статус заказа', snippet=body, url=ROOT+'status', source='docs')
    assert not knowledge.fits_buyer(item, 'Какой статус заказа?')


@pytest.mark.parametrize('question,required', [
    ('При запуске не проходит активация другой редакции. Куда обратиться?', 'редакцию'),
    ('Пропало письмо с лицензионным ключом после покупки.', 'номер заказа'),
    ('Купил лишнюю лицензию, хочу вернуть деньги.', 'не могу подтвердить'),
    ('Сменил телефон, при входе нужен второй фактор.', 'коды подтверждения не присылайте'),
    ('Бухгалтеру нужен УПД по прошлому заказу.', 'названия нужного документа'),
])
def test_miss_gives_safe_next_step_not_an_unrelated_source(turn, question, required):
    reply = turn(question, 'совсемнетхита')['reply']
    assert ROOT not in reply and required in reply


@pytest.mark.parametrize('q', ['Не знаю, какую программу купить для работы с документами.',
                                'Ищу приложение для подготовки бухгалтерских документов.'])
def test_software_selection_does_not_display_a_document_procedure(q):
    source = dict(title='Закрывающие документы', heading='УПД', source='store',
                  url='https://store.softline.ru/b2b/', snippet='Подписанный УПД скачайте в разделе документов.')
    assert not knowledge.fits_buyer(source, q)
    assert 'Что нужно сделать' in knowledge.learned_reply(q, {'query': 'УПД', 'results': []})

@pytest.mark.parametrize('text,prior,expected', [
    ('Установил правильную редакцию, теперь заработало.', 'При активации возникла ошибка.', 'заработало'),
    ('После обновления ошибка исчезла.', '', 'заработало'),
    ('После сверки редакции ошибка активации осталась.', '', 'не устранил'),
    ('Проверка не помогла.', 'Не запускается приложение', 'не устранил'),
    ('Когда заработает?', 'Ошибка при входе', None),
    ('Не получилось оформить новый заказ.', '', None),
])
def test_support_outcome_is_not_a_new_product_search(text, prior, expected):
    reply = knowledge.support_followup(text, prior)
    assert expected in reply if expected else reply is None

@pytest.mark.parametrize('q,wrong', [
    ('Примете заявку через электронный конкурс?', 'УПД скачайте в кабинете.'),
    ('Каким способом заплатить?', 'Покупки отображаются в списке заказов.'),
    ('Можно оплатить лицензии после поставки?', 'Мы гарантируем отсрочку всем.'),
])
def test_topically_adjacent_or_unsupported_article_is_not_evidence(q, wrong):
    assert not knowledge.fits_buyer(dict(title=q, heading=q, url=ROOT+'test', source='docs', snippet=wrong), q)
