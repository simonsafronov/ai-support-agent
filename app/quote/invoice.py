"""Учебный счет по КП диалога: позиции, цены со скидкой, суммы и НДС берутся из КП.

Проект закрыт для покупателя. После выставления (автоматического или ручного) PDF доступен
покупателю только в своем чате. Документ не создает заказ и не содержит платежных реквизитов.
"""
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, NextPageTemplate, PageTemplate, Paragraph,
                                Spacer, Table)

from app import config, store
from app.quote import kp

DRAFT, ISSUED = "draft", "issued"
UNIT = "шт"
DUE_DAYS = kp.VALID_DAYS  # оплатить до: тот же срок, что срок действия цен в КП
REGISTRY_NOTE = "Позиции без НДС: программы из реестра отечественного ПО, пп. 26 п. 2 ст. 149 НК РФ."
DRAFT_HEAD = "Проект счета: ждет проверки менеджера."
DRAFT_BODY = "Подготовлен ИИ-консультантом Softline Store по черновику КП. Клиент получит счет после выставления."

COLS = (("n", "№", 22.0), ("code", "Артикул", 84.0), ("name", "Товары (работы, услуги)", 194.5),
        ("qty", "Кол-во", 40.0), ("unit", "Ед.", 28.0), ("price", "Цена, руб.", 81.0), ("sum", "Сумма, руб.", 82.0))
S_BANK = kp._style("bank", size=8.5, leading=11)
S_TITLE = kp._style("title", "Inter-Bold", 13, 17)
S_PARTY = kp._style("party", size=9, leading=12)
S_HEAD = kp._style("ih", "Inter-Bold", 8.5, 10.5, alignment=1)
S_CELL = kp._style("ic", size=8.5, leading=10.5, alignment=TA_LEFT)
S_TOTAL_L = kp._style("itl", "Inter-Bold", 9, 12, alignment=2)
S_TOTAL_N = kp._style("itn", "Inter-Bold", 9, 12, alignment=2)
S_TEXT = kp._style("it", size=9, leading=12.5)
S_WORDS = kp._style("iw", "Inter-Bold", 9, 12.5)


# --- Сумма прописью --------------------------------------------------------------------------------------------------
_ONES = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
         "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать",
         "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот")
