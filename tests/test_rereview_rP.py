"""Повторное ревью, цены: перестановка цен в одной фразе с двумя позициями и сумма в чужой валюте.

Модели здесь нет: ответы модели подставлены, проверка идет по тем же функциям, что стоят в цикле агента.
"""
import re

import pytest

from app import store
from app.agent import loop, prices
from app.agent.llm import Scripted

EDITIONS = [{"id": 1, "name": "Редакция А", "price": 10000}, {"id": 2, "name": "Редакция Б", "price": 20000}]
KNOWN = {10000.0, 20000.0}
SWAPPED = "Редакция А стоит 20 000 ₽, а Редакция Б стоит 10 000 ₽."  # пример ревьюера: цены обратные
RIGHT = "Редакция А стоит 10 000 ₽, а Редакция Б стоит 20 000 ₽."


def guards(res: dict) -> list[tuple]:
    return [(t["name"], t.get("outcome")) for t in res["trace"] if t["kind"] == "guard"]


def session_with(offers) -> str:
    sid = store.get_session(None)["id"]
    store.update_session(sid, items={"offers": offers})
    return sid


# --- Перестановка цен внутри одной фразы ------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    SWAPPED,
    "Редакция А стоит 20 000 ₽, Редакция Б стоит 10 000 ₽.",                         # без союза
    "Редакция А стоит 20 000 ₽ [[offer:1]], а Редакция Б стоит 10 000 ₽ [[offer:2]].",  # метка у своей цены
    "Редакция А [[offer:1]] стоит 20 000 ₽, а Редакция Б [[offer:2]] стоит 10 000 ₽.",  # номер в метке не количество
    "Цены за лицензию: Редакция А по 20 000 ₽, Редакция Б по 10 000 ₽.",
    "Вариант [[offer:1]] стоит 20 000 ₽, а вариант [[offer:2]] стоит 10 000 ₽ за лицензию.",  # только метки
    "Вариант [[offer:1]] за 20 000 ₽ и Standard [[offer:2]] за 10 000 ₽, цена за лицензию.",
])
def test_swap_inside_one_sentence_is_caught(reply):
    """Зачем: повторное ревью. Две позиции в одной фразе, у каждой цена другой: раньше проверка молчала."""
    positions = prices.load_positions(None, EDITIONS)
    assert prices.unknown(reply, KNOWN) == []  # обе суммы есть в каталоге: без позиций перестановку не видно
    assert prices.unknown(reply, KNOWN, (), positions) == ["20 000 ₽", "10 000 ₽"] == prices.misplaced(reply, positions)


@pytest.mark.parametrize("reply", [
    RIGHT,
    "Редакция А стоит 10 000 ₽ [[offer:1]], а Редакция Б стоит 20 000 ₽ [[offer:2]].",
    "Редакция А, 2 лицензии по 10 000 ₽, итого 20 000 ₽, а Редакция Б стоит 20 000 ₽.",
])
def test_right_order_passes(reply):
    positions = prices.load_positions(None, EDITIONS)
    assert prices.unknown(reply, KNOWN, (), positions) == [] == prices.misplaced(reply, positions)


THREE = EDITIONS + [{"id": 3, "name": "Редакция В", "price": 30000}]


@pytest.mark.parametrize("reply", [
    "Редакция В стоит на 10 000 ₽ больше, чем Редакция Б, за лицензию.",   # разница, а не цена
    "Редакция В дороже, чем Редакция Б, на 10 000 ₽ за лицензию.",
    "Редакция Б и Редакция В стоят от 10 000 до 20 000 ₽ за лицензию.",     # диапазон
    "Цена за лицензию: Редакция Б и Редакция В, 10 000 ₽ и 20 000 ₽.",      # две суммы в отрезке
    "На 1 год стоит 30 000 ₽ [[offer:3]], на 2 года стоит 10 000 ₽ [[offer:1]].",  # метка за суммой подписывает ее
    "Вариант [[offer:3]], на 2 года стоит 10 000 ₽ для малого и среднего бизнеса [[offer:1]].",
])
def test_ambiguous_sentences_stay_silent(reply):
    """Ложная тревога хуже пропуска: позиция суммы не определяется однозначно, проверка молчит."""
    positions = prices.load_positions(None, THREE)
    assert prices.misplaced(reply, positions) == [], reply
    assert prices.unknown(reply, KNOWN | {30000.0}, (), positions) == [], reply


