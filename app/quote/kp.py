"""Черновик коммерческого предложения (КП) в PDF по шаблону Softline.

Названия, артикулы и цены берутся только из каталога по offer_id, вся арифметика
считается здесь через Decimal. Модель передает лишь offer_id и количество.
Размеры и координаты в пунктах сняты с шаблона заказчика data/raw/kp_template.pdf.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, HRFlowable, KeepTogether, NextPageTemplate,
                                PageTemplate, Paragraph, Spacer, Table)
from reportlab.platypus.doctemplate import LayoutError

from app import config, store

# --- Условия, которые правит человек -------------------------------------------------------------------------------
VAT_RATE = Decimal("22")  # ставка НДС в РФ с 01.01.2026, проценты
DEFAULT_PAYMENT = "Предоплата"
DEFAULT_DELIVERY = "электронная поставка, 1-3 рабочих дня после оплаты"
VALID_DAYS = 14
MAX_ITEMS = 30
MAX_QTY = 100_000

NO_VAT = "Без НДС"  # ПО из реестра отечественного ПО: пп. 26 п. 2 ст. 149 НК РФ
ON_REQUEST = "по запросу"
QUOTE_NOTE = "КП сформировано по данным каталога. Заказ не создан."

SELLER = "ПАО «Софтлайн»"
ADDRESS = ("119021, город Москва, ул Льва Толстого, д.",
           "5 стр. 1, этаж 3, помещ. 1, ком. №2, 2а (а-311)",
           "Т +7 495 232 00 23 | info@softline.com",
           "www.softline.ru | www.softline.com")
DRAFT_TITLE = "Черновик коммерческого предложения."
DRAFT_TEXT = "Подготовлен ИИ-консультантом Softline Store. Заказ не создан."
VAT_FOOTNOTE = "НДС рассчитан автоматически."
ON_REQUEST_LINE = "Позиции с ценой по запросу рассчитает менеджер."
ON_REQUEST_HEAD = "Позиции с ценой по запросу, в итог не входят, их рассчитает менеджер:"
FEED_UNAVAILABLE = "По фиду: нет в наличии"
FEED_AVAILABLE = "По фиду: в наличии"
PRICE_MARK = "*"
PRICE_CHECK_LINE = "У артикула в каталоге несколько цен, цену подтвердит менеджер."

MSK = timezone(timedelta(hours=3))  # дата КП по московскому времени
_NUMBER_LOCK = threading.Lock()
CENT = Decimal("0.01")
NBSP = " "
GROUP = "\u202f"  # узкий неразрывный пробел между разрядами числа

# --- Оформление ----------------------------------------------------------------------------------------------------
ASSETS = Path(__file__).parent / "assets"
for _font, _file in (("Inter", "Inter-Regular.ttf"), ("Inter-Bold", "Inter-Bold.ttf"),
                     ("Inter-Light", "Inter-Light.ttf")):  # Inter, лицензия OFL: assets/Inter-LICENSE.txt
    pdfmetrics.registerFont(TTFont(_font, str(ASSETS / _file)))
pdfmetrics.registerFontFamily("Inter", normal="Inter", bold="Inter-Bold", italic="Inter", boldItalic="Inter-Bold")

PAGE_W, PAGE_H = A4
LEFT, RIGHT = 35.5, 567.0  # края таблицы в шаблоне, по ним выровнено все остальное
WIDTH = RIGHT - LEFT
TOP, BOTTOM = 41.9, 752.0  # верх логотипа и низ поля текста (ниже слоган), от верхнего края листа
TOP_NEXT = 88.0  # верх поля текста на второй и следующих страницах
COLS = {"code": 90.0, "name": 167.5, "qty": 42.0, "price": 72.0, "sum": 76.0, "vat": 84.0}  # в сумме WIDTH
HEAD = (("code", "Артикул"), ("name", "Наименование"), ("qty", "Кол-во"), ("price", "Цена, руб."),
        ("sum", "Сумма, руб."), ("vat", "В т.ч. НДС, руб."))  # заголовки шаблона, каждый в одну строку
MINUS = "−"
PAD = 4.0  # поля ячейки слева и справа
BRAND = colors.HexColor("#9F1A2F")  # красный из логотипа
BRAND_TINT = colors.HexColor("#FAF1F2")
GREY = colors.HexColor("#6B6B6B")


def _style(name: str, font: str = "Inter", size: float = 8.5, leading: float | None = None, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading or round(size * 1.25, 2),
                          **{"textColor": colors.black, **kw})


S_ADDRESS = _style("address", "Inter-Light", 10, 14.65)
S_TO = _style("to", size=9.3, leading=12, alignment=TA_RIGHT)
S_HELLO = _style("hello", size=9.3, leading=12, alignment=TA_CENTER)
S_INTRO = _style("intro", size=9.3, leading=12, leftIndent=3.5, firstLineIndent=35.4)
S_HEAD = _style("th", "Inter-Bold", 9, 11.5, alignment=TA_CENTER)
S_CODE = _style("code", size=8, leading=10)
S_NAME = _style("name")
S_QTY = _style("qty", alignment=TA_CENTER)
S_NUM = _style("num", alignment=TA_RIGHT)
S_NUM_BOLD = _style("num-bold", "Inter-Bold", alignment=TA_RIGHT)
S_TOTAL = _style("total", "Inter-Bold", 9, alignment=TA_CENTER)
S_SUBTOTAL = _style("subtotal", alignment=TA_CENTER)
S_FOOTNOTE = _style("footnote", size=7, alignment=TA_RIGHT, textColor=GREY)
S_TERMS = _style("terms", leading=12.5, leftIndent=43.1)
S_REQUEST = _style("request", leading=12.5, leftIndent=55.1, bulletIndent=43.1)
S_DRAFT = _style("draft", size=9, leading=13)
S_SIGN = _style("sign", size=9.3, leading=14)


# --- Данные ----------------------------------------------------------------------------------------------------------
def _offer(offer_id: int) -> dict | None:
    """Позиция каталога по id: через поиск каталога, а если он не дал позицию, прямо из таблицы offers."""
    try:
        from app.catalog.search import get_offer
        found = get_offer(offer_id)
        if isinstance(found, dict) and found.get("name"):
            return found
    except Exception:  # модуля поиска нет или он не знает такой id: ответ дает сама таблица
        pass
    con = sqlite3.connect(f"file:{config.CATALOG_DB}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id AS offer_id, name, vendor, vendor_code, price, available, url, registry_no, "
                          "product_id, qty_pricing, qty_min, qty_max, same_code_prices, price_ambiguous "
                          "FROM offers WHERE id=?", (offer_id,)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def _as_int(value) -> int | None:
    """Целое из аргумента модели: 5, 5.0 и "5" годятся, а 2.5, True и "пять" нет."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value)
    return None


