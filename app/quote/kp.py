"""Черновик коммерческого предложения (КП) в PDF по шаблону Softline.

Названия, артикулы и цены берутся только из каталога по offer_id, вся арифметика
считается здесь через Decimal. Модель передает лишь offer_id и количество.
Размеры и координаты в пунктах сняты с шаблона заказчика data/raw/kp_template.pdf.
"""
from __future__ import annotations

import io
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
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, KeepTogether, NextPageTemplate, PageTemplate,
                                Paragraph, Spacer, Table)
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
QUOTE_NOTE = "Черновик КП сформирован, его проверит менеджер."

SELLER = "ПАО «Софтлайн»"
ADDRESS = ("119021, город Москва, ул Льва Толстого, д.",
           "5 стр. 1, этаж 3, помещ. 1, ком. №2, 2а (а-311)",
           "Т +7 495 232 00 23 | info@softline.com",
           "www.softline.ru | www.softline.com")
SIGNER_NAME, SIGNER_TITLE, SIGNER_SHORT = "Бондаренко Александр", "Директор департамента", "Бондаренко А.С."
DRAFT_TITLE = "Черновик коммерческого предложения."
DRAFT_TEXT = "Подготовлен ИИ-консультантом Softline Store, требует проверки менеджером."
VAT_FOOTNOTE = "НДС рассчитан автоматически, проверяет менеджер."
ON_REQUEST_LINE = "Позиции с ценой по запросу рассчитает менеджер."
UNAVAILABLE_LINE = "Наличие позиций, которых сейчас нет в продаже на сайте, подтвердит менеджер."

MSK = timezone(timedelta(hours=3))  # дата КП по московскому времени
_NUMBER_LOCK = threading.Lock()
CENT = Decimal("0.01")
NBSP = " "

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
COLS = (90.0, 186.0, 35.5, 72.0, 76.0, 72.0)  # артикул, наименование, кол-во, цена, сумма, НДС
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
S_FOOTNOTE = _style("footnote", size=7, alignment=TA_RIGHT, textColor=GREY)
S_TERMS = _style("terms", leading=12.5, leftIndent=43.1)
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
                          "product_id FROM offers WHERE id=?", (offer_id,)).fetchone()
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


def _fmt(value: Decimal) -> str:
    """1054.17 -> «1 054,17»."""
    return f"{value:,.2f}".replace(",", NBSP).replace(".", ",")


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _sentence(text: str) -> str:
    return text.rstrip(" .") + "."


def _error(text: str) -> dict:
    return {"ok": False, "error": text}


NO_CUSTOMER = "Заказчик уточняется"  # клиент не назвал организацию: черновик все равно нужен менеджеру


