"""Session-local selection for the compact engine. Only catalog results supply IDs."""
import re

from app import store
from app.agent import tools
from app.quote import delivery, kp

COUNT = r'(?P<count>[1-9]\d{0,5})\s*(?:шт(?:ук[аи]?)?\.?|лицензи[йияю]+|экземпляр(?:а|ов)?)'
QUANTITY = re.compile(rf'(?:нужно\s+|на\s+|количество\s*[:—-]?\s*)?{COUNT}', re.I)
SKU = r'[\w./-]+'
PICK = re.compile(rf'(?:(?P<verb>выбрать|выбираю|беру|возьму|добавьте|добавить)\s+)?(?:артикул\s+)?(?P<sku>{SKU})(?:\s*[,—:]?\s+{COUNT})?', re.I)
QUOTE = re.compile(r'(?:пожалуйста[, ]+)?(?:(?:подготовьте|сделайте|сформируйте|пришлите|оформите|нужно|хочу|прошу|запрашиваю)\s+)?(?:мне\s+)?(?:кп|коммерческое предложение)(?:\s+(?:по выбранн\w+ (?:позици\w+|вариант\w+)|на выбранн\w+ (?:позици\w+|вариант\w+)))?(?:,? пожалуйста)?', re.I)
COMPANY = re.compile(r'(?<!\w)(ООО|АО|ПАО|ИП)\s+[«"]([^«»"\n]{1,100})[»"]', re.I)
UNAVAILABLE = kp.FEED_UNAVAILABLE + '. Поставка требует подтверждения менеджера; срок не указан.'


def bind_search(items, offers, product_id=None):
    items.pop('consult', None)  # a new router search must not inherit the former product's constraints
    items['compact_product_id'] = product_id
    items.pop('compact_filter_qty', None)
    items['compact_offers'] = offers[:3]
    # A fresh search changes the subject. Never apply a previous quantity or draft to a new item.
    for key in ('compact_selected', 'compact_qty', 'compact_lines', 'compact_pending_quote'):
        items.pop(key, None)


def _line(offer, qty):
    return {'offer_id': offer['offer_id'], 'qty': qty}


def _accept(items, offer, qty):
    problem = kp.tier_problem(offer, qty)
    if problem:
        items.pop('compact_qty', None)
        return 'Количество вне диапазона выбранной позиции. Проверьте указанную в каталоге ступень и выберите подходящий артикул.'
    items['compact_qty'] = qty
    lines = items.setdefault('compact_lines', [])
    lines[:] = [line for line in lines if line['offer_id'] != offer['offer_id']] + [_line(offer, qty)]
    return None


def remember_total(items, trace):
    if not trace or trace[-1].get('name') != 'calc_total': return
    output = trace[-1]['output']
    lines = output.get('lines', [])
    if len(lines) != 1 or not lines[0].get('sum'): return
    line = lines[0]
    for offer in items.get('compact_offers', []):
        if offer['offer_id'] == line['offer_id']:
            items['compact_selected'] = offer
            items['compact_lines'] = [_line(offer, line['qty'])]
            items['compact_qty'] = line['qty']
            return


def _quote(items, tool):
    if not items.get('compact_selected') or not items.get('compact_qty') or not items.get('compact_lines'):
        items['compact_pending_quote'] = True
        return 'Для КП укажите ' + ('количество для выбранного артикула.' if items.get('compact_selected') else 'артикул из показанного списка и количество.')
    items.pop('compact_pending_quote', None)
    out = tool('make_quote', {'items':items['compact_lines']})
    if not out.get('ok'):
        return 'КП не сформировано: проверьте текущую позицию и допустимое количество.'
    reply = f"Черновик КП {out['number']} готов. PDF доступен в этом чате. Заказ не создан."
    if any(o.get('available') is False for o in out.get('items', [])) or kp.FEED_UNAVAILABLE in out.get('note',''):
        reply += ' ' + UNAVAILABLE
    return reply