def _price(value) -> Decimal | None:
    """Цена из каталога в Decimal; None, если цена по запросу."""
    if value is None or value == "":
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return _money(price) if price.is_finite() and price > 0 else None


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _vat(amount: Decimal | None, registry: bool) -> Decimal | str | None:
    """НДС в том числе: у ПО из реестра его нет, у суммы по запросу он неизвестен."""
    if registry:
        return NO_VAT
    return _money(amount * VAT_RATE / (100 + VAT_RATE)) if amount is not None else None


def _percent(value: Decimal) -> str:
    return f"{float(value):g}".replace(".", ",")


def _fmt(value: Decimal) -> str:
    """1054.17 -> «1 054,17»: разряды разделяет узкий неразрывный пробел.

    В тексте PDF он остается своим знаком, а колонки разделяет обычный пробел: при копировании строки
    таблицы количество не сливается с ценой.
    """
    return f"{value:,.2f}".replace(",", GROUP).replace(".", ",")


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _with_feed_status(text: str, line: dict) -> str:
    """Подпись у конкретной строки; старые документы без флага не объявляем отсутствующими."""
    available = line.get("available")
    if available is None:
        return escape(text)
    status = FEED_AVAILABLE if available else FEED_UNAVAILABLE
    return f"{escape(text)}<br/>{status}"


def _sentence(text: str) -> str:
    return text.rstrip(" .") + "."