# Позиции из настоящих диалогов прогонов 11-13 (названия, артикулы и цены из ответов инструментов): все в одном
# диалоге, чтобы цена любой из них была "чужой" для остальных.
REAL_TOOLS = [
    {"product": {"product_id": "p1", "title": "AnyDesk"}, "offers": [
        {"offer_id": 402265, "name": "AnyDesk (лицензия на 1 год), Solo", "price": 37193.0},
        {"offer_id": 402266, "name": "AnyDesk (лицензия на 1 год), Standard", "price": 57751.0},
        {"offer_id": 402267, "name": "AnyDesk (лицензия на 1 год), Advanced", "price": 126903.0}]},
    {"product": {"product_id": "p2", "title": "Программа КриптоПро CSP 5.0"}, "offers": [
        {"offer_id": 410137, "name": "КриптоПро CSP (бессрочная лицензия СКЗИ КриптоПро CSP 5.0), на одном рабочем "
                                     "месте", "price": 3700.0},
        {"offer_id": 411620, "name": "КриптоПро CSP 5.0: годовая лицензия на 1 рабочее место", "price": 1850.0}]},
    {"lines": [{"offer_id": 410137, "qty": 10, "price": 3700.0, "sum": 37000.0},  # расчет на 10 мест
               {"offer_id": 411620, "qty": 10, "price": 1850.0, "sum": 18500.0}], "total": 55500.0},
    {"product": {"product_id": "p3", "title": "Master Pdf Editor"}, "offers": [
        {"offer_id": 461796, "name": "Code Industry Master Pdf Editor (подписка на 2 года),", "price": 9100.0},
        {"offer_id": 461797, "name": "Code Industry Master Pdf Editor (подписка на 2 года),", "price": 8800.0}]},
    {"product": {"product_id": "p4", "title": "Astra Linux Special Edition"}, "offers": [
        {"offer_id": 517995, "name": "Astra Linux Special Edition 1.8, x86-64, «Воронеж», для рабочей станции, тип "
                                     "Базовая, на 12 мес. (для МСП)", "vendor_code": "TC1100X8600DIG000WS00-BS12MSP",
         "price": 5600.0},
        {"offer_id": 482112, "name": "Сертификат техподдержки на ОС Astra Linux Special Edition",
         "vendor_code": "TC1100X8600DIG000WS00-ST12", "price": 7900.0}]},
    {"product": {"product_id": "p5", "title": "Positive Technologies XSpider"}, "offers": [
        {"offer_id": 384692, "name": "Positive Technologies XSpider (продление, обновление в течение 1 года), на 256 "
                                     "хоста", "vendor_code": "PT-XS-IP256-EXT", "price": 93694.0},
        {"offer_id": 384705, "name": "Positive Technologies XSpider (продление на дополнительный хост к лицензии), на "
                                     "256 хоста", "vendor_code": "PT-XS-IP256-ADD-EXT", "price": 304.0}]},
]
REAL_REPLIES = [  # фразы ответов модели из прогонов 11-13, в каждой названо несколько позиций
    "Для AnyDesk в каталоге есть лицензии на 1 год для организации: Solo [[offer:402265]] стоит 37 193 руб. за одну "
    "позицию, Standard [[offer:402266]] стоит 57 751 руб. за одну позицию.",
    "Годовые лицензии для коммерческих организаций в каталоге такие: AnyDesk Solo [[offer:402265]] за 37 193 руб., "
    "AnyDesk Standard [[offer:402266]] за 57 751 руб., AnyDesk Advanced [[offer:402267]] за 126 903 руб., цена указана "
    "за одну позицию, как в названии.",
    "Цены остаются прежними: бессрочная 3 700 руб. за место (37 000 руб. на 10 мест) [[offer:410137]], годовая "
    "1 850 руб. за место (18 500 руб. на 10 мест) [[offer:411620]].",
    "Master PDF Editor, подписка на 2 года для организации, цена за одну лицензию зависит от количества: от 1 до 9 "
    "лицензий 9 100 руб. [[offer:461796]], от 10 до 35 лицензий 8 800 руб. [[offer:461797]].",
    "Базовая техподдержка на 12 месяцев (для малого и среднего бизнеса) стоит 5 600 руб. [[offer:517995]], "
    "сертификат на 12 месяцев для крупного бизнеса 7 900 руб. [[offer:482112]].",
    "- Продление на 256 хостов, 93 694 руб. [[offer:384692]], плюс продление на 44 дополнительных хоста по 304 руб. "
    "за хост [[offer:384705]].",
]


def real_dialog() -> tuple[dict, set]:
    positions, known = {}, set()
    for out in REAL_TOOLS:
        prices.collect(out, known)
        prices.collect_positions(out, positions)
    return positions, known


