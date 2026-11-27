"""Список позиций одним сообщением: разбор строк и сопоставление с каталогом кодом, модель не вызывается.

Клиент вставляет в чат спецификацию: ячейки из Excel (строки с табуляцией), нумерованный список
("1. KL4867RATFS - 50 шт"), названия с количеством ("Astra Linux SE 1.8 сервер, 2 лицензии") или просто артикулы.
parse отличает такой список от обычного сообщения из нескольких строк, match находит каждую строку в каталоге
и дает ей статус. Результат цикл агента записывает как шаг инструмента match_spec, а клиент видит таблицу.
"""
from __future__ import annotations

import re

from app.catalog import search as catalog
from app.quote import kp

TOOL = "match_spec"  # имя шага в журнале и в истории для модели; в описании инструментов модели его нет
MIN_ROWS = 3    # столько строк с артикулом или количеством делают сообщение списком
MAX_ROWS = 30   # столько строк сопоставляется за ход, как позиций в одном КП
LINE_MAX = 250  # строка длиннее это текст письма, а не позиция
RESULT_LIMIT = 12000  # знаков ответа для модели: 30 строк целиком, из усеченного списка КП не собрать
EXACT, CHECK, ABSENT, ON_REQUEST, SKIPPED = "точно", "проверьте", "нет в каталоге", "цена по запросу", "не разобрано"
NOTE = ("Клиент видит эту таблицу под ответом. total это сумма строк со статусом точно. Строки точно можно "
        "включить в КП с их offer_id и qty, остальные строки решает менеджер.")

_URL = re.compile(r"https?://\S+|\S+@\S+")  # адрес страницы и почта: в них бывают буквы с цифрами, но это не артикул
_MARK = re.compile(r"^\s*(?:\d{1,3}[.)]\s*|[-–—•*·]\s+)")  # нумерация и маркер списка: "1. ", "2) ", "- ", "• "
_UNIT = (r"(?:шт|штук[аи]?|лиц(?:енз\w*)?|ед(?:иниц\w*)?|экз\w*|компл\w*|польз\w*|узл\w*|устройств\w*|"
         r"раб(?:очих|\.)?\s?мест\w*|копи[йи]|pcs|licen[cs]es?|users?)")
_QTY = re.compile(rf"(?<![\w.,])(\d{{1,6}})\s*{_UNIT}\.?(?![\w])", re.I)                 # "50 шт", "2 лицензии"
_QTY_TAIL = re.compile(r"(?:\s+[-–—:=xх×*]|\s*[,;]|\s+на)\s*(\d{1,6})\s*\.?\s*$", re.I)  # "KL4867RATFS - 50"
_CELL_QTY = re.compile(rf"^(\d{{1,6}})(?:[.,]0+)?\s*(?:{_UNIT}\.?)?$", re.I)            # ячейка Excel "50", "50 шт"
_UNIT_CELL = re.compile(rf"^(?:{_UNIT}|ед\.?\s*изм\.?)\.?$", re.I)
_HEADER = re.compile(r"^(?:№|№\s*п/п|п/п|наименовани\w*|названи\w*|товар\w*|продукт\w*|артикул\w*|код\w*|"
                     r"кол(?:-во|\.|ичеств\w*)|ед\.?(?:\s*изм\.?)?|цен\w*|сумм\w*|стоимост\w*|примечани\w*|"
                     r"позици\w*|описани\w*|вендор\w*|производител\w*)$", re.I)
_TOTAL_ROW = re.compile(r"^(?:итого|всего)\b", re.I)
_LABEL = re.compile(r"(?i)(?<![а-яёa-z])(?:артикул|арт\.|sku|p/n)\s*[:№#]?\s*")
_WORD = re.compile(r"[0-9a-zа-я]+(?:[.,][0-9]+)*")
# слова о лицензии и сроке есть у любого продукта: по ним не видно, тот ли продукт нашелся
_GENERIC = {"лицензия", "лицензии", "лицензий", "лиц", "license", "продление", "продления", "подписка", "подписки",
            "новая", "для", "на", "по", "и", "с", "в", "год", "года", "лет", "мес", "бессрочная", "версия",
            "редакция", "электронная", "сервер", "серверная", "рабочая", "станция", "for", "the"}


def _article(word: str) -> bool:
    """Похоже на артикул вендора: буквы вместе с цифрами, от 5 знаков ("KL4867RATFS", "X5-STD-CF-RDNL-A").

    Слово из строчных русских букв с цифрами это дата или количество ("2024г", "10шт"), x86-64 это разрядность,
    четыре и больше групп знаков одной длины через дефис это ключ лицензии: таких артикулов в каталоге нет.
    """
    word = word.strip(".-/")
    letters = [c for c in word if c.isalpha()]
    groups = word.split("-")
    return (len(word) >= 5 and any(c.isdigit() for c in word) and len(letters) >= 2
            and not all("а" <= c <= "я" or c == "ё" for c in letters) and not re.match(r"(?i)[xх]86", word)
            and not (len(groups) >= 4 and len({len(g) for g in groups}) == 1))


