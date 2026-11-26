"""Проверка цен: любая сумма в ответе агента должна быть из данных инструментов или из слов самого клиента.

Суммы сравниваются до копейки с допуском меньше рубля: "1 841 руб." при цене 1 841,40 допустимо,
"1 999 руб." при цене 2 000 уже нет. Числа клиента без слова "руб" считаются суммой, только если
похожи на нее по величине: "50 пользователей" не делает допустимой цену 50 руб.

Суммой считается число с валютой (рубли, доллары, евро), с множителем ("100 тысяч рублей", "99К руб."),
обе границы диапазона "от A до B руб." и число без валюты сразу после слов "итого", "стоит", "цена", "сумма".
Там же проверка скидки словами: обещать скидку больше своей агент не вправе.
"""
from __future__ import annotations

import re

from app import config

_SP = "    -   　"  # пробелы между разрядами: обычный, неразрывный, тонкий, узкий
# число: группы по три цифры через пробел, точку, запятую или апостроф ("99.999", "99'999") либо подряд;
# группы по три не дают склеить соседнее количество с ценой ("до 14 1 841,40 руб." это 1 841,40)
_NUM = rf"\d{{1,3}}(?:[{_SP}.,'’`]\d{{3}})+(?:[.,]\d{{1,2}})?(?!\d)|\d+(?:[.,]\d{{1,2}})?"
_RUB = r"(?:₽|руб(?:лей|ля|ль|\.)?|р\.|р(?![а-яёa-z])|ru[br]\b)"
_FOREIGN = r"(?:\$|€|usd\b|eur\b|евро\b|доллар\w*|у\.\s?е\.?)"
_CUR = rf"(?:{_RUB}|{_FOREIGN})"
# множитель: слово через пробел ("100 тысяч", "1,2 млн") или буква вплотную к числу ("99К");
# "5 000 к оплате" не множитель
_MULT = r"((?<=\d)[кk](?![а-яёa-z0-9])|\s*(?:тыс(?:яч[аи]?|\.)?|млн\.?|миллион(?:а|ов)?|млрд\.?))"
_START = r"(?<![\d.,])"

# сумма с валютой: "5 000 руб.", "5 тыс. руб.", "₽5 000", "5000р", "$1200", "1 200 €", "99К руб."
MONEY = re.compile(rf"(?:[₽$€]\s*({_NUM}){_MULT}?|{_START}({_NUM}){_MULT}?\s*{_CUR})", re.I)
# диапазон: "от 50 000 до 99 999 руб." и "50000-99999 руб.": нижняя граница тоже сумма
RANGE = re.compile(rf"(?:\bот\s+({_NUM}){_MULT}?\s+до\s+|(?<![\w.,-])({_NUM}){_MULT}?[-–—])"
                   rf"({_NUM}){_MULT}?\s*{_CUR}", re.I)
# сумма без валюты: число сразу после слова о цене ("итого 99 999 с НДС", "стоит 1,2 млн"), но не количество
_CUE = r"(?:итого|итог|стоит|стоить|стоимость\w*|цен[аыуе]|ценой|сумм[аыуе]|суммой|всего|обойд[её]тся|выйдет|к оплате)"
_FILLER = (r"(?:[:=—–-]|будет|составит|составляет|около|примерно|порядка|приблизительно|ровно|получается|получится|"
           r"заказа|покупки|со скидкой|с ндс|без ндс|с уч[её]том ндс|за вс[её])")
_UNIT = (r"(?:%|процент|шт|лиценз|пользовател|устройств|узл|компьютер|пк\b|рабоч|сотрудник|человек|мест|вариант|"
         r"позици|предложен|наименован|продукт|товар|год|лет\b|мес|дн|недел|час|минут|сервер|ядер|ядр|почтов|ящик|"
         r"копи|гб\b|мб\b|тб\b|-?\s?фз|знак|раз\b|балл|токен|запрос|обращен|диалог|комплект|ключ|верси)")
BARE = re.compile(rf"\b{_CUE}(?:\s+{_FILLER}|\s*[:=—–])*\s+({_NUM})(?![{_SP}.,'’`]?\d){_MULT}?"
                  rf"(?!\s*(?:{_UNIT}|{_CUR}))", re.I)