_SCALES = ((("рубль", "рубля", "рублей"), False), (("тысяча", "тысячи", "тысяч"), True),
           (("миллион", "миллиона", "миллионов"), False), (("миллиард", "миллиарда", "миллиардов"), False))


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _triad(n: int, female: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if rest < 20:
        words.append(_ONES[rest])
    else:
        words += [_TENS[rest // 10], _ONES[rest % 10]]
    if female and words[-1] in ("один", "два"):
        words[-1] = {"один": "одна", "два": "две"}[words[-1]]
    return [w for w in words if w]


def amount_words(amount: Decimal) -> str:
    """266529.40 -> «Двести шестьдесят шесть тысяч пятьсот двадцать девять рублей 40 копеек»."""
    amount = kp._money(Decimal(amount))
    rub, kop = int(amount), int((amount - int(amount)) * 100)
    if rub == 0:
        words = ["ноль", "рублей"]
    else:
        words, n = [], rub
        for forms, female in _SCALES:
            n, part = divmod(n, 1000)
            if part:
                words = _triad(part, female) + [plural(part, forms)] + words
            elif forms[0] == "рубль":
                words = ["рублей"]
        if n:
            raise ValueError("сумма больше триллиона рублей")
    text = " ".join(words)
    return f"{text[:1].upper()}{text[1:]} {kop:02d} {plural(kop, ('копейка', 'копейки', 'копеек'))}"


# --- Данные ----------------------------------------------------------------------------------------------------------
def _error(text: str) -> dict:
    return {"ok": False, "error": text}


def number_for(quote_number: str) -> str:
    return "СЧ-" + quote_number.removeprefix("КП-")


def _lines(items: list[dict]) -> list[dict]:
    """Строки КП из хранилища в Decimal: цена со скидкой за штуку, сумма и НДС строки, как в документе КП."""
    out = []
    for it in items:
        price, total = kp._price(it.get("price")), kp._price(it.get("sum"))
        vat = it.get("vat")
        out.append({"offer_id": it.get("offer_id"), "name": kp._clean(it.get("name")), "qty": int(it.get("qty") or 0),
                    "vendor_code": kp._clean(it.get("vendor_code")), "price": price, "sum": total,
                    **({"available": it["available"]} if it.get("available") is not None else {}),
                    "vat": kp.NO_VAT if vat == kp.NO_VAT else kp._money(Decimal(str(vat))) if vat is not None else None,
                    "check": bool(it.get("price_to_confirm"))})
    return out


def prepare(session_id: str) -> dict:
    """Проект счета по последнему КП диалога. Не хватает реквизитов или цены: ok=False и что сделать."""
    quote = store.last_quote(session_id)
    if quote is None:
        return _error("В диалоге нет КП: сначала сформируйте его (make_quote), счет собирается по его позициям.")
    lines = _lines(json.loads(quote["items_json"] or "[]"))
    on_request = [ln["name"] for ln in lines if ln["price"] is None or ln["sum"] is None]
    if on_request:
        return _error(f"В КП есть позиции с ценой по запросу ({'; '.join(on_request)}): счет по ним выставит менеджер "
                      "после расчета. Передайте обращение менеджеру.")
    unsure = [ln["name"] for ln in lines if ln["check"]]
    if unsure:
        return _error(f"Цену позиции {'; '.join(unsure)} подтвердит менеджер: счет он выставит сам. Передайте "
                      "обращение менеджеру.")
    facts = store.get_session(session_id)["facts"]
    customer = kp._clean(facts.get("компания")) or (quote["customer"] if quote["customer"] != kp.NO_CUSTOMER else "")
    inn, kpp, missing = kp.requisites(customer, facts.get("ИНН", ""), facts.get("КПП", ""))
    if missing:
        return {"ok": False, "customer_missing": missing,
                "error": f"Для счета не хватает реквизитов покупателя: {', '.join(missing)}. Попросите их у клиента "
                         "одним сообщением и вызовите make_invoice еще раз с ними."}
    total, vat_total = kp._totals(lines)
    if not lines or total != Decimal(str(quote["total"])) or any(
            ln["qty"] <= 0 or ln["sum"] != ln["price"] * ln["qty"] for ln in lines):
        return _error("Суммы строк расходятся с КП. Обновите КП перед формированием счета.")
    data = {"quote_number": quote["number"], "customer": customer, "inn": inn, "kpp": kpp,
            "lines": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in ln.items() if k != "check"}
                      for ln in lines],
            "total": str(total), "vat_total": str(vat_total)}
    return {"ok": True, **_save(session_id, number_for(quote["number"]), data)}


def _save(session_id: str, number: str, data: dict) -> dict:
    """Проект счета диалога: тот же, если КП не менялся; обновленный, если менялся; новый номер, если прежний
    счет по этому КП уже выставлен, а позиции с тех пор изменились."""
    with kp._NUMBER_LOCK:
        cur = store.last_invoice(session_id)
        if cur and cur["data"] == data:
            return public(cur, unchanged=True)
        if cur and cur["status"] == DRAFT:
            inv_id, number = cur["id"], cur["number"]
            store.update_invoice(inv_id, data)
        else:
            base, n = number, 2
            while store.invoice_by_number(number):
                number, n = f"{base}-{n}", n + 1
            inv_id = store.add_invoice(session_id, number, data)
        inv = {**data, "number": number, "date": today(), "due": due(today()), "status": DRAFT}
        write(inv)
    return public(store.invoice(inv_id))


def public(inv: dict, **extra) -> dict:
    """Счет для инструмента агента, экрана оператора и Telegram: номер, суммы, позиции, статус."""
    d = inv["data"]
    return {"id": inv["id"], "number": inv["number"], "status": inv["status"], "quote_number": d["quote_number"],
            "customer": d["customer"], "total": float(d["total"]), "vat_total": float(d["vat_total"]),
            "items": [{"offer_id": ln["offer_id"], "name": ln["name"], "qty": ln["qty"], "price": float(ln["price"]),
                       "sum": float(ln["sum"]), **({"available": ln["available"]} if "available" in ln else {})}
                      for ln in d["lines"]],
            "url": f"/quotes/{inv['number']}.pdf", "issued_by": inv.get("issued_by") or "", **extra}


def issue(invoice_id: int, who: str) -> dict | None:
    """Выставляет учебный счет: файл получает дату выдачи и теряет плашку проекта.

    None, если счета нет; ok=False, если он уже выставлен (повтор нажатия второго счета клиенту не дает).
    """
    date = today()
    with kp._NUMBER_LOCK:
        inv = store.invoice(invoice_id)
        if inv is None:
            return None
        if inv["status"] != DRAFT:
            return {"ok": False, **public(inv)}
        write({**inv["data"], "number": inv["number"], "date": date, "status": ISSUED, "issued_by": who})
        if not store.issue_invoice(invoice_id, who):
            return {"ok": False, "status": store.invoice(invoice_id)["status"], **public(store.invoice(invoice_id))}
    return {"ok": True, "date": date, "due": due(date), **public(store.invoice(invoice_id))}


# --- PDF -------------------------------------------------------------------------------------------------------------
def _grid(data: list, widths: list[float], extra: list) -> Table:
    return Table(data, colWidths=widths, style=[
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), *extra])