def _invoice(ctx, tool):
    # delivery rechecks consent, session type, last quote and revision in the existing implementation.
    if not store.last_quote(ctx.session_id):
        return 'В этом диалоге пока нет КП. Для учебного счёта сначала нужно КП.'
    out = tool('make_invoice', {})
    if out.get('ok'): return 'Учебный PDF по последнему КП доступен в этом чате. Не для оплаты; заказ не создан.'
    if out.get('customer_missing'): return 'Для учебного PDF нужны реквизиты покупателя: ' + ', '.join(out['customer_missing']) + '.'
    return 'Учебный PDF не выдан. Проверьте, что это приватный чат и в последнем КП указаны цены.'


def handle(ctx, items, tool, render_catalog):
    """Return None when this is a read/router turn. No LLM can trigger a document write."""
    raw = ctx.user_request.strip().rstrip('.!').strip()
    company = COMPANY.search(raw)
    buyer_details = bool(company and not re.search(r'поставщик|продавец|контрагент|производитель', raw, re.I)
                         and (company.start()==0 or re.search(r'(?:покупатель|заказчик|наша компания)\s*[:—-]?\s*$', raw[:company.start()], re.I)))
    if buyer_details:
        tools.save_facts(ctx, {'company':company.group()})
        store.update_session(ctx.session_id, facts=ctx.facts)
    if delivery.requested(ctx.session_id):
        # The normal loop persists new requisites at turn end, but invoice.prepare reads them now.
        store.update_session(ctx.session_id, facts=ctx.facts)
        return _invoice(ctx, tool)
    if QUOTE.fullmatch(raw): return _quote(items, tool)
    if buyer_details: return 'Реквизиты покупателя сохранены в этом диалоге.'
    offers = items.get('compact_offers', [])
    qty_only = QUANTITY.fullmatch(raw)
    selected = items.get('compact_selected')
    if qty_only and not selected and items.get('compact_product_id'):
        qty = int(qty_only['count'])
        out = tool('get_product', {'product_id':items['compact_product_id'], 'quantity':qty})
        items['compact_offers'] = out.get('offers', [])[:3]
        items['compact_filter_qty'] = qty
        if not items['compact_offers']: return 'В этом продукте по указанному количеству вариантов не найдено.'
        return render_catalog(dict(products=[out['product']], total=1, match='all', first_product=out)) + f' Количество {qty} сохранено; выберите артикул.'
    if qty_only and selected:
        items.pop('consult', None)  # an explicit order quantity supersedes inferred role counts
        qty = int(qty_only['count'])
    else:
        pick = PICK.fullmatch(raw)
        if not pick: return None
        candidates = [o for o in offers if o.get('vendor_code') and o['vendor_code'].casefold()==pick['sku'].casefold()]
        if len(candidates)!=1:
            if pick['verb']:
                items.pop('consult', None)
                for key in ('compact_selected','compact_qty','compact_lines','compact_pending_quote'): items.pop(key,None)
                return 'Не могу однозначно выбрать этот артикул среди показанных. Уточните позицию.'
            return None
        selected = candidates[0]
        items.pop('consult', None)  # an explicit variant replaces the provisional recommendation
        if (pick['verb'] or '').lower() not in ('добавьте','добавить'):
            items['compact_lines'] = []
        items['compact_selected'] = selected
        items.pop('compact_qty',None)
        count = pick['count'] or items.get('compact_filter_qty')
        if not count: return f"Выбран артикул {selected['vendor_code']}. Сколько штук нужно?"
        qty = int(count)
    issue = _accept(items, selected, qty)
    if issue: return issue
    if items.get('compact_pending_quote'): return _quote(items,tool)
    return f"Выбрано: {selected['vendor_code']}, {qty} шт." + (' '+UNAVAILABLE if selected.get('available') is False else '')
