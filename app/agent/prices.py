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
_NOT_ASSERT = re.compile(r"\bне\b|\bнет\b|\bни\b|уточн|провер|бюджет|в рамках|укладыва|улож|в пределах|превыша|лимит|"
                         r"вы (?:назвали|указали|написали|упомянули|сказали|говорили|называли|привели|видели|писали)|"
                         r"по вашим словам|с ваших слов|названн\w+ вами|у конкурент|друго\w+ поставщик|"
                         r"(?:чужом|другом|стороннем|вашем) (?:сч[её]те|кп|предложении)", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?![а-яёa-z])|\n")  # "руб. за место" фразу не заканчивает
# Метка [[facts: ...]] это запись модели о клиенте: клиент ее не видит, код убирает ее из ответа. Бюджет и просьба
# о скидке в ней ("budget=1400 руб. за место", "нужна скидка 15%") это слова клиента, а не цена и не обещание.
FACTS_MARK = re.compile(r"\[\[facts:.*?\]\]", re.S | re.I)

# Сумма без валюты в свободной формулировке: "отдам за 4999", "обойдется вам в 4999", "стоимость лицензии 4999
# (без НДС)", "1 лицензия = 4999". Между словом о цене и числом допускаются только строчные русские слова:
# название продукта ("стоимость Microsoft 365") суммой не считается. Год и число с единицей измерения тоже.
_LOOSE_CUE = (rf"(?:{_CUE}|составит|составляет|будет|отдам|отдадим|уступлю|уступим|"
              r"обойд[её]тся(?:\s+вам)?\s+в|выйдет\s+в|за|=)")
_NOT_A_SUM = re.compile(r"артикул|\bкод|номер|№|заказ|сч[её]т|\bкп\b|\bинн\b|\bкпп\b|тел|верси|редакци|выпуск|"
                        r"\bдо\b|\bс\b|\bот\b|\bпо\b", re.I)
LOOSE = re.compile(rf"(?<![а-яёa-z])({_LOOSE_CUE})((?:[\s:=~(«\"—–-]+|[а-яё]+\s+){{0,4}}?)(?<![\d.,])({_NUM})"
                   rf"(?![{_SP}.,'’`]?\d){_MULT}?(?!\s*(?:{_UNIT}|{_CUR}))"
                   # после суммы конец фразы, знак или предлог; иначе это количество чего-то: "за 100 виртуальных машин"
                   r"(?=\s*(?:$|[.,;:!?()\n])|\s+(?:в|за|с|со|без|на|при|и|или|для|плюс|это)(?![а-яёa-z]))", re.I)
LOOSE_MAX = 99_999_999  # длиннее это ИНН, телефон или артикул
# Сумма словами: "пять тысяч рублей", "полторы тысячи руб.". Сверить ее с каталогом нельзя, значит называть нельзя.
_NUM_WORD = (r"(?:один|одна|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|\w+надцать|двадцать|тридцать|"
             r"сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|сто|двести|триста|четыреста|\w+сот|"
             r"полтор[аы]|пол|тысяч[аиу]?|миллион(?:а|ов)?|млн|тыс\.?)")
WORDS = re.compile(rf"(?<![а-яёa-z])(?:{_NUM_WORD}\s+){{0,5}}(?:сто|двести|триста|четыреста|[а-яё]+сот|тысяч[аиу]?|"
                   rf"миллион(?:а|ов)?)(?:\s+{_NUM_WORD}){{0,4}}\s+{_RUB}", re.I)
# то же без слова "руб", но сразу после слова о цене: "стоит пять тысяч", "итого полтора миллиона"; не количество
WORDS_BARE = re.compile(rf"\b{_CUE}(?:\s+{_FILLER}|\s*[:=—–])*\s+((?:{_NUM_WORD}\s+){{0,5}}(?:тысяч[аиу]?|"
                        rf"миллион(?:а|ов)?)(?![а-яё]))(?!\s*(?:{_UNIT}|{_CUR}))", re.I)


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


_NAMED = re.compile(r"\b([A-ZА-ЯЁ][\w.+-]*)\s+(\d{3,})(?![\d.,]\d)")  # слово с заглавной: "на 500 узлов" не название