def _items(inv: dict) -> Table:
    widths = [w for _, _, w in COLS]
    inner = {key: w - 8 for key, _, w in COLS}
    data = [[Paragraph(title, S_HEAD) for _, title, _ in COLS]]
    for n, ln in enumerate(inv["lines"], 1):
        data.append([kp._fit(str(n), inner["n"], kp.S_QTY), kp._wrap_code(ln["vendor_code"] or "—", inner["code"],
                                                                           kp.S_CODE),
                     Paragraph(kp._with_feed_status(ln["name"], ln), S_CELL),
                     kp._fit(str(ln["qty"]), inner["qty"], kp.S_QTY),
                     kp._fit(UNIT, inner["unit"], kp.S_QTY), kp._fit(kp._fmt(Decimal(ln["price"])), inner["price"],
                                                                     kp.S_NUM),
                     kp._fit(kp._fmt(Decimal(ln["sum"])), inner["sum"], kp.S_NUM)])
    return _grid(data, widths, [("BACKGROUND", (0, 0), (-1, 0), kp.BRAND_TINT)])


def _vat_line(inv: dict) -> tuple[str, str]:
    vats = [ln["vat"] for ln in inv["lines"]]
    if all(v == kp.NO_VAT for v in vats):
        return "Без налога (НДС):", "—"
    return f"В том числе НДС ({kp._percent(kp.VAT_RATE)}%):", kp._fmt(Decimal(inv["vat_total"]))


def _totals(inv: dict) -> Table:
    vat_label, vat_value = _vat_line(inv)
    total = kp._fmt(Decimal(inv["total"]))
    rows = [("Итого:", total), (vat_label, vat_value), ("Итого по КП:", total)]
    return Table([[Paragraph(a, S_TOTAL_L), Paragraph(b, S_TOTAL_N)] for a, b in rows],
                 colWidths=[kp.WIDTH - 110.0, 110.0],
                 style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)])