NUMBER = re.compile(rf"{_START}(?:{_NUM})")
SCALED = re.compile(rf"{_START}({_NUM}){_MULT}", re.I)
TOLERANCE = 0.51
BARE_MIN = 100  # число без валюты считается суммой от сотни: "итого 3 позиции" и "всего 2" это не деньги
CLIENT_MIN, CLIENT_MAX = 1_000, 99_999_999  # бюджет клиента без слова "руб": от тысячи; длиннее это ИНН или телефон
_PRICE_KEY = re.compile(r"price|total|sum|vat|amount")
# Сумму из слов клиента агент вправе повторить (бюджет, цена из чужого счета, отказ "цену 1 руб. назвать не могу"),
# но не вправе утверждать ею цену товара: "подтвердите цену 500 руб." не делает допустимым ответ "стоит 500 руб."
_ASSERT = re.compile(r"стоит|стоимост|\bцен[аыуе]\b|ценой|итого|\bсумм[аыуе]\b|составит|составляет|обойд[её]тся|выйдет|"
                     r"к оплате|подтвержда[юе]|верно|договорились|оформл|\bда[,.!\s]|"
                     r"за (?:лицензи|штук|шт\b|комплект|мест|позици|единиц|кажд)", re.I)
_NOT_ASSERT = re.compile(r"\bне\b|\bнет\b|\bни\b|уточн|провер|бюджет|в рамках|укладыва|в пределах|превыша|лимит|"
                         r"вы (?:назвали|указали|написали|упомянули|сказали|говорили|называли|привели|видели|писали)|"
                         r"по вашим словам|с ваших слов|названн\w+ вами|у конкурент|друго\w+ поставщик|"
                         r"(?:чужом|другом|стороннем|вашем) (?:сч[её]те|кп|предложении)", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?![а-яёa-z])|\n")  # "руб. за место" фразу не заканчивает


def _scale(word: str | None) -> float:
    word = (word or "").strip().lower()
    return 1e9 if word.startswith("млрд") else 1e6 if word.startswith(("млн", "миллион")) else 1e3 if word else 1


def number(s: str) -> float | None:
    s = re.sub(rf"[{_SP}'’`\s]", "", s)
    m = re.fullmatch(r"(\d{1,3}(?:[.,]\d{3})+)([.,]\d{1,2})?", s)
    if m:  # точка или запятая между группами по три цифры это разделитель разрядов: "99.999" это 99 999
        s = re.sub(r"[.,]", "", m.group(1)) + (m.group(2) or "")
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def mentions(text: str) -> list[tuple[int, int, list[float]]]:
    """Упоминания сумм в тексте без пересечений: (начало, конец, значения в рублях или в названной валюте)."""
    found = []
    for m in RANGE.finditer(text):
        lo_raw, lo_mult = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        lo, hi = number(lo_raw), number(m.group(5))
        if lo is None or hi is None:
            continue
        hi_scale = _scale(m.group(6))
        lo_scale = _scale(lo_mult) if lo_mult else (hi_scale if lo <= hi else 1)  # "от 50 до 100 тыс. руб."
        found.append((m.start(), m.end(), [lo * lo_scale, hi * hi_scale]))
    for m in MONEY.finditer(text):
        v = number(m.group(1) or m.group(3))
        if v is not None:
            found.append((m.start(), m.end(), [v * _scale(m.group(2) or m.group(4))]))
    for m in BARE.finditer(text):
        v = number(m.group(1))
        if v is not None and v * _scale(m.group(2)) >= BARE_MIN:
            found.append((m.start(1), m.end(2) if m.group(2) else m.end(1), [v * _scale(m.group(2))]))
    found.sort(key=lambda f: (f[0], f[0] - f[1]))  # при пересечении остается более раннее и длинное: диапазон целиком
    out, end = [], -1
    for f in found:
        if f[0] >= end:
            out.append(f)
            end = f[1]
    return out