def _totals(rows: list[dict]) -> tuple[Decimal, Decimal]:
    """Сумма и НДС по строкам: без позиций по запросу и без строк, где НДС нет."""
    return (sum((r["sum"] for r in rows if r["sum"] is not None), Decimal(0)),
            sum((r["vat"] for r in rows if isinstance(r["vat"], Decimal)), Decimal(0)))


def _error(text: str) -> dict:
    return {"ok": False, "error": text}


NO_CUSTOMER = "Заказчик уточняется"  # клиент не назвал организацию: черновик все равно нужен менеджеру


INN_RE = re.compile(r"\d{10}|\d{12}")  # юрлицо 10 цифр, ИП 12
KPP_RE = re.compile(r"\d{4}[\dA-Z]{2}\d{3}")


def requisites(customer: str, inn: str, kpp: str) -> tuple[str, str, list[str]]:
    """Реквизиты графы «Заказчик»: ИНН и КПП только правильной длины, плюс список того, чего не хватает."""
    inn, kpp = re.sub(r"\D", "", str(inn or "")), re.sub(r"\s", "", str(kpp or "")).upper()
    inn = inn if INN_RE.fullmatch(inn) else ""
    kpp = kpp if KPP_RE.fullmatch(kpp) and len(inn) != 12 else ""
    missing = [name for name, ok in (("название организации", _clean(customer) not in ("", NO_CUSTOMER)),
                                     ("ИНН", bool(inn)), ("КПП", bool(kpp) or len(inn) == 12)) if not ok]
    return inn, kpp, missing


def allowed_discount(percent) -> Decimal:
    """Скидка, которую агент вправе дать сам: не больше AGENT_DISCOUNT_MAX процентов, остальное решает менеджер."""
    try:
        asked = Decimal(str(percent or 0))
    except ArithmeticError:
        return Decimal(0)
    return max(Decimal(0), min(asked, Decimal(str(config.AGENT_DISCOUNT_MAX))))


def discount_arg(value) -> tuple[Decimal, str]:
    """Скидка из аргумента модели. Не число, NaN, меньше 0 или больше 100 считается нулем, причина идет в note."""
    if value is None or value == "":
        return Decimal(0), ""
    try:
        asked = Decimal(str(value).strip().rstrip("%").strip().replace(",", ".")) if not isinstance(value, bool) else None
    except ArithmeticError:
        asked = None
    if asked is None or not asked.is_finite() or not 0 <= asked <= 100:
        return Decimal(0), (f" Скидка {str(value)[:20]!r} не распознана: нужен процент от 0 до 100. "
                            "Черновик посчитан без скидки.")
    return asked, ""


def price_to_confirm(offer: dict) -> bool:
    """У артикула в каталоге несколько цен, и ступень количества разницу не объясняет: цену подтвердит менеджер.

    Зачем: иначе в документ молча уходит одна из цен артикула, а в магазине клиент видит другую.
    """
    prices = offer.get("same_code_prices") or []
    if isinstance(prices, str):  # из таблицы offers список приходит строкой JSON
        prices = json.loads(prices or "null") or []
    return bool(offer.get("price_to_confirm") or offer.get("price_ambiguous")) or (
        len(prices) > 1 and not offer.get("qty_pricing"))


def discounted(price: Decimal, discount: Decimal) -> Decimal:
    return _money(price * (100 - discount) / 100)


def tier_problem(offer: dict, qty: int) -> str:
    """Цена за штуку из ступени "от 10 до 14" на другое количество не годится: одна проверка для расчета и для КП.

    Зачем: иначе КП на 1 лицензию выходит по цене ступени 10-14, клиент получает сумму, которой в каталоге нет.
    """
    lo, hi = offer.get("qty_min"), offer.get("qty_max")
    if offer.get("qty_pricing") != "per_unit" or not ((lo and qty < lo) or (hi and qty > hi)):
        return ""
    span = f"от {lo or 1} до {hi}" if hi else f"от {lo} и больше"
    return (f"вариант действует при покупке {span} лицензий, а запрошено {qty} (offer_id {offer.get('offer_id')}). "
            f"Найдите вариант под это количество: get_product с quantity={qty}")