def render(inv: dict) -> bytes:
    """PDF счета. inv: данные из prepare плюс number, date, due и status; проект помечен плашкой."""
    party = lambda who, text: Paragraph(f"{who}: <b>{escape(text)}</b>", S_PARTY)
    buyer = f"{inv['customer']}, ИНН {inv['inn']}" + (f", КПП {inv['kpp']}" if inv["kpp"] else "")
    count = len(inv["lines"])
    lines = [f"Всего наименований {count}, на сумму {kp._fmt(Decimal(inv['total']))} руб."]
    terms = [f"Основание расчета: коммерческое предложение {inv['quote_number']}.",
             "Заказ не создан. Настоящий счет оформляется в интернет-магазине."]
    terms.append("Учебный документ, не для оплаты. Платежные реквизиты не указаны.")
    if any(ln.get("available") is False for ln in inv["lines"]):
        terms.append("По фиду: нет в наличии — см. отмеченные позиции. Фактическую поставку подтверждает менеджер. "
                     "Срок поставки не подтвержден.")
    if any(ln["vat"] == kp.NO_VAT for ln in inv["lines"]) and not all(ln["vat"] == kp.NO_VAT for ln in inv["lines"]):
        terms.append(REGISTRY_NOTE)
    story = [NextPageTemplate("next"), Spacer(1, 40),
             Paragraph(f"Демонстрационный счет № {escape(inv['number'])} от {inv['date']}", S_TITLE), Spacer(1, 4),
             Table([[""]], colWidths=[kp.WIDTH], rowHeights=[2],
                   style=[("LINEABOVE", (0, 0), (-1, 0), 1.5, colors.black)]), Spacer(1, 8),
             party("Покупатель", buyer), Spacer(1, 12),
             _items(inv), Spacer(1, 6), _totals(inv), Spacer(1, 8),
             KeepTogether([Paragraph(escape(lines[0]), S_TEXT), Paragraph(amount_words(Decimal(inv["total"])),
                                                                          S_WORDS), Spacer(1, 8),
                           *(Paragraph(escape(t), S_TEXT) for t in terms)])]
    if inv["status"] != ISSUED:
        story += [Spacer(1, 18), Table([[Paragraph(f"<b>{DRAFT_HEAD}</b><br/>{DRAFT_BODY}", kp.S_DRAFT)]],
                                       colWidths=[kp.WIDTH], style=[
            ("BACKGROUND", (0, 0), (-1, -1), kp.BRAND_TINT), ("LINEBEFORE", (0, 0), (0, -1), 3, kp.BRAND),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9)])]
    buf = io.BytesIO()
    title = f"Демонстрационный счет {inv['number']} от {inv['date']}"
    doc = BaseDocTemplate(buf, pagesize=A4, title=title, author="Команда хакатона", subject="Учебный образец",
                          leftMargin=kp.LEFT, rightMargin=kp.PAGE_W - kp.RIGHT)

    def frame(top: float) -> Frame:
        return Frame(kp.LEFT, kp.PAGE_H - kp.BOTTOM, kp.WIDTH, kp.BOTTOM - top, leftPadding=0, rightPadding=0,
                     topPadding=0, bottomPadding=0)

    def mark_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(kp.BRAND)
        canvas.setFont("Inter-Bold", 11)
        canvas.drawString(kp.LEFT, kp.PAGE_H - 35, "ДЕМО • НЕ ДЛЯ ОПЛАТЫ")
        canvas.setFillColor(kp.GREY)
        canvas.setFont("Inter", 8)
        canvas.drawRightString(kp.RIGHT, 36, f"Учебный образец  /  {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.addPageTemplates([PageTemplate("first", frames=[frame(kp.TOP)], onPage=mark_page),
                          PageTemplate("next", frames=[frame(kp.TOP_NEXT)], onPage=mark_page)])
    doc.build(story)
    return buf.getvalue()


def write(inv: dict) -> str:
    """Рисует счет и кладет файл рядом с КП. Возвращает путь."""
    config.QUOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = config.QUOTES_DIR / f"{inv['number']}.pdf"
    from tempfile import NamedTemporaryFile
    pdf = render(inv)
    with NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(pdf)
    from pathlib import Path
    Path(tmp.name).replace(path)
    return str(path)


def today() -> str:
    return datetime.now(kp.MSK).strftime("%d.%m.%Y")


def due(start: str) -> str:
    return (datetime.strptime(start, "%d.%m.%Y") + timedelta(days=DUE_DAYS)).strftime("%d.%m.%Y")