def amounts(text: str) -> list[tuple[str, float]]:
    """Суммы в тексте: (как написано, значение)."""
    return [(text[a:b].strip(), v) for a, b, values in mentions(text) for v in values]


def collect(obj, acc: set) -> None:
    """Все числа из полей с ценами и суммами в ответе инструмента."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and _PRICE_KEY.search(k):
                acc.add(float(v))
            else:
                collect(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect(v, acc)


def client_amounts(text: str) -> set[float]:
    """Суммы, которые назвал сам клиент (бюджет, цена из чужого счета): агент вправе их повторить."""
    found = {v for _, v in amounts(text)}
    for m in SCALED.finditer(text):  # "бюджет 300 тысяч" без слова "руб"
        v = number(m.group(1))
        if v is not None:
            found.add(v * _scale(m.group(2)))
    for raw in NUMBER.findall(text):
        v = number(raw)
        if v is not None and CLIENT_MIN <= v <= CLIENT_MAX:
            found.add(v)
    return found


def _is_known(value: float, known) -> bool:
    return any(abs(value - k) <= TOLERANCE for k in known)


def _sentence_at(text: str, pos: int) -> str:
    start = 0
    for m in _SENTENCE.finditer(text):
        if m.start() >= pos:
            return text[start:m.start()]
        start = m.end()
    return text[start:]


def _asserts_price(sentence: str) -> bool:
    return bool(_ASSERT.search(sentence)) and not _NOT_ASSERT.search(sentence)


def _bad(text: str, known, said) -> list[tuple[int, int]]:
    """Места в тексте с суммой, которой нет в данных инструментов и которая не подана как слова клиента."""
    out = []
    for a, b, values in mentions(text):
        for v in values:
            if v < 1 or _is_known(v, known):
                continue
            if _is_known(v, said) and not _asserts_price(_sentence_at(text, a)):
                continue  # клиент сам назвал эту сумму, и ответ повторяет ее как его слова, а не как цену товара
            out.append((a, b))
            break
    return out


def unknown(text: str, known, said=()) -> list[str]:
    """Суммы в тексте ответа, которых нет среди известных.

    known это суммы из ответов инструментов, said это суммы из слов клиента: вторые можно повторить
    ("в ваш бюджет 300 000 руб. укладывается"), но утверждать ими цену товара нельзя ("лицензия стоит 500 руб.").
    """
    return [text[a:b].strip() for a, b in _bad(text, known, said)]


def mask_unknown(text: str, known, said=()) -> str:
    """Последний рубеж: неизвестные суммы заменяются словами, известные остаются как есть."""
    for a, b in reversed(_bad(text, known, said)):
        text = text[:a] + "цена по запросу" + text[b:]
    # "цена 12 345 руб." не должна стать "цена цена по запросу", а "около 100 тыс." не должно стать "около цена..."
    return re.sub(r"(?:\b(?:цена|цены|около|примерно|порядка|приблизительно)\s+)+цена по запросу",
                  lambda m: "Цена по запросу" if m.group(0)[0].isupper() else "цена по запросу", text, flags=re.I)


# --- Скидка словами --------------------------------------------------------------------------------------------------
# Потолок скидки в расчете и в КП держит код. Здесь то же самое для текста ответа: пообещать словами скидку больше
# своей агент не вправе. Процент из справки или каталога этого диалога (акция вендора "переход со скидкой 50%")
# пересказать можно, но не от первого лица: "даю вам 50%" остается обещанием.
PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
REFUSAL = re.compile(r"не могу|не вправе|нельзя|не даю|не обещаю|не в моих|менеджер|больше\s+\d", re.I)
_HARD_REFUSAL = re.compile(r"не могу|не вправе|нельзя|не даю|не обещаю|не в моих", re.I)
_RESTATE = re.compile(r"прос(?:ите|или|ил|ит|ьб)|запр(?:ос|аш|ош)|хот(?:ите|ели)|по вопросу|насч[её]т|"
                      r"вы (?:назвали|указали|написали|упомянули|говорили)", re.I)
_TO_PERCENT = r"[^.!?%\d]{0,30}?(\d+(?:[.,]\d+)?)\s*%"
# агент дает скидку сам: процент идет сразу за этими словами
_GIVE_SELF = re.compile(r"(?:\bда(?:ю|м|дим|[её]м)\b|предоставл?(?:ю|яю|им|яем)\b|сдела(?:ю|ем|л[аи]?)\b|"
                        r"примен(?:ю|яю|им|яем|ил[аи]?)\b|учт[уеё]м?\b|уч[её]л[аи]?\b|"
                        r"для вас скидк\w*|ваша скидк\w*)" + _TO_PERCENT, re.I)
# скидка названа как условие покупки: обещание, если процента нет в справке и каталоге этого диалога
_GIVE = re.compile(r"(?:предостав\w+|со скидкой|скидка (?:составит|будет|составляет))" + _TO_PERCENT, re.I)
# "менеджер согласовал скидку 50%": агент этого знать не может, решение менеджера клиент получает от менеджера
_APPROVED = re.compile(r"(?<!будет )(?<!быть )(?<!не )(?:согласовал[аи]?\b|согласована\b|одобрил[аи]?\b|одобрена\b|"
                       r"разрешил[аи]?\b|подтвердил[аи]?\b|подтверждена\b)", re.I)
# "скидка 15% возможна после согласования", "10% не получится": отсылка к чужому решению или отказ, а не обещание
_DEFER = re.compile(r"согласовани|согласу[ею]т|согласовыва|рассмотр|\bне\b|\bнет\b", re.I)
DISCOUNT_CUT = "Скидку больше {limit:g}% согласует менеджер."


def _percent(value: str) -> float:
    return float(value.replace(",", "."))


def _over(value: str) -> bool:
    return _percent(value) > config.AGENT_DISCOUNT_MAX


def collect_percents(obj, acc: set) -> None:
    """Проценты из текста ответа инструмента (справка, каталог): акцию вендора агент вправе пересказать."""
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            collect_percents(v, acc)
    elif isinstance(obj, str):
        acc.update(_percent(p) for p in PERCENT.findall(obj))


def _discount_percents(sentence: str) -> list[str]:
    """Проценты фразы, кроме ставки НДС: "цена со скидкой 5% включает НДС 22%" скидку больше своей не обещает."""
    return [m.group(1) for m in PERCENT.finditer(sentence)
            if "ндс" not in sentence[max(0, m.start() - 12):m.end() + 8].lower()]


def _promises_too_much(sentence: str, allowed=()) -> bool:
    if "скидк" not in sentence.lower() or _HARD_REFUSAL.search(sentence):
        return False
    over = [p for p in _discount_percents(sentence) if _over(p)]
    if not over:
        return False
    if any(_over(m.group(1)) for m in _GIVE_SELF.finditer(sentence)) or _APPROVED.search(sentence):
        return True
    if all(any(abs(_percent(p) - a) < 1e-9 for a in allowed) for p in over):
        return False  # все проценты фразы взяты из справки или каталога: это пересказ акции, а не обещание
    if any(_over(m.group(1)) for m in _GIVE.finditer(sentence)):
        return True
    return not (REFUSAL.search(sentence) or _RESTATE.search(sentence) or _DEFER.search(sentence))


def discount_over_limit(replies: list[str], allowed=()) -> list[str]:
    """Фразы, где агент дает скидку больше своей (AGENT_DISCOUNT_MAX).

    Отказ ("15% дать не могу"), отсылка к менеджеру и повтор просьбы клиента ("вы просите 15%") сюда не попадают.
    allowed это проценты из ответов инструментов этого диалога: акцию из справки можно пересказать.
    """
    return [s.strip() for s in _SENTENCE.split(" ".join(replies)) if _promises_too_much(s, allowed)]


def cut_discount(text: str, allowed=()) -> str:
    """Последний рубеж: фраза с обещанием скидки больше своей заменяется отсылкой к менеджеру."""
    cut = DISCOUNT_CUT.format(limit=config.AGENT_DISCOUNT_MAX)
    out, done = [], False
    for part in re.split(f"({_SENTENCE.pattern})", text):
        if _promises_too_much(part, allowed):
            part = "" if done else cut
            done = True
        out.append(part)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()