def make_quote(session_id: str, customer: str, items: list[dict], inn: str = "", payment: str = "",
               contact: str = "", delivery: str = "", kpp: str = "", discount_percent: float = 0) -> dict:
    """Собирает черновик КП: проверяет позиции, считает суммы и НДС, пишет PDF и запись в хранилище.

    payment не используется: условие оплаты в документе всегда DEFAULT_PAYMENT. Отсрочку магазин не дает,
    просьбу клиента о ней видит менеджер в карточке обращения. Параметр оставлен для старых вызовов.
    """
    asked, bad_discount = discount_arg(discount_percent)  # до любых записей: неверная скидка не роняет КП
    discount = allowed_discount(asked)
    customer = _clean(customer) or NO_CUSTOMER
    given_inn, given_kpp = _clean(inn), _clean(kpp)
    inn, kpp, _ = requisites(customer, inn, kpp)
    if not isinstance(items, list) or not items:
        return _error("Нет позиций для КП. Передайте в items хотя бы одну позицию: offer_id и qty.")
    if len(items) > MAX_ITEMS:
        return _error(f"В одном КП не больше {MAX_ITEMS} позиций, получено {len(items)}. "
                      "Сократите список или передайте запрос менеджеру.")

    lines, problems = [], []
    for n, item in enumerate(items, 1):
        item = item if isinstance(item, dict) else {}
        offer_id, qty = _as_int(item.get("offer_id")), _as_int(item.get("qty"))
        if offer_id is None:
            problems.append(f"позиция {n}: не указан offer_id (целое число, id варианта из каталога)")
            continue
        if qty is None or not 1 <= qty <= MAX_QTY:
            problems.append(f"позиция {n} (offer_id {offer_id}): количество qty должно быть целым числом "
                            f"от 1 до {MAX_QTY}, получено {item.get('qty')!r}")
            continue
        try:
            offer = _offer(offer_id)
        except sqlite3.Error:
            return _error("Каталог сейчас недоступен, КП не сформировано. Передайте обращение менеджеру.")
        if not offer:
            problems.append(f"позиция {n}: offer_id {offer_id} не найден в каталоге, возьмите id из get_product")
            continue
        tier = tier_problem(offer, qty)
        if tier:
            problems.append(f"позиция {n}: {tier}")
            continue
        list_price = _price(offer.get("price"))
        price = discounted(list_price, discount) if list_price is not None and discount else list_price
        amount = _money(price * qty) if price is not None else None
        list_sum = _money(list_price * qty) if list_price is not None else None
        registry = bool(_clean(offer.get("registry_no")))
        check = price is not None and price_to_confirm(offer)
        lines.append({"offer_id": offer_id, "name": _clean(offer.get("name")).strip(" ,;"),
                      "vendor_code": _clean(offer.get("vendor_code")), "qty": qty, "price": price, "sum": amount,
                      "vat": _vat(amount, registry),
                      "available": bool(offer["available"]) if offer.get("available") is not None else None,
                      **({"price_to_confirm": True} if check else {}),
                      "list": {"price": list_price, "sum": list_sum, "vat": _vat(list_sum, registry)}})
    if problems:
        return _error("КП не сформировано, исправьте и повторите: " + "; ".join(problems) + ".")

    total, vat_total = _totals(lines)
    list_total, list_vat_total = _totals([ln["list"] for ln in lines])
    on_request = any(ln["price"] is None for ln in lines)
    today = datetime.now(MSK).date()
    quote = {
        "customer": customer, "inn": inn, "kpp": kpp, "contact": _clean(contact),
        "payment": DEFAULT_PAYMENT,
        "delivery": (_clean(delivery) or DEFAULT_DELIVERY) if all(ln["available"] for ln in lines) else "",
        "date": today.strftime("%d.%m.%Y"), "valid_until": (today + timedelta(days=VALID_DAYS)).strftime("%d.%m.%Y"),
        "lines": lines, "total": total, "vat_total": vat_total, "has_price_on_request": on_request,
        "discount": discount, "list_total": list_total, "list_vat_total": list_vat_total,
    }
    out_items = [{**{k: v for k, v in ln.items() if k != "list"}, "price": _num(ln["price"]), "sum": _num(ln["sum"]),
                  "vat": _num(ln["vat"]) if isinstance(ln["vat"], Decimal) else ln["vat"]} for ln in lines]

    with _NUMBER_LOCK:  # номер, файл и запись идут вместе, чтобы два КП не получили один номер
        prev = store.last_quote(session_id)
        if prev and customer == NO_CUSTOMER and prev["customer"] != NO_CUSTOMER:
            quote["customer"] = customer = prev["customer"]  # название уже называли: при правке позиций не теряем
        seq = store.db().execute("SELECT COALESCE(MAX(id), 0) + 1 FROM quotes").fetchone()[0]
        quote["number"] = number = prev["number"] if prev else f"КП-{today:%Y%m%d}-{seq:04d}"
        try:
            pdf = _render(quote)
        except LayoutError:  # текст, который не помещается на лист целиком
            return _error("КП не сформировано: слишком длинный текст в названии заказчика, контакте или условиях. "
                          "Сократите его и повторите.")
        config.QUOTES_DIR.mkdir(parents=True, exist_ok=True)
        path = config.QUOTES_DIR / f"{number}.pdf"
        path.write_bytes(pdf)
        if prev:
            store.update_quote(prev["id"], customer, out_items, float(total))
        else:
            store.add_quote(session_id, number, customer, out_items, float(total), str(path))

    missing = requisites(customer, inn, kpp)[2]
    wrong = [name for name, given, kept in (("ИНН", given_inn, inn), ("КПП", given_kpp, kpp)) if given and not kept
             and not (name == "КПП" and len(inn) == 12)]
    note, saved = QUOTE_NOTE, {}
    if discount and list_total > total:
        note += (f" Цены и суммы в этом ответе указаны со скидкой {float(discount):g}%. В документе клиент видит "
                 "цены каталога, сумму без скидки, скидку и итог.")
        saved = {"total_without_discount": float(list_total), "discount_sum": float(list_total - total)}
    note += bad_discount
    if asked > discount:
        note += (f" Запрошено {_percent(asked)}%. Без купона применено {_percent(discount)}%: "
                 "скидку больше согласует менеджер, передайте ему исходный запрос.")
    if wrong:
        note += (f" {' и '.join(wrong)} в черновик не попал: в ИНН должно быть 10 цифр (у ИП 12), в КПП 9 знаков. "
                 "Уточните у клиента.")
    unavailable = [ln["name"] for ln in lines if ln["available"] is False and ln["price"] is not None]
    if unavailable:
        note += f" {FEED_UNAVAILABLE} — {'; '.join(unavailable)}. Срок поставки не указан."
    if on_request:
        note += " " + ON_REQUEST_LINE
    unsure = [ln["name"] for ln in lines if ln.get("price_to_confirm")]
    if unsure:
        note += (f" У артикула позиции {', '.join(unsure)} в каталоге несколько цен, в черновике цена выбранного "
                 "варианта. Скажите клиенту, что цену этой позиции подтвердит менеджер.")
    if missing:
        note += (f" В графе «Заказчик» не хватает: {', '.join(missing)}. Попросите их одним сообщением один раз; "
                 "клиент не дал: черновик остается как есть, недостающее он может прислать в этот чат позже. "
                 "Обращение менеджеру ради реквизитов не заводите.")
    return {"ok": True, "number": number, "updated": bool(prev), "contact_in_quote": bool(quote["contact"]),
            "customer_missing": missing,
            "url": f"/quotes/{number}.pdf", "file": str(path), "date": quote["date"],
            "valid_until": quote["valid_until"], "customer": customer, "items": out_items, "total": float(total),
            "vat_total": float(vat_total), "has_price_on_request": on_request, "discount_percent": float(discount),
            **saved, "price_to_confirm": unsure,
            "needs_availability_check": unavailable,
            "note": note}


