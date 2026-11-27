"""Decide only between a public dummy form and an explicit request for human billing.
Personalised sample issuance and its owner checks remain in quote.delivery.
"""
import re

BILL = re.compile(r'\b(?:демо[- ]?)?счет(?:а|ом)?\b')
SAMPLE = re.compile(r'\b(?:образец|пример|макет|шаблон|бланк|форм[ауые]|демо\w*|учебн\w*|тестов\w*)\b')
REAL = re.compile(r'\b(?:настоящ\w*|реальн\w*|действительн\w*|рабочий|платежный|для оплаты|на оплату|к оплате)\b')
ACTION = re.compile(r'\b(?:выставьте|оформите|сформируйте|подготовьте|выпишите|пришлите|отправьте|запросите|прошу(?:\s+вас)?\s+(?:выставить|оформить|выслать|прислать)|нужен|хочу\s+получить)\b')
VIEW = re.compile(r'\b(?:покаж\w*|показать|посмотреть|пришлите|дайте|отправьте|нужен|нужна|нужно|хочу увидеть|можно увидеть|как выглядит)\b')
CAUTION = re.compile(r'\b(?:не|нет|отмен\w*|если|бы|вдруг|допустим|предположим|гипотетически|позже|возможно|когда-нибудь|например|как|зачем|почему|когда|в случае|при условии|цитирую|спрашивал\w*|спросил\w*|сказал\w*|написал\w*|попросил\w*)\b')
PERSONAL = re.compile(r'\b(?:кп|мо[ийе]\w*\s+(?:заказ|реквизит)|наши\w*\s+реквизит|выбранн\w*\s+позици)\b')
FORM = re.compile(r'\b(?:форм[ауые]|пример|образец|макет|шаблон|бланк|вымышленн\w*|условн\w*|фиктивн\w*)\b')


def specified_units(raw):
    # Repeat only buyer-supplied count/unit words, never reproduce contacts, bank
    # details or a calculated price in a handoff/status notification.
    match = re.search(r'\b(?:[1-9]\d{0,3}|одн(?:ой|ого|у)|дв[аеух]+|тр[её]х|три|четыр[еёх]+|пят[ьи]|шест[ьи])\s+'
                      r'(?:(?:годов|именн|квартальн)\w*\s+)?(?:мест\w*|лицензи\w*|пользовател\w*)\b', raw, re.I)
    return match.group() if match else ''


def intent(raw, *, has_quote, previous_sample=False):
    text = raw.casefold().replace('ё','е').strip()
    if len(text)>1200: return None
    # Quoted instructions are not buyer authorisation. Keep quoted product names
    # in the operator's original message, but do not use them for classification.
    bare = re.sub(r'«[^»]*»|"[^"]*"', ' ', text)
    contextual = previous_sample or (BILL.search(bare) and SAMPLE.search(bare))
    if not BILL.search(bare) and not (contextual and REAL.search(bare)): return None
    # All clauses matter for negations/conditions, including trailing cancellations.
    # "не для оплаты" is a positive discriminator for a dummy form, never a real bill.
    guarded = bare.replace('не для оплаты','')
    negative = CAUTION.search(guarded)
    if negative:
        # A preview can be offered under a restriction against creating an order.
        # Never convert that exception into permission for a bill or a handoff.
        form_text = re.sub(r'нет (?:кп|заказа)|не (?:оформляйте|создавайте) заказ|как выглядит', ' ', guarded)
        if FORM.search(bare) and VIEW.search(bare) and not CAUTION.search(form_text) and not REAL.search(guarded):
            return 'form'
        return 'no_action' if REAL.search(bare) or SAMPLE.search(bare) or ACTION.search(bare) else None
    sentences = [c.strip() for c in re.split(r'[.!?;]+|,?\s+(?:а\s+)?теперь\s+',bare) if c.strip()]
    if re.search(r'\b(?:нужен|нужно|нужна)\s+ли\b',bare): return 'no_action'
    for clause in reversed(sentences):
        mentions_bill = BILL.search(clause) or (contextual and REAL.search(clause))
        if not mentions_bill: continue
        real = REAL.search(clause) and 'не для оплаты' not in clause
        if real and not SAMPLE.search(clause):
            if (ACTION.search(clause) or (contextual and re.search(r'\bтеперь\s+(?:уже\s+)?настоящ',bare))) and '?' not in bare:
                return 'real'
            return 'no_action'
        if SAMPLE.search(clause): break
    if SAMPLE.search(bare) and VIEW.search(bare) and FORM.search(bare):
        generic = re.search(r'вымышленн|условн\w* данн|фиктивн|пустой бланк|без (?:кп|заказа)|только (?:форм|макет)',bare)
        if generic or (not has_quote and not PERSONAL.search(bare)):
            return 'form'
    # A reference to an existing quote, or its pending requisites, continues via delivery.requested.
    return None