@pytest.mark.parametrize("reply", REAL_REPLIES)
def test_real_sentences_with_several_positions_pass(reply):
    """Зачем: проверка не должна резать витрину. Настоящие ответы с несколькими позициями в одной фразе проходят
    и с метками (так их видит цикл), и без меток (так их видит клиент)."""
    positions, known = real_dialog()
    for text in (reply, re.sub(r"\s*\[\[offer:\d+\]\]", "", reply)):
        assert prices.misplaced(text, positions) == [], text
        assert prices.unknown(text, known, (), positions) == [], text


def test_real_sentence_with_swapped_prices_is_caught():
    """Та же настоящая фраза про AnyDesk, но цены Solo и Standard переставлены."""
    positions, known = real_dialog()
    swapped = REAL_REPLIES[0].replace("37 193", "@").replace("57 751", "37 193").replace("@", "57 751")
    assert prices.unknown(swapped, known, (), positions) == ["57 751 руб.", "37 193 руб."]


def test_swap_in_one_sentence_goes_back_to_the_model_and_then_is_cut():
    """Зачем: повторное ревью проверяло полный цикл ответа. Перестановку модель переписывает после замечания,
    а если повторяет ее, обе суммы убирает код."""
    llm = Scripted([SWAPPED, RIGHT])
    res = loop.run_turn(session_with(EDITIONS), "Сколько стоят редакции?", llm=llm)
    assert res["reply"] == RIGHT and guards(res) == [("price", "rewritten")]
    assert "это цены других позиций" in llm.calls[1][-1]["content"]
    res = loop.run_turn(session_with(EDITIONS), "Сколько стоят редакции?", llm=Scripted([SWAPPED, SWAPPED]))
    assert "20 000" not in res["reply"] and "10 000" not in res["reply"] and guards(res) == [("price", "cut")]


# --- Сумма в чужой валюте ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    "Редакция А стоит 10 000 долларов.",
    "Редакция А стоит $10 000.",
    "Редакция А стоит 10 000 $.",
    "Редакция А стоит 10 000 €.",
    "Редакция А стоит 10 000 евро.",
    "Редакция А стоит 10 000 USD.",
    "Редакция А стоит 10 000 eur.",
    "Редакция А стоит 10 000 у.е.",
    "Редакция А стоит 10 тыс. долларов.",
    "Цены от 10 000 до 20 000 долларов.",
])
def test_foreign_currency_is_not_a_known_price(reply):
    """Зачем: повторное ревью. Каталог рублевый, "10 000 долларов" при цене 10 000 руб. проходило как известная
    сумма, потому что число совпало."""
    positions = prices.load_positions(None, EDITIONS)
    assert prices.unknown(reply, KNOWN, (), positions), reply
    assert prices.misplaced(reply, positions) == []  # не "цена другой позиции": такой суммы в каталоге нет вовсе
    assert not re.search(r"\d", prices.mask_unknown(reply, KNOWN, (), positions).replace("Редакция А", ""))


def test_ruble_sums_are_checked_as_before():
    positions = prices.load_positions(None, EDITIONS)
    for reply in ("Редакция А стоит 10 000 руб.", "Редакция А стоит 10 000 ₽.", "Редакция А стоит 10000р.",
                  "Редакция А стоит 10 000 RUB.", "Редакция А стоит 10 тыс. рублей."):
        assert prices.unknown(reply, KNOWN, (), positions) == [], reply
    assert prices.unknown("Редакция А стоит 12 345 руб.", KNOWN, (), positions) == ["12 345 руб."]


def test_client_amount_in_foreign_currency_can_be_repeated_but_not_asserted():
    """Сумму в валюте, которую назвал сам клиент, агент вправе повторить как его слова, но не выдать за цену."""
    said = prices.client_amounts("Бюджет 5000 долларов, что можно взять?")
    for reply in ("В ваш бюджет 5000 долларов укладывается Редакция А за 10 000 руб.",
                  "Вы назвали бюджет $5000, цены в каталоге в рублях: Редакция А стоит 10 000 руб."):
        assert prices.unknown(reply, KNOWN, said) == [], reply
    assert prices.unknown("Редакция А стоит 5000 долларов.", KNOWN, said) == ["5000 долларов"]
    assert prices.unknown("Редакция А стоит 7000 долларов.", KNOWN, said) == ["7000 долларов"]


def test_foreign_currency_goes_back_to_the_model():
    llm = Scripted(["Редакция А стоит 10 000 долларов.", "Редакция А стоит 10 000 ₽."])
    res = loop.run_turn(session_with(EDITIONS), "Сколько стоит редакция А?", llm=llm)
    assert res["reply"] == "Редакция А стоит 10 000 ₽." and guards(res) == [("price", "rewritten")]
    assert "10 000 долларов" in llm.calls[1][-1]["content"] and "других позиций" not in llm.calls[1][-1]["content"]