# --- PDF -------------------------------------------------------------------------------------------------------------
class _Line(Flowable):
    """Одна строка в ячейке, без переноса. Текст уходит в PDF знак в знак: абзац reportlab заменил бы узкий
    пробел между разрядами обычным, и при копировании таблицы числа сливались бы с соседней колонкой."""

    def __init__(self, text: str, style: ParagraphStyle, width: float):
        super().__init__()
        self.text, self.style, self.width, self.height = text, style, width, style.leading

    def wrap(self, avail_width, avail_height):
        return self.width, self.height

    def draw(self):
        st, canv = self.style, self.canv
        canv.setFont(st.fontName, st.fontSize)
        canv.setFillColor(st.textColor)
        y = self.height - pdfmetrics.getAscent(st.fontName, st.fontSize)
        if st.alignment == TA_RIGHT:
            canv.drawRightString(self.width, y, self.text)
        elif st.alignment == TA_CENTER:
            canv.drawCentredString(self.width / 2, y, self.text)
        else:
            canv.drawString(0, y, self.text)


def _fit(text: str, width: float, style: ParagraphStyle) -> _Line:
    """Значение в ячейку одной строкой: если не помещается по ширине, шрифт уменьшается."""
    need = pdfmetrics.stringWidth(text, style.fontName, style.fontSize)
    if need > width:
        size = style.fontSize * width / need * 0.99
        style = ParagraphStyle(style.name + "-fit", parent=style, fontSize=size, leading=size * 1.25)
    return _Line(text, style, width)


