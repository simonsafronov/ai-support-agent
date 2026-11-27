"""Состав черновика КП: те ли позиции и количества, что заказал клиент, и сходится ли сумма с каталогом.

Проверка "черновик_кп" отвечает только на вопрос, появился ли документ. Эта отвечает, что в нем: у кейса с КП
в expect задан список quote_items (артикул или слова из названия, количество), и последний успешный make_quote
диалога сверяется с ним позиция в позицию. Цена каждой строки сверяется с ценой того же предложения в каталоге
(цены в каталоге нет: строка не проходит, кроме цены по запросу), суммы пересчитываются. Документ на одну лицензию
вместо сорока проверку не проходит.
"""
from __future__ import annotations

from math import isfinite

from app import config

CENT = 0.011  # копейка с запасом на округление


def last_quote(run: dict) -> dict | None:
    """Последний успешно сформированный черновик КП диалога: ответ инструмента make_quote."""
    quotes = [x["output"] for t in run["turns"] for x in t["tools"]
              if x["name"] == "make_quote" and isinstance(x.get("output"), dict) and x["output"].get("ok")]
    return quotes[-1] if quotes else None


def _is(item: dict, want: dict) -> bool:
    name = str(item.get("name", "")).lower()
    if want.get("code") and str(item.get("vendor_code") or "").lower() != want["code"].lower():
        return False
    return all(word.lower() in name for word in want.get("has", []))


def mismatches(want_items: list[dict], quote: dict | None, catalog_price=None) -> list[str]:
    """Расхождения КП с заказом. Пустой список: состав, количества и суммы верны.

    catalog_price(offer_id) дает цену предложения в каталоге или None, если ее там нет. Эталона нет, а в КП цена
    стоит: строка сверку не проходит. Без функции цена с каталогом не сверяется (так проверяются сохраненные диалоги
    без базы).
    """
    if quote is None:
        return ["КП не сформировано"]
    items, bad = list(quote.get("items") or []), []
    if len(items) != len(want_items):
        bad.append(f"позиций {len(items)}, заказано {len(want_items)}")
    left = list(items)
    for want in want_items:
        label = want.get("code") or " ".join(want.get("has", []))
        found = [i for i in left if _is(i, want)]
        if not found:
            bad.append(f"нет позиции: {label}")
            continue
        item = found[0]
        left.remove(item)
        if item.get("qty") != want["qty"]:
            bad.append(f"{label}: количество {item.get('qty')}, заказано {want['qty']}")
    return bad + arithmetic(quote, catalog_price)


def arithmetic(quote: dict, catalog_price=None) -> list[str]:
    """Расхождения в числах КП: цена строки с каталогом, сумма строки, итог. Годится для любого КП прогона."""
    items, bad = list(quote.get("items") or []), []
    try:
        discount = float(quote.get("discount_percent") or 0)
    except (TypeError, ValueError):
        return ["неверная скидка в КП"]
    if not isfinite(discount) or not 0 <= discount <= config.AGENT_DISCOUNT_MAX:
        return ["скидка в КП за пределами полномочий агента"]
    for i in items:
        price, qty = float(i.get("price") or 0), i.get("qty") or 0
        if abs(price * qty - float(i.get("sum") or 0)) > CENT * max(qty, 1):
            bad.append(f"{i.get('vendor_code') or i.get('name')}: сумма строки не равна цене на количество")
        if not catalog_price:
            continue
        base = catalog_price(i.get("offer_id"))
        if base is None and i.get("price") is None:  # цена по запросу и в КП, и в каталоге: это совпадение
            continue
        if base is None:  # эталона нет: строку не с чем сверить, молча она не проходит
            bad.append(f"эталонная цена не найдена: {i.get('vendor_code') or i.get('name')} (offer_id "
                       f"{i.get('offer_id')}), в каталоге нет такого предложения или у него нет цены")
        elif abs(base * (1 - discount / 100) - price) > CENT:
            bad.append(f"{i.get('vendor_code') or i.get('name')}: цена {price}, в каталоге {base}")
    if abs(sum(float(i.get("sum") or 0) for i in items) - float(quote.get("total") or 0)) > CENT:
        bad.append("итог не равен сумме строк")
    return bad