def make_quote(session_id: str, customer: str, items: list[dict], inn: str = "", payment: str = "",
               contact: str = "", delivery: str = "") -> dict:
    """Собирает черновик КП: проверяет позиции, считает суммы и НДС, пишет PDF и запись в хранилище."""
    customer = _clean(customer) or NO_CUSTOMER
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
        price = _price(offer.get("price"))
        amount = _money(price * qty) if price is not None else None
        if _clean(offer.get("registry_no")):
            vat = NO_VAT
        else:
            vat = _money(amount * VAT_RATE / (100 + VAT_RATE)) if amount is not None else None
        lines.append({"offer_id": offer_id, "name": _clean(offer.get("name")).strip(" ,;"),
                      "vendor_code": _clean(offer.get("vendor_code")), "qty": qty, "price": price, "sum": amount,
                      "vat": vat, "available": bool(offer.get("available"))})
    if problems:
        return _error("КП не сформировано, исправьте и повторите: " + "; ".join(problems) + ".")

    total = sum((ln["sum"] for ln in lines if ln["sum"] is not None), Decimal(0))
    vat_total = sum((ln["vat"] for ln in lines if isinstance(ln["vat"], Decimal)), Decimal(0))
    on_request = any(ln["price"] is None for ln in lines)
    today = datetime.now(MSK).date()
    quote = {
        "customer": customer, "inn": _clean(inn), "contact": _clean(contact),
        "payment": _clean(payment) or DEFAULT_PAYMENT, "delivery": _clean(delivery) or DEFAULT_DELIVERY,
        "date": today.strftime("%d.%m.%Y"), "valid_until": (today + timedelta(days=VALID_DAYS)).strftime("%d.%m.%Y"),
        "lines": lines, "total": total, "vat_total": vat_total, "has_price_on_request": on_request,
        "has_unavailable": any(not ln["available"] and ln["price"] is not None for ln in lines),
    }
    out_items = [{**ln, "price": _num(ln["price"]), "sum": _num(ln["sum"]),
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

    return {"ok": True, "number": number, "updated": bool(prev), "contact_in_quote": bool(quote["contact"]),
            "url": f"/quotes/{number}.pdf", "file": str(path), "date": quote["date"],
            "valid_until": quote["valid_until"], "customer": customer, "items": out_items, "total": float(total),
            "vat_total": float(vat_total), "has_price_on_request": on_request,
            "needs_availability_check": [ln["name"] for ln in lines if not ln["available"] and ln["price"] is not None],
            "note": QUOTE_NOTE + (" Наименование заказчика не указано: менеджер уточнит его перед отправкой."
                                  if customer == NO_CUSTOMER else "")}


# --- PDF -------------------------------------------------------------------------------------------------------------
def _fit(text: str, width: float, style: ParagraphStyle) -> Paragraph:
    """Значение в ячейку одной строкой: если не помещается по ширине, шрифт уменьшается."""
    text = text.replace(" ", NBSP)
    need = pdfmetrics.stringWidth(text, style.fontName, style.fontSize)
    if need > width:
        size = style.fontSize * width / need * 0.99
        style = ParagraphStyle(style.name + "-fit", parent=style, fontSize=size, leading=size * 1.25)
    return Paragraph(escape(text), style)


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


def _items_table(q: dict) -> Table:
    inner = [w - 2 * PAD for w in COLS]
    data = [[Paragraph(t, S_HEAD) for t in ("Артикул", "Наименование", "Кол-<br/>во", "Цена, руб.", "Сумма, руб.",
                                             "В т.ч. НДС,<br/>руб.")]]
    for ln in q["lines"]:
        priced = ln["price"] is not None
        vat = _fmt(ln["vat"]) if isinstance(ln["vat"], Decimal) else (ln["vat"] or "—")
        data.append([_wrap_code(ln["vendor_code"], inner[0], S_CODE), Paragraph(escape(ln["name"]), S_NAME),
                     _fit(str(ln["qty"]), inner[2], S_QTY),
                     _fit(_fmt(ln["price"]) if priced else ON_REQUEST, inner[3], S_NUM),
                     _fit(_fmt(ln["sum"]) if priced else ON_REQUEST, inner[4], S_NUM),
                     _fit(vat, inner[5], S_NUM)])
    vats = [ln["vat"] for ln in q["lines"]]
    if any(isinstance(v, Decimal) for v in vats):
        vat_total = _fmt(q["vat_total"])
    else:
        vat_total = NO_VAT if all(v == NO_VAT for v in vats) else "—"
    has_sum = any(ln["sum"] is not None for ln in q["lines"])
    data.append(["", Paragraph("Итого", S_TOTAL), "", "",
                 _fit(_fmt(q["total"]) if has_sum else ON_REQUEST, inner[4], S_NUM_BOLD),
                 _fit(vat_total, inner[5], S_NUM_BOLD)])
    data.append([Paragraph(VAT_FOOTNOTE, S_FOOTNOTE), "", "", "", "", ""])
    return Table(data, colWidths=COLS, repeatRows=1, style=[
        ("GRID", (0, 0), (-1, -2), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), PAD), ("RIGHTPADDING", (0, 0), (-1, -1), PAD),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, 0), 3), ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, -2), (-1, -2), 7), ("BOTTOMPADDING", (0, -2), (-1, -2), 7),
        ("SPAN", (0, -1), (-1, -1)),
        ("LEFTPADDING", (0, -1), (-1, -1), 0), ("RIGHTPADDING", (0, -1), (-1, -1), 0),
        ("TOPPADDING", (0, -1), (-1, -1), 3), ("BOTTOMPADDING", (0, -1), (-1, -1), 0),
        ("NOSPLIT", (0, -3), (-1, -1)),  # последняя позиция, итог и сноска не отрываются друг от друга
    ])


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
    to = f"Куда: {escape(q['customer'])}" + (f", ИНН {escape(q['inn'])}" if q["inn"] else "")
    phone_glued = re.sub(r"(?<=[\d()+-]) (?=[\d(])", NBSP, q["contact"])  # номер телефона не рвется на строки
    contact = Paragraph(f"Контакт заказчика: {escape(phone_glued)}", S_TO) if q["contact"] else Spacer(1, 12)
    head = _plain([[Spacer(1, 42.3), Paragraph("<br/>".join(ADDRESS), S_ADDRESS)],
                   [Spacer(1, 15.6), Paragraph(to, S_TO), contact,
                    Paragraph(f"Исх. № {q['number']} от {q['date']}.", S_TO)]],
                  (236.0, WIDTH - 236.0),
                  [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 14.5),
                   ("RIGHTPADDING", (1, 0), (1, 0), 6.5)])

    terms = ["Цена указана с учетом всех налогов, сборов, и других обязательных платежей."]
    if q["has_price_on_request"]:
        terms.append(ON_REQUEST_LINE)
    if q.get("has_unavailable"):
        terms.append(UNAVAILABLE_LINE)
    terms += [f"Условия оплаты: {_sentence(q['payment'])}", f"Срок поставки: {_sentence(q['delivery'])}",
              f"Срок действия коммерческого предложения - до {q['valid_until']}."]
    draft = Table([[Paragraph(f"<b>{DRAFT_TITLE}</b><br/>{DRAFT_TEXT}", S_DRAFT)]], colWidths=[WIDTH], style=[
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_TINT), ("LINEBEFORE", (0, 0), (0, -1), 3, BRAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)])
    sign = _plain([Paragraph(f"{SIGNER_NAME}<br/>{SIGNER_TITLE}", S_SIGN),
                   HRFlowable(width="100%", thickness=0.5, color=colors.black, spaceBefore=0, spaceAfter=0),
                   Paragraph(SIGNER_SHORT, S_SIGN)],
                  (320.5, 84.0, WIDTH - 404.5),
                  [("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (0, 0), 26.5),
                   ("LEFTPADDING", (2, 0), (2, 0), 6), ("BOTTOMPADDING", (1, 0), (1, 0), 2.5)])

    story = [NextPageTemplate("next"), head, Spacer(1, 57.4), Paragraph("Уважаемый заказчик!", S_HELLO),
             Spacer(1, 11.8),
             Paragraph(f"{SELLER} предлагает Вашему вниманию коммерческое предложение на следующих условиях:",
                       S_INTRO),
             Spacer(1, 11.1), _items_table(q), Spacer(1, 6),
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