def _wrap_code(code: str, width: float, style: ParagraphStyle) -> Paragraph:
    """Артикул: длинный переносится после разделителей (- _ / + . пробел), а не в случайном месте."""
    def wide(s: str) -> bool:
        return pdfmetrics.stringWidth(s.rstrip(), style.fontName, style.fontSize) > width

    rows, cur = [], ""
    for part in re.findall(r"[^-_/+.\s]*[-_/+.\s]*", code):
        if cur and wide(cur + part):
            rows.append(cur)
            cur = ""
        cur += part
        while wide(cur):  # кусок без разделителей шире колонки: режем по символам
            cut = next(k for k in range(len(cur) - 1, 0, -1) if k == 1 or not wide(cur[:k]))
            rows.append(cur[:cut])
            cur = cur[cut:]
    rows.append(cur)
    return Paragraph("<br/>".join(escape(r.rstrip()) for r in rows if r.strip()), style)


def column_widths(with_article: bool) -> dict:
    """Ширины колонок; без артикулов графа «Артикул» не печатается, ее место занимает наименование."""
    widths = dict(COLS)
    if not with_article:
        widths["name"] += widths.pop("code")
    return widths


def _split(q: dict) -> tuple[list[dict], list[dict]]:
    """Позиции таблицы и позиции по запросу: вторые идут блоком под таблицей, чтобы итог читался однозначно.

    Когда цены нет ни у одной позиции, все остаются в таблице: итога в рублях в таком КП нет.
    """
    priced = [ln for ln in q["lines"] if ln["sum"] is not None]
    return (priced, [ln for ln in q["lines"] if ln["sum"] is None]) if priced else (q["lines"], [])


def table_cells(q: dict) -> list[dict]:
    """Текст ячеек таблицы строка за строкой, как он будет напечатан: позиции, строки скидки, итог."""
    lines = _split(q)[0]
    off = q["list_total"] - q["total"]  # скидка в рублях: тогда позиции идут по ценам каталога
    shown = [ln["list"] if off else ln for ln in lines]

    def vat_text(rows: list[dict], vat_sum: Decimal, sign: str = "") -> str:
        """НДС итоговой строки: сумма, а когда складывать нечего, «Без НДС» или прочерк."""
        vats = [r["vat"] for r in rows]
        if any(isinstance(v, Decimal) for v in vats):
            return sign + _fmt(vat_sum)
        return NO_VAT if all(v == NO_VAT for v in vats) else "—"

    cells = []
    for ln, cell in zip(lines, shown):
        priced = cell["price"] is not None
        mark = PRICE_MARK if ln.get("price_to_confirm") else ""
        cells.append({"kind": "item", "code": ln["vendor_code"] or "—", "name": ln["name"], "qty": str(ln["qty"]),
                      "available": ln.get("available"), "price": _fmt(cell["price"]) + mark if priced else ON_REQUEST,
                      "sum": _fmt(cell["sum"]) if priced else ON_REQUEST,
                      "vat": _fmt(cell["vat"]) if isinstance(cell["vat"], Decimal) else (cell["vat"] or "—")})
    if off:
        cells += [{"kind": "subtotal", "name": "Сумма без скидки", "sum": _fmt(q["list_total"]),
                   "vat": vat_text(shown, q["list_vat_total"])},
                  {"kind": "subtotal", "name": f"Скидка {_percent(q['discount'])}%", "sum": MINUS + _fmt(off),
                   "vat": vat_text(shown, q["list_vat_total"] - q["vat_total"], MINUS)}]
    has_sum = any(ln["sum"] is not None for ln in lines)
    cells.append({"kind": "total", "name": "Итого", "sum": _fmt(q["total"]) if has_sum else ON_REQUEST,
                  "vat": vat_text(lines, q["vat_total"])})
    return cells