def _articles(text: str) -> list[str]:
    return [w.strip(".-/") for w in re.split(r"[\s,;:()\[\]«»\"']+", _URL.sub(" ", text)) if _article(w)]


def _cells(line: str) -> tuple[str, int | None] | None:
    """Строка из ячеек Excel: (текст позиции, количество). Шапка таблицы и строка итога: None."""
    cells = [c.strip() for c in line.split("\t") if c.strip()]
    if not cells or all(_HEADER.match(c) for c in cells) or _TOTAL_ROW.match(cells[0]):
        return None
    if len(cells) > 1 and cells[0].isdigit():  # номер строки в первой колонке
        cells = cells[1:]
    text, qty = [], None
    for c in cells:
        if _UNIT_CELL.match(c) or (qty is not None and re.fullmatch(r"[\d\s.,]+", c)):
            continue  # единица измерения, а после количества цена и сумма
        m = _CELL_QTY.match(c)
        if m and text and qty is None:  # первое целое число после названия это количество
            qty = int(m.group(1))
        else:
            text.append(c)
    return " ".join(text), qty


def _row(line: str) -> dict | None:
    """Одна строка сообщения как позиция: {line, query, article, qty, strong}. Не позиция: None.

    strong: в строке есть артикул или количество. Без них позицией считается только пункт списка или строка
    ячеек, и то лишь внутри сообщения, где таких строк хватает (см. parse).
    """
    line = line.strip()
    if not line or len(line) > LINE_MAX or line.endswith(("?", ":")) or _TOTAL_ROW.match(line):
        return None
    item = "\t" in line or bool(_MARK.match(line))
    if "\t" in line:
        cells = _cells(line)
        if cells is None:
            return None
        text, qty = cells
    else:
        text, qty = _MARK.sub("", line), None
    if qty is None:
        found = list(_QTY.finditer(text))[-1:] or list(_QTY_TAIL.finditer(text))
        if found:
            qty = int(found[0].group(1))
            text = text[:found[0].start()] + text[found[0].end():]
    query = re.sub(r"\s{2,}", " ", _LABEL.sub(" ", text)).strip(" ,;:-–—.")
    if not re.search(r"[A-Za-zА-Яа-яЁё]", query):
        return None
    codes = _articles(query)
    qty = qty if qty and qty <= kp.MAX_QTY else None
    if not (codes or qty or item):
        return None
    return {"line": re.sub(r"\s*\t[\s\t]*", " | ", line)[:160], "query": query[:160],
            "article": codes[0] if codes else "", "qty": qty,
            "strong": bool(codes or qty)}


def parse(text: str) -> list[dict]:
    """Строки списка позиций {line, query, article, qty}; пусто, если сообщение не список.

    Список это от MIN_ROWS строк с артикулом или количеством. qty None: количество в строке не названо.
    """
    rows = [r for r in map(_row, (text or "").splitlines()) if r]
    if sum(r["strong"] for r in rows) < MIN_ROWS:
        return []
    return [{k: r[k] for k in ("line", "query", "article", "qty")} for r in rows]


def _words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower().replace("ё", "е"))


def _same(a: str, b: str) -> bool:
    """Одно слово в разных формах: "сервер" и "серверная", "лицензии" и "лицензия". Числа и короткие слова только
    целиком: "index" и "indeed" это разные слова."""
    short, long_ = sorted((a, b), key=len)
    if a == b or not short.isalpha() or len(short) < 4:
        return a == b
    return long_.startswith(short) or (len(short) >= 6 and long_.startswith(short[:-1]))


def _overlap(words: list[str], text: str) -> int:
    have = _words(text)
    return sum(1 for w in words if any(_same(w, h) for h in have))


def _relevant(query: str, product: dict) -> bool:
    """Нашелся тот продукт, а не соседний по одному общему слову: в его названии больше половины значимых слов."""
    words = [w for w in _words(query) if w.isalpha() and w not in _GENERIC]
    return bool(words) and _overlap(words, f"{product['title']} {product['vendor']}") * 2 > len(words)


def _by_code(hits: list[dict]) -> list[dict]:
    """Все предложения каталога с артикулом, который поиск нашел точным совпадением (у первого продукта)."""
    code, offers = hits[0]["offer_hit"]["vendor_code"].lower(), []
    for p in hits:
        if p["offer_hit"]["vendor_code"].lower() == code:
            found = catalog.get_product(p["product_id"], filters={"text": code}, limit=100)
            offers += [{**o, "product_id": p["product_id"]} for o in found.get("offers") or []
                       if (o.get("vendor_code") or "").lower() == code]
    return offers