def collect_named(obj, acc: set) -> None:
    """Числа из названий продуктов и вариантов вместе со словом перед ними: "яндекс 360", "office 2021".

    Такое число после слова о цене ("Стоимость Яндекс 360 для бизнеса") это часть названия, а не сумма."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("title", "name") and isinstance(v, str):
                acc.update(f"{w.lower()} {n}" for w, n in _NAMED.findall(v))
            else:
                collect_named(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_named(v, acc)


def _in_name(text: str, start: int, raw: str, named) -> bool:
    """Число стоит в ответе после того же слова, что и в названии из ответов инструментов."""
    word = re.search(r"([A-Za-zА-Яа-яЁё][\w.+-]*)\s+$", text[:start])
    return bool(word) and f"{word.group(1).lower()} {raw}" in named


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


def _bad(text: str, known, said, positions=None, named=()) -> list[tuple[int, int]]:
    """Места в тексте с суммой, которой нет в данных инструментов и которая не подана как слова клиента.

    С positions сюда же попадает известная сумма, которая стоит не у своей позиции (см. price_pairs).
    named это числа из названий (collect_named): число без валюты в составе названия суммой не считается.
    """
    out = []
    marks = [m.span() for m in FACTS_MARK.finditer(text)]
    hidden = lambda a, b: any(x <= a and b <= y for x, y in marks)  # внутри метки [[facts: ...]]
    for a, b, values in mentions(text):
        for v in values:
            if v < 1 or _is_known(v, known) or hidden(a, b):
                continue
            if _is_known(v, said) and not _asserts_price(_sentence_at(text, a)):
                continue  # клиент сам назвал эту сумму, и ответ повторяет ее как его слова, а не как цену товара
            out.append((a, b))
            break
    taken = [(a, b) for a, b, _ in mentions(text)]
    free = lambda a, b: all(b <= x or a >= y for x, y in taken + out)
    for m in LOOSE.finditer(text):
        v, a, b = number(m.group(3)), m.start(3), m.end(4) if m.group(4) else m.end(3)
        if v is None or not free(a, b) or hidden(a, b):
            continue
        year = v == int(v) and 1900 <= v <= 2100 and len(m.group(3)) == 4 and not m.group(4)
        v *= _scale(m.group(4))  # "цена лицензии 5 тыс.", "обойдется в 120 тыс. на всех"
        if not BARE_MIN <= v <= LOOSE_MAX or _is_known(v, known) or _NOT_A_SUM.search(m.group(2)) or year:
            continue  # "стоимость по артикулу 4601546", "цена действует до 2027"
        if _in_name(text, a, m.group(3), named):
            continue  # "стоимость Яндекс 360 для бизнеса"
        if _is_known(v, said) and not _asserts_price(_sentence_at(text, a)):
            continue
        out.append((a, b))
    for m in list(WORDS.finditer(text)) + list(WORDS_BARE.finditer(text)):
        a, b = m.span(m.lastindex or 0)
        if free(a, b) and not hidden(a, b) and not _NOT_ASSERT.search(_sentence_at(text, a)):
            out.append((a, b))
    out += [(a, b) for a, b, _, verdict in price_pairs(text, positions) if verdict == FOREIGN and (a, b) not in out]
    return sorted(out)


def unknown(text: str, known, said=(), positions=None, named=()) -> list[str]:
    """Суммы в тексте ответа, которых нет среди известных.

    known это суммы из ответов инструментов, said это суммы из слов клиента: вторые можно повторить
    ("в ваш бюджет 300 000 руб. укладывается"), но утверждать ими цену товара нельзя ("лицензия стоит 500 руб.").
    positions это позиции диалога (collect_positions): известная сумма у чужой позиции тоже считается ошибкой.
    """
    return [text[a:b].strip() for a, b in _bad(text, known, said, positions, named)]


def mask_unknown(text: str, known, said=(), positions=None, named=()) -> str:
    """Последний рубеж: неизвестные суммы заменяются словами, известные остаются как есть."""
    for a, b in reversed(_bad(text, known, said, positions, named)):
        text = text[:a] + "цена по запросу" + text[b:]
    # "цена 12 345 руб." не должна стать "цена цена по запросу", а "около 100 тыс." не должно стать "около цена..."
    return re.sub(r"(?:\b(?:цена|цены|около|примерно|порядка|приблизительно)\s+)+цена по запросу",
                  lambda m: "Цена по запросу" if m.group(0)[0].isupper() else "цена по запросу", text, flags=re.I)


# --- Цена у своей позиции --------------------------------------------------------------------------------------------
# Обе суммы взяты из каталога, но стоят не у своих позиций: "А стоит 20 000, Б стоит 10 000" при ценах 10 000 и
# 20 000. Проверка "сумма есть в данных диалога" этого не видит. Здесь узкая проверка привязки: во фразе или пункте
# списка названа ровно одна известная позиция (метка [[offer:id]], артикул, название варианта или продукта из
# ответов инструментов) и есть цена за единицу. Если ни одна сумма фразы к этой позиции не относится, а одна из них
# это цена за единицу другой позиции диалога, цена стоит не у своей позиции. Позицию однозначно определить нельзя
# (две позиции во фразе, названия нет): проверка молчит, ложная тревога хуже пропуска.
OWN, FOREIGN, OTHER = "своя", "чужая", "прочая"
_ROWS = ("offers", "lines", "items")  # где в ответах инструментов лежат позиции: варианты, строки расчета и КП
_UNIT_PRICE = re.compile(r"стоит|стоимост|\bцен[аыуе]\b|ценой|\bпо\s+\d|за (?:одну |1 |кажд\w+ )?(?:лицензи|штук|шт\b|"
                         r"комплект|мест|единиц|пользовател|устройств|узел|пк\b|копи)", re.I)
_REF_MARK = re.compile(r"\[\[(offer|product):\s*([\w-]+)\]\]", re.I)
POSITIONS_KEPT = 60  # сколько позиций диалог помнит между ходами


def collect_positions(out, acc: dict) -> None:
    """Позиции из ответа инструмента: id варианта -> название, артикул, продукт, цены за единицу и все суммы."""
    if not isinstance(out, dict):
        return
    titles = {str(p.get("product_id")): p.get("title") or "" for p in out.get("products") or [] if isinstance(p, dict)}
    blocks = [out] + ([out["first_product"]] if isinstance(out.get("first_product"), dict) else [])
    for block in blocks:
        product = block.get("product") if isinstance(block.get("product"), dict) else {}
        pid = str(product.get("product_id") or block.get("product_id") or "")
        title = product.get("title") or titles.get(pid, "")
        for row in (r for key in _ROWS for r in block.get(key) or [] if isinstance(r, dict)):
            if row.get("offer_id") is None:
                continue
            p = acc.setdefault(str(row["offer_id"]), {"name": "", "code": "", "product": "", "pid": "",
                                                       "unit": set(), "sums": set()})
            p["name"], p["code"] = row.get("name") or p["name"], row.get("vendor_code") or p["code"]
            p["product"], p["pid"] = title or p["product"], pid or p["pid"]
            for k, v in row.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and _PRICE_KEY.search(k):
                    p["sums"].add(float(v))
                    if k.startswith("price"):  # price, price_with_discount: цена за единицу
                        p["unit"].add(float(v))


def load_positions(rows, offers=()) -> dict:
    """Позиции прошлых ходов (dump_positions) и запомненные варианты диалога (id, name, price)."""
    acc = {str(r[0]): {"name": r[1], "code": r[2], "product": r[3], "pid": r[4], "unit": set(r[5]), "sums": set(r[6])}
           for r in rows or []}
    for o in offers or []:
        if o.get("id") is not None and o.get("price"):
            p = acc.setdefault(str(o["id"]), {"name": o.get("name") or "", "code": "", "product": "", "pid": "",
                                              "unit": set(), "sums": set()})
            p["unit"].add(float(o["price"]))
            p["sums"].add(float(o["price"]))
    return acc


def dump_positions(acc: dict) -> list:
    return [[i, p["name"], p["code"], p["product"], p["pid"], sorted(p["unit"]), sorted(p["sums"])]
            for i, p in list(acc.items())[-POSITIONS_KEPT:]]


# Между названием позиции и суммой допускаются только слова о цене, количестве и сроке ("стоит", ": ", ", 50 лицензий
# по", "на 1 год стоит"). Любое другое слово может быть вторым подлежащим ("дешевле: вариант на 2 года стоит ..."),
# и тогда сумма относится не к названной позиции: такая фраза не проверяется.
_TIGHT = re.compile(r"(?:[\s:;,.()«»\"“”„—–-]|\d+(?:[.,]\d+)?\s*%?|стоит|стоимость\w*|цен[аыуе]|ценой|по|за|это|будет|"
                    r"составит|составляет|обойд[её]тся|выйдет|в|от|на|с|со|без|скидкой|ндс|всего|итого|нов(?:ая|ую)|"
                    r"одн[ауы]|кажд\w+|(?:шт|штук\w*|лиценз\w+|пользовател\w+|узл\w+|мест\w*|пк|год\w*|лет|месяц\w*|мес)\.?)*",
                    re.I)
_QUOTES = "«»\"“”„'"


def _plain(s: str) -> str:
    return re.sub(rf"[{_QUOTES}]", "", (s or "").lower().replace("ё", "е")).split()


def _named(sentence: str, positions: dict) -> tuple[set, list[tuple[int, int]]]:
    """Позиции, названные во фразе, если это одна позиция (иначе пустое множество), и места упоминаний."""
    found, spans = [], []  # по одному множеству id на каждое упоминание
    for m in _REF_MARK.finditer(sentence):
        kind, ident = m.group(1).lower(), m.group(2)
        ids = {i for i, p in positions.items() if (i if kind == "offer" else p["pid"]) == ident}
        if not ids:
            return set(), []  # названа позиция, про которую ничего не известно: привязку не определить
        found.append(ids)
        spans.append(m.span())
    low = sentence.lower().replace("ё", "е")  # длина та же: места совпадают с местами во фразе
    names: dict[tuple, set] = {}
    for i, p in positions.items():
        for words in {tuple(_plain(p["name"])), tuple(_plain(p["product"])), tuple(_plain(p["code"]))}:
            if len(" ".join(words)) >= 5:
                names.setdefault(words, set()).add(i)
    for words in sorted(names, key=lambda w: -len(" ".join(w))):  # длинное название раньше короткого внутри него
        hit = re.compile(r"(?<![\w-])" + rf"[\s{_QUOTES}]+".join(map(re.escape, words)) + r"(?![\w-])")
        for m in hit.finditer(low):
            low = low[:m.start()] + " " * (m.end() - m.start()) + low[m.end():]
            found.append(names[words])
            spans.append(m.span())
    return (set.intersection(*found) if found else set()), spans


def _sentences(text: str):
    """Фразы и пункты списка с их местом в тексте. Метка после суммы ("16 800 руб. [[offer:1]], а также")
    относится к уже названной цене: перед меткой фраза не рвется."""
    start = 0
    for m in _SENTENCE.finditer(text):
        if text.startswith("[[", m.end()) and "\n" not in m.group(0):
            continue
        yield start, text[start:m.start()]
        start = m.end()
    yield start, text[start:]


def price_pairs(text: str, positions) -> list[tuple[int, int, float, str]]:
    """Суммы ответа, у которых однозначно определена позиция: (начало, конец, сумма, своя | чужая | прочая).

    Своя: цена за единицу или сумма этой позиции из ответов инструментов, она же со скидкой в пределах своей,
    цена на названное во фразе количество. Чужая: цена за единицу другой позиции диалога во фразе, где своей
    суммы нет и между названием и суммой стоят только слова о цене и количестве. Прочая: сумма, про которую по данным позиций
    сказать нечего (ее сверяет проверка известных сумм).
    """
    out = []
    if not positions or not config.PRICE_BINDING:
        return out
    hidden = [m.span() for m in FACTS_MARK.finditer(text)]
    for start, sentence in _sentences(text):
        if not _UNIT_PRICE.search(sentence) or any(x <= start < y for x, y in hidden):
            continue
        named, spans = _named(sentence, positions)
        sums = [(a, b, v) for a, b, values in mentions(sentence) for v in values if v >= 1]
        if not named or not sums:
            continue
        counts = {v for v in map(number, NUMBER.findall(sentence)) if v and v == int(v) and 2 <= v <= 100_000}
        own = set().union(*(positions[i]["sums"] | positions[i]["unit"] for i in named))
        own |= {u * q for i in named for u in positions[i]["unit"] for q in counts}
        others = {u for i, p in positions.items() if i not in named for u in p["unit"]}
        low = 1 - config.AGENT_DISCOUNT_MAX / 100
        mine = [any(x * low - TOLERANCE <= v <= x + TOLERANCE for x in own) for _, _, v in sums]
        for (a, b, v), ok in zip(sums, mine):
            verdict = OWN if ok else OTHER
            if not any(mine) and _is_known(v, others):
                prev = max((e for _, e in spans if e <= a), default=0)
                gap = sentence[prev:a]
                verdict = FOREIGN if _TIGHT.fullmatch(_REF_MARK.sub(" ", gap)) else OTHER
            out.append((start + a, start + b, v, verdict))
    return out


def misplaced(text: str, positions) -> list[str]:
    """Суммы, названные не у своей позиции: цена другой позиции диалога во фразе про эту."""
    return [text[a:b].strip() for a, b, _, verdict in price_pairs(text, positions) if verdict == FOREIGN]


# --- Скидка словами --------------------------------------------------------------------------------------------------
# Потолок скидки в расчете и в КП держит код. Здесь то же самое для текста ответа: пообещать словами скидку больше
# своей агент не вправе. Процент из справки или каталога этого диалога (акция вендора "переход со скидкой 50%")
# пересказать можно, но не от первого лица: "даю вам 50%" остается обещанием.
_PCT = r"(?:%|процент\w*)"
PERCENT = re.compile(rf"(\d+(?:[.,]\d+)?)\s*{_PCT}", re.I)
# процент словами: "десять процентов", "двадцать пять процентов" (берется старшее слово: для сравнения хватает)
_E = r"(?:ь|и|ью)"
_WORD_VALUES = tuple((re.compile(rx), v) for rx, v in (  # числительные от шести: меньшие обещанием сверх своей не будут
    (rf"шест{_E}", 6), (rf"сем{_E}", 7), (r"восемь|восьми", 8), (rf"девят{_E}", 9), (rf"десят{_E}", 10),
    (rf"одиннадцат{_E}", 11), (rf"двенадцат{_E}", 12), (rf"тринадцат{_E}", 13), (rf"четырнадцат{_E}", 14),
    (rf"пятнадцат{_E}", 15), (rf"шестнадцат{_E}", 16), (rf"семнадцат{_E}", 17), (rf"восемнадцат{_E}", 18),
    (rf"девятнадцат{_E}", 19), (rf"двадцат{_E}", 20), (rf"тридцат{_E}", 30), (r"сорока?", 40),
    (r"пятьдесят|пятидесяти", 50), (r"половин[ауы]", 50)))
WORD_PERCENT = re.compile(r"(?<![а-яё])((?:[а-яё]+\s+){1,2})процент", re.I)
# о снижении цены говорят и без слова "скидка": "минус 15% от цены", "цена будет на 20% ниже"
_CHEAPER = re.compile(r"скидк|дисконт|дешевле|ниже|минус|уступ|сброш|сниж|сбав|\bсним(?:у|ем)\b|\bскин(?:у|ем)\b", re.I)
REFUSAL = re.compile(r"не могу|не вправе|нельзя|не даю|не обещаю|не в моих|менеджер|больше\s+\d", re.I)
_HARD_REFUSAL = re.compile(r"не могу|не вправе|нельзя|не даю|не обещаю|не в моих", re.I)
_RESTATE = re.compile(r"прос(?:ите|или|ил|ит|ьб)|запр(?:ос|аш|ош)|хот(?:ите|ели)|по вопросу|насч[её]т|"
                      r"вы (?:назвали|указали|написали|упомянули|говорили)", re.I)
_TO_PERCENT = r"[^.!?%\d]{0,30}?(\d+(?:[.,]\d+)?)\s*" + _PCT
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


def _word_percents(sentence: str) -> list[str]:
    """Проценты, названные словами: "скидка десять процентов"."""
    found = []
    for m in WORD_PERCENT.finditer(sentence):
        values = [v for word in m.group(1).lower().split() for rx, v in _WORD_VALUES if rx.fullmatch(word)]
        value = max(values, default=None)
        if value is not None and "ндс" not in sentence[max(0, m.start() - 12):m.end() + 8].lower():
            found.append(str(value))
    return found


def _promises_too_much(sentence: str, allowed=()) -> bool:
    if not _CHEAPER.search(sentence) or _HARD_REFUSAL.search(sentence):
        return False
    over = [p for p in _discount_percents(sentence) + _word_percents(sentence) if _over(p)]
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
    return [s.strip() for s in _SENTENCE.split(FACTS_MARK.sub(" ", " ".join(replies))) if _promises_too_much(s, allowed)]


def cut_discount(text: str, allowed=()) -> str:
    """Последний рубеж: фраза с обещанием скидки больше своей заменяется отсылкой к менеджеру."""
    cut = DISCOUNT_CUT.format(limit=config.AGENT_DISCOUNT_MAX)
    out, done = [], False
    for chunk in re.split(f"({FACTS_MARK.pattern})", text, flags=re.S | re.I):
        if FACTS_MARK.fullmatch(chunk):  # запись о клиенте остается как есть: ее разберет код после проверки
            out.append(chunk)
            continue
        for part in re.split(f"({_SENTENCE.pattern})", chunk):
            if _promises_too_much(part, allowed):
                part = "" if done else cut
                done = True
            out.append(part)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()