NAME_STYLE = {"item": S_NAME, "subtotal": S_SUBTOTAL, "total": S_TOTAL}


def _items_table(q: dict) -> Table:
    cells = table_cells(q)
    widths = column_widths(any(c["kind"] == "item" and c["code"] != "—" for c in cells))
    inner = {key: w - 2 * PAD for key, w in widths.items()}

    def flow(cell: dict, key: str):
        text, kind = cell[key], cell["kind"]
        if key == "code":
            return _wrap_code(text, inner[key], S_CODE)
        if key == "name":
            return Paragraph(_with_feed_status(text, cell), NAME_STYLE[kind])
        return _fit(text, inner[key], S_QTY if key == "qty" else S_NUM_BOLD if kind == "total" else S_NUM)

    data = [[Paragraph(title.replace(" ", NBSP), S_HEAD) for key, title in HEAD if key in widths]]
    data += [[flow(c, key) if c.get(key) else "" for key in widths] for c in cells]
    footnote = VAT_FOOTNOTE
    if any(c.get("price", "").endswith(PRICE_MARK) for c in cells):
        footnote += f"<br/>{PRICE_MARK} {PRICE_CHECK_LINE}"
    data.append([Paragraph(footnote, S_FOOTNOTE)] + [""] * (len(widths) - 1))
    tail = 3 + sum(c["kind"] == "subtotal" for c in cells)  # последняя позиция, строки скидки, итог и сноска
    return Table(data, colWidths=list(widths.values()), repeatRows=1, style=[
        ("GRID", (0, 0), (-1, -2), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), PAD), ("RIGHTPADDING", (0, 0), (-1, -1), PAD),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
        ("TOPPADDING", (0, -2), (-1, -2), 7), ("BOTTOMPADDING", (0, -2), (-1, -2), 7),
        ("SPAN", (0, -1), (-1, -1)),
        ("LEFTPADDING", (0, -1), (-1, -1), 0), ("RIGHTPADDING", (0, -1), (-1, -1), 0),
        ("TOPPADDING", (0, -1), (-1, -1), 3), ("BOTTOMPADDING", (0, -1), (-1, -1), 0),
        ("NOSPLIT", (0, -tail), (-1, -1)),  # не отрываются друг от друга
    ])


def _on_request_block(q: dict) -> list:
    """Позиции по запросу списком под таблицей: наименование, артикул и количество."""
    rows = _split(q)[1]
    if not rows:
        return []
    items = [Paragraph(_with_feed_status(f"{ln['name']}{', арт. ' + ln['vendor_code'] if ln['vendor_code'] else ''}, "
                                         f"{ln['qty']} шт.", ln), S_REQUEST, bulletText="•") for ln in rows]
    return [Paragraph(ON_REQUEST_HEAD, S_TERMS), *items, Spacer(1, 8)]


def _plain(children: list, widths: tuple, extra: list) -> Table:
    """Таблица без рамок для раскладки блоков в одну строку."""
    return Table([children], colWidths=widths, style=[
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0), *extra])


def _letterhead(canv, doc):
    """Логотип и слоган на каждой странице, на местах из шаблона."""
    canv.drawImage(str(ASSETS / "logo.png"), 50.0, PAGE_H - 68.9, width=137.5, height=27.05)
    canv.drawImage(str(ASSETS / "slogan.png"), 108.7, PAGE_H - 821.9, width=378.0, height=46.3)