def _candidates(row: dict, qty: int) -> tuple[dict | None, int, bool]:
    """Лучшее предложение под строку, сколько еще вариантов и найдено ли по артикулу.

    Артикул вендора ищется как точный артикул варианта. Не нашелся: поиск по названию, из вариантов лучшего
    продукта под количество первым идет тот, в названии которого больше слов строки ("1.8", "сервер").
    """
    res = catalog.search_products(row["query"], limit=8)
    hits = [p for p in res.get("products") or [] if p.get("offer_hit")]
    if hits:
        offers = _by_code(hits)
        return (offers[0], len(offers) - 1, True) if offers else (None, 0, True)
    products = [p for p in res.get("products") or [] if res.get("match") == "all" or _relevant(row["query"], p)]
    if not products:
        return None, 0, False
    pid = products[0]["product_id"]
    found = catalog.get_product(pid, filters={"users": qty, "nodes": qty, "qty": qty}, limit=100)
    words = _words(row["query"])
    offers = sorted(found.get("offers") or [], key=lambda o: -_overlap(words, f"{o['name']} {o.get('version') or ''}"))
    if not offers:
        return None, 0, False
    return {**offers[0], "product_id": pid}, found.get("matching", len(offers)) - 1 + len(products) - 1, False


def _span(offer: dict) -> str:
    lo, hi = offer.get("qty_min") or 1, offer.get("qty_max")
    return f"от {lo} до {hi}" if hi else f"от {lo}"


def _tier(offer: dict, qty: int) -> tuple[dict, str]:
    """Цена предложения действует для другой ступени количества: вариант того же названия под это количество."""
    found = catalog.get_product(offer["product_id"], filters={"qty": qty}, limit=100)
    for o in found.get("offers") or []:
        if o["name"] == offer["name"] and o.get("price") is not None and not kp.tier_problem(o, qty):
            return o, f"артикул {offer.get('vendor_code')} для покупки {_span(offer)} шт., на {qty} шт. подходит этот"
    return offer, f"цена действует при покупке {_span(offer)} шт."


def _line(n: int, row: dict) -> dict:
    """Строка таблицы: что клиент написал, что нашлось, статус, количество, цена и сумма."""
    qty = row["qty"] or 1
    out = {"n": n, "line": row["line"], "qty": qty, **({} if row["qty"] else {"qty_assumed": True})}
    if n > MAX_ROWS:
        return {**out, "status": SKIPPED, "hint": f"в одном списке разбирается до {MAX_ROWS} строк, эту уточнит менеджер"}
    best, others, by_code = _candidates(row, qty)
    if best is None:
        return {**out, "status": ABSENT}
    status, hints = EXACT if by_code and not others else CHECK, []
    if not by_code:
        hints.append("найдено по названию" + (f", артикула {row['article']} в каталоге нет" if row["article"] else ""))
    if others:
        hints.append(f"других вариантов: {others}")
    if best.get("price") is None:
        status = ON_REQUEST
    elif kp.tier_problem(best, qty):  # та же проверка ступени, что в расчете и КП
        status, (best, hint) = CHECK, _tier(best, qty)
        hints.append(hint)
    elif kp.price_to_confirm(best):
        status = CHECK
        hints.append("у артикула в каталоге несколько цен, цену подтвердит менеджер")
    if best.get("available") is False:
        hints.append(kp.FEED_UNAVAILABLE)
    out.update(status=status, offer_id=best["offer_id"], name=best["name"], vendor_code=best.get("vendor_code") or "",
               price=best.get("price"), available=best.get("available"), url=best.get("url") or "")
    if status == EXACT:
        out["sum"] = round(best["price"] * qty, 2)
    if hints:
        out["hint"] = "; ".join(hints)
    return out


def match(rows: list[dict]) -> dict:
    """Сопоставление строк списка с каталогом: по строке статус, найденное предложение, количество и сумма.

    точно: артикул вендора нашелся ровно одним предложением с ценой под это количество; проверьте: нашлось по
    названию, кандидатов несколько, количество вне ступени цены или у артикула несколько цен; цена по запросу:
    нашлось, цены нет; нет в каталоге. Строки сверх MAX_ROWS не разбираются, их уточнит менеджер.
    """
    lines = [_line(n, row) for n, row in enumerate(rows, 1)]
    counts: dict[str, int] = {}
    for ln in lines:
        counts[ln["status"]] = counts.get(ln["status"], 0) + 1
    return {"lines": lines, "counts": counts, "total": round(sum(ln.get("sum", 0) for ln in lines), 2), "note": NOTE}
