"""Проверка цен: любая сумма в ответе агента должна быть из данных инструментов или из слов самого клиента.

Суммы сравниваются до копейки с допуском меньше рубля: "1 841 руб." при цене 1 841,40 допустимо,
"1 999 руб." при цене 2 000 уже нет. Числа клиента без слова "руб" считаются суммой, только если
похожи на нее по величине: "50 пользователей" не делает допустимой цену 50 руб.
"""
from __future__ import annotations

import re

_NUM = r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CURRENCY = r"(?:₽|руб(?:лей|ля|ль|\.)?|р\.|р(?![а-яёa-z]))"
_SCALE = {"тыс": 1_000, "млн": 1_000_000}
# сумма с валютой: "5 000 руб.", "5 тыс. руб.", "₽5 000", "5000р"; группы по три цифры не дают склеить
# соседнее количество с ценой ("до 14 1 841,40 руб." это 1 841,40)
MONEY = re.compile(rf"(?<![\d.,])(?:₽\s*({_NUM})|({_NUM})\s*(?:(тыс|млн)\.?\s*)?{_CURRENCY})", re.I)
NUMBER = re.compile(rf"(?<![\d.,])(?:{_NUM})")
TOLERANCE = 0.51
CLIENT_MIN, CLIENT_MAX = 1_000, 99_999_999  # бюджет клиента без слова "руб": от тысячи; длиннее это ИНН или телефон
_PRICE_KEY = re.compile(r"price|total|sum|vat|amount")


def number(s: str) -> float | None:
    s = re.sub(r"[\s  ]", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def amounts(text: str) -> list[tuple[str, float]]:
    """Суммы в тексте: (как написано, значение в рублях)."""
    found = []
    for m in MONEY.finditer(text):
        v = number(m.group(1) or m.group(2))
        if v is not None:
            found.append((m.group(0).strip(), v * _SCALE.get((m.group(3) or "").lower(), 1)))
    return found


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
    for raw in NUMBER.findall(text):
        v = number(raw)
        if v is not None and CLIENT_MIN <= v <= CLIENT_MAX:
            found.add(v)
    return found


def _is_known(value: float, known) -> bool:
    return any(abs(value - k) <= TOLERANCE for k in known)


def unknown(text: str, known) -> list[str]:
    """Суммы в тексте ответа, которых нет среди известных."""
    return [raw for raw, v in amounts(text) if v >= 1 and not _is_known(v, known)]


def mask_unknown(text: str, known) -> str:
    """Последний рубеж: неизвестные суммы заменяются словами, известные остаются как есть."""
    def sub(m: re.Match) -> str:
        v = number(m.group(1) or m.group(2))
        v = None if v is None else v * _SCALE.get((m.group(3) or "").lower(), 1)
        return m.group(0) if v is None or v < 1 or _is_known(v, known) else "цена по запросу"
    return MONEY.sub(sub, text)