def _letterhead_next(canv, doc):
    _letterhead(canv, doc)
    canv.setFont("Inter", 8)
    canv.setFillColor(GREY)
    canv.drawRightString(RIGHT - 6.5, PAGE_H - 66.8, f"{doc.title}, стр. {canv.getPageNumber()}")


def _render(q: dict) -> bytes:
    """PDF черновика КП по структуре шаблона заказчика."""
    to = (f"Куда: {escape(q['customer'])}" + (f", ИНН{NBSP}{escape(q['inn'])}" if q["inn"] else "")
          + (f", КПП{NBSP}{escape(q['kpp'])}" if q.get("kpp") else ""))
    phone_glued = re.sub(r"(?<=[\d()+-]) (?=[\d(])", NBSP, q["contact"])  # номер телефона не рвется на строки
    contact = Paragraph(f"Контакт заказчика: {escape(phone_glued)}", S_TO) if q["contact"] else Spacer(1, 12)
    head = _plain([[Spacer(1, 42.3), Paragraph("<br/>".join(ADDRESS), S_ADDRESS)],
                   [Spacer(1, 15.6), Paragraph(to, S_TO), contact,
                    Paragraph(f"Исх. № {q['number']} от {q['date']}.", S_TO)]],
                  (236.0, WIDTH - 236.0),
                  [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 14.5),
                   ("RIGHTPADDING", (1, 0), (1, 0), 6.5)])

    below = _on_request_block(q)
    terms = ["Цена указана с учетом всех налогов, сборов, и других обязательных платежей."]
    if q["has_price_on_request"] and not below:
        terms.append(ON_REQUEST_LINE)
    terms.append(f"Условия оплаты: {_sentence(q['payment'])}")
    if q["delivery"]:
        terms.append(f"Срок поставки: {_sentence(q['delivery'])}")
    terms.append(f"Срок действия коммерческого предложения - до {q['valid_until']}.")
    draft = Table([[Paragraph(f"<b>{DRAFT_TITLE}</b><br/>{DRAFT_TEXT}", S_DRAFT)]], colWidths=[WIDTH], style=[
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_TINT), ("LINEBEFORE", (0, 0), (0, -1), 3, BRAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)])
    sign = _plain([Paragraph(f"{escape(config.KP_SIGNER_NAME)}<br/>{escape(config.KP_SIGNER_TITLE)}", S_SIGN),
                   HRFlowable(width="100%", thickness=0.5, color=colors.black, spaceBefore=0, spaceAfter=0),
                   Paragraph(escape(config.KP_SIGNER_SHORT), S_SIGN)],
                  (320.5, 84.0, WIDTH - 404.5),
                  [("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (0, 0), 26.5),
                   ("LEFTPADDING", (2, 0), (2, 0), 6), ("BOTTOMPADDING", (1, 0), (1, 0), 2.5)])

    story = [NextPageTemplate("next"), head, Spacer(1, 57.4), Paragraph("Уважаемый заказчик!", S_HELLO),
             Spacer(1, 11.8),
             Paragraph(f"{SELLER} предлагает Вашему вниманию коммерческое предложение на следующих условиях:",
                       S_INTRO),
             Spacer(1, 11.1), _items_table(q), Spacer(1, 6), *below,
             KeepTogether([*(Paragraph(escape(t), S_TERMS) for t in terms), Spacer(1, 22), draft, Spacer(1, 30),
                           sign])]

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"{q['number']} от {q['date']}", author=SELLER,
                          subject="Черновик коммерческого предложения", leftMargin=LEFT, rightMargin=PAGE_W - RIGHT)

    def frame(top: float) -> Frame:
        return Frame(LEFT, PAGE_H - BOTTOM, WIDTH, BOTTOM - top, leftPadding=0, rightPadding=0, topPadding=0,
                     bottomPadding=0)

    doc.addPageTemplates([PageTemplate("first", frames=[frame(TOP)], onPage=_letterhead),
                          PageTemplate("next", frames=[frame(TOP_NEXT)], onPage=_letterhead_next)])
    doc.build(story)
    return buf.getvalue()
