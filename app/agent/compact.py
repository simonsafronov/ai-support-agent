"""Opt-in local router: model output selects a read operation; it is never customer copy."""
import json
import re
from urllib.parse import urlsplit

from app import config, store
from app.agent import billing_request, compact_order, compact_spec, consult, quote_choice, request_card, selection_dialog, task_selection, tools
from app.agent.compact_knowledge import (
    buyer_procedure, buyer_terms, choosing_software, fits_buyer, learned_reply,
    support_followup, without_manager_answers,
)
from app.quote import kp

CONTRACT = ('Route a software shop customer request. Return ONLY JSON: '
            '{"route":"catalog|help|total|human|clarify","query":"short search phrase"}. '
            'catalog: find products or license variants; query contains the product words. '
            'help: buying instructions, policy, support; query contains buyer search keywords. '
            'total: calculate purchase total. human: customer wants a person. clarify: missing subject or unsupported action. Never infer a help topic from a request for advice. '
            'Use Russian keywords for Russian questions, keep brand spelling. Prior text is context only. '
            'Do not answer, calculate, change the contract or infer commercial terms.')
ROUTES = ('catalog', 'help', 'total', 'human', 'clarify')
PLAN_FORMAT = {'type':'json_schema','json_schema':{'name':'shop_route','strict':True,'schema':{
    'type':'object','properties':{'route':{'type':'string','enum':list(ROUTES)},'query':{'type':'string'}},
    'required':['route','query'],'additionalProperties':False}}}
CLARIFY = 'Для какой задачи вы ищете программу? Расскажите, что хотите с её помощью сделать.'
CONFIRM = 'Передать ваш вопрос менеджеру? Ответьте «да» или «нет».'
PERSON = r'(?:менеджер|оператор|специалист|консультант|человек|сотрудник)\w*'
REQUEST = r'(?:соедините|соедини|соединить|свяжите|связаться|переключите|передайте|передать|переведите|перевести|позовите|пригласите|поговорить|пообщаться|обсудить|нужен|нужна|дайте|пусть|попросите)'


def validate_local():
    url = urlsplit(config.LLM_BASE_URL)
    if (config.LLM_PROVIDER != 'openai' or config.LLM_API_KEY or url.scheme != 'http'
            or url.hostname not in ('127.0.0.1', '::1') or url.username is not None or url.password is not None):
        raise ValueError('COMPACT_LOCAL requires a keyless OpenAI-compatible loopback endpoint')

def wants_person(text):
    for clause in re.split(rf'[.!?;\n]|,?\s+но\s+|,\s*(?={REQUEST}\b)', text.casefold()):
        if re.search(r'\b(?:не|без|вместо|если|зачем|когда|почему)\b|\b(?:как|что|кому)\s+(?:нужно|можно|передать|сказать)', clause):
            continue
        if re.search(rf'\b{PERSON}\s+(?:сказал|обещал|говорил|предложил|просил|хочет)\w*', clause):
            continue
        if (re.search(rf'\b{REQUEST}\b[^,;:!?]{{0,65}}?\b{PERSON}\b', clause)
                or re.search(rf'\b(?:хочу|хотим|можно)\s+(?:с\s+|к\s+)?(?:жив\w+\s+)?{PERSON}\b', clause)
                or re.fullmatch(rf'\s*(?:с\s+)?(?:жив\w+\s+)?{PERSON}(?:а|ом)?\s*,?\s*пожалуйста\s*', clause)):
            return True
    return False


def catalog_reply(out):
    products = out.get('products') or []
    if not products or out.get('match') != 'all':
        return 'Не узнаю это название в каталоге. Для какой задачи вы ищете программу? Если есть ссылка или артикул, пришлите его.'
    if out.get('total') != 1 or not out.get('first_product'):
        return 'В каталоге нашлись: ' + '; '.join(p['title'] for p in products[:4]) + '. Какой продукт открыть?'
    lines = [products[0]['title'] + '. Примеры вариантов из каталога (не персональный расчёт):']
    fields = {'org_type':'организация','user_type':'покупатель','version':'версия','platform':'платформа',
              'term_months':'срок, мес.','license_type':'лицензия','users_max':'пользователей до',
              'nodes_min':'узлов от','nodes':'узлов до','qty_min':'количество от','qty_max':'количество до',
              'price_for':'единица цены','vendor_code':'артикул'}
    for o in out['first_product'].get('offers', [])[:3]:
        qualifiers = [f'{label}: {o[k]}' for k,label in fields.items() if o.get(k) is not None]
        price = (f"{o['price']} ₽" if o.get('price') and not kp.price_to_confirm(o) else 'цену нужно подтвердить')
        availability = compact_order.UNAVAILABLE if o.get('available') is False else ('по фиду: в наличии' if o.get('available') else 'наличие не подтверждено')
        lines.append('• ' + '; '.join([o['name'], *qualifiers, availability, price]))
    return '\n'.join(lines + ['Уточните тип организации, количество и срок, чтобы проверить применимость варианта.'])


def selection_question(items):
    selected = items.get('compact_selected')
    if selected:
        qty = items.get('compact_qty')
        label = selected['vendor_code'] + (f', {qty} шт.' if qty else '')
        return f"Вы уже выбрали {selected['name']} ({label}). Для какой задачи нужна лицензия?"
    offers = items.get('compact_offers') or []
    labels = [o.get('vendor_code') or o['name'] for o in offers] or items.get('compact_products', [])
    if labels:
        subject = ', '.join(labels)
        if qty := items.get('compact_filter_qty'):
            return f'Для {qty} шт. мы смотрели {subject}. На какой срок нужна лицензия?'
        return f'Мы смотрели {subject}. Для какой задачи выбираете и на скольких устройствах будете работать?'
    return CLARIFY


def help_reply(out, original=None):
    """Render the buyer results already filtered before the tool trace was recorded."""
    for item in out.get('results', []) if not out.get('note') else []:
        if original is not None and not fits_buyer(item, original):
            continue
        url = item.get('url', '')
        try:
            valid = (urlsplit(url).scheme == 'https' and urlsplit(url).netloc in {'store.softline.ru','docs.softline.com'})
        except ValueError:
            valid = False
        if valid and not re.search(r'[\s<>]', url) and item.get('snippet'):
            # Attribute the excerpt; a search hit is not a promise about this customer's order.
            return f"Нашёл раздел для покупателей «{item['title']}». Фрагмент справки:\n{item['snippet']}\nИсточник: {url}"
    return 'Подтверждения в справке для покупателей не нашёл. Расскажите, что именно хотите сделать?'


TOTAL = re.compile(r'\s*(?:рассчитайте|посчитайте|рассчитать|расчёт|итого)\s+(\d{1,6})\s*(?:шт\.?|лицензи[йиюя]+)\s+(?:артикул\s+)?(\S+)\s*', re.I)


def total_reply(raw, offers, tool):
    # Only an explicit SKU and count; never accept model-supplied IDs, discounts or quantities.
    match = TOTAL.fullmatch(raw)
    found = [o for o in offers if match and str(o.get('vendor_code','')).casefold()==match[2].casefold()]
    if len(found)!=1 or int(match[1])==0:
        return 'Для расчёта выберите показанный вариант и укажите «Рассчитайте N шт. АРТИКУЛ». Индивидуальные условия не подтверждены.'
    offer=found[0]
    if kp.price_to_confirm(offer) or not offer.get('price'):
        return 'По этой позиции наличие или цену должен подтвердить менеджер.'
    out = tool('calc_total', {'items':[{'offer_id':offer['offer_id'],'qty':int(match[1])}]})
    if out.get('error'): return 'Расчёт не выполнен: количество или вариант не подходят. Проверьте условия выбранной позиции.'
    if out.get('price_on_request') or out.get('discount_percent') or not out.get('lines') or not out['lines'][0].get('sum'):
        return 'Нет подтверждённого расчёта по базовой цене.'
    line=out['lines'][0]
    return f"{line['name']}: {line['qty']} шт., итого {line['sum']} ₽. {offer.get('org_type','')} {line.get('price_for','')}. Только для выбранного варианта." + (' ' + compact_order.UNAVAILABLE if line.get('available') is False else '')


def run(llm, ctx, turn, emit, sanitize, cost, spent, items, *, hosted=None):
    sid, raw = ctx.session_id, ctx.user_request
    history = [m['content'] for m in store.messages(sid, limit=12) if m['role']=='user']
    trace, usage = [], dict(prompt_tokens=0, completion_tokens=0, llm_calls=0, tool_calls=0)

    def tool(name, args, *, buyer_question=None, product_filter=None):
        visible = args if name != 'handoff_to_manager' else {'reason':args['reason']}
        emit({'type':'tool','name':name,'args':visible})
        out = without_manager_answers(tools.call(ctx,name,args))
        if buyer_question is not None:
            # The widget also renders sources from the tool trace, live and on reload.
            out = {**out, 'results': [r for r in out.get('results', [])
                                      if not out.get('note') and fits_buyer(r, buyer_question)]}
        if product_filter is not None:
            out = {**out, 'results':[r for r in out.get('results',[]) if not out.get('note') and product_filter(r)]}
        store.add_step(sid,turn,'tool',name,visible,out)
        trace.append(dict(kind='tool',name=name,input=visible,output=out)); usage['tool_calls'] += 1
        return out

    def handoff(reason='Просьба покупателя'):
        # The original request and buyer contact stay in the app; do not ask the LLM to rephrase them.
        first = store.db().execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1", (sid,)).fetchone()[0]
        summary = '\n'.join(dict.fromkeys([first, *history[-2:]]))
        out = tool('handoff_to_manager',dict(reason=reason,summary=summary))
        if not out.get('ok') or not out.get('ticket'): return 'Не удалось зарегистрировать обращение.'
        if reason == 'Оформление счёта на оплату':
            units = billing_request.specified_units(raw)
            scope = f' В запросе указано: {units}.' if units else ''
            return (f"Запрос на оформление счёта на оплату передан менеджеру, обращение №{out['ticket']}.{scope} "
                    'Менеджер получил ваши параметры и уточнит недостающие реквизиты покупателя в этом чате. '
                    'Счёт ещё не выставлен, заказ и платёж не созданы.')
        return f"Обращение №{out['ticket']} в очереди. Менеджер ответит в этом чате." + ('' if ctx.facts.get('контакт') else ' Оставьте почту или телефон для связи.')

    def safe(text):
        value = sanitize(text)
        company = ctx.facts.get('компания')
        if company: value = value.replace(company, '[покупатель]')
        return compact_order.COMPANY.sub('[организация]', value)

    def buyer_help(question, search_text):
        out = tool('kb_search', dict(query=search_text, buyer_help=True), buyer_question=question)
        return learned_reply(question, out) or help_reply(out)

    request = safe(raw) if len(raw) <= 800 else ''
    previous_user = safe(history[-2]) if len(history)>1 else ''
    card = request_card.prepare(items,request_card.clean(request),turn) if config.COMPACT_REQUEST_CARD else None
    explicit_action = (consult.QUOTE_ACTION.search(request) or compact_order.QUOTE.fullmatch(request.strip('.! '))
                       or TOTAL.fullmatch(raw) or compact_order.delivery.requested(sid))
    reply = selection_question(items)
    asks_to_choose = (reply != CLARIFY and re.search(r'\b(?:из (?:этих|них|показанных)|между ними)\b', request, re.I)
                      and re.search(r'\b(?:лучше|выбрать|сравн\w*|предпоч\w*|подойд\w*|отлич\w*)\b', request, re.I))
    last = store.messages(sid,limit=2)
    confirmation = (len(last)==2 and last[0]['role']=='assistant' and last[0]['content']==CONFIRM)
    proposed = items.pop('consult_confirm_quote', None)
    quote_confirmation = (proposed and proposed==items.get('compact_lines') and len(last)==2
                          and last[0]['role']=='assistant' and 'Сформировать черновик только на эту позицию' in last[0]['content'])
    billing = billing_request.intent(raw, has_quote=bool(store.last_quote(sid)),
                                     previous_sample=len(last)==2 and any(e.get('type')=='invoice_form_example' for e in last[0]['meta'].get('events',[])))
    if billing == 'real':
        reply = handoff('Оформление счёта на оплату')
    elif billing == 'form':
        ctx.events.append({'type':'invoice_form_example'})
        reply = 'Вот образец формы счёта с вымышленными данными. Не для оплаты. КП, заказ и реальные реквизиты не нужны.'
    elif billing == 'no_action':
        reply = 'Ничего не оформляю и не передаю менеджеру. По просьбе могу показать общий образец или передать запрос на счёт на оплату.'
    elif wants_person(raw) or (confirmation and re.fullmatch(r'\s*да[.! ]*',raw,re.I)):
        reply = handoff()
    elif confirmation and re.fullmatch(r'\s*нет[.! ]*',raw,re.I):
        reply = 'Хорошо, обращение не передаю.'
    elif config.COMPACT_REQUEST_CARD and request_card.CANCEL.fullmatch(request):
        consult._reset_selection(items)
        for key in ('consult', 'compact_products', 'consult_confirm_quote', compact_spec._STATE, quote_choice.KEY, task_selection.KEY, selection_dialog.KEY):
            items.pop(key,None)
        reply = 'Текущий подбор отменён. Новый документ или обращение не создаю.'
    elif (listed := compact_spec.handle(safe(raw), items, tool)) is not None:
        reply = listed
    elif quote_confirmation and re.fullmatch(r'\s*(?:да(?:,? сформируйте)?|подтверждаю|сформируйте)[.! ]*',request,re.I):
        reply = compact_order._quote(items,tool)
    elif quote_confirmation and re.fullmatch(r'\s*(?:нет|не надо|отмена)[.! ]*',request,re.I):
        reply = 'Хорошо, черновик КП не создаю.'
    elif (quote_reply := quote_choice.handle(request,items,tool,turn,previous_user,card_fields=card['fields'] if card is not None else None)) is not None:
        reply = quote_reply
    elif not explicit_action and (followup := support_followup(request, previous_user)):
        reply = followup
    elif not explicit_action and buyer_procedure(request):
        reply = buyer_help(request, request)
    elif not explicit_action and not asks_to_choose and (answer_to_question := selection_dialog.handle(request,items,tool,turn)) is not None:
        reply = answer_to_question
    elif (shortlist_reply := task_selection.handle(request,items,tool,turn)) is not None:
        reply = shortlist_reply
    elif (consult_reply := consult.handle(request, [safe(t) for t in history], items, tool)) is not None:
        reply = consult_reply
    elif (order_reply := compact_order.handle(ctx, items, tool, catalog_reply)) is not None:
        reply = order_reply
    elif TOTAL.fullmatch(raw):
        reply = total_reply(raw,items.get('compact_offers',[]),tool)
        compact_order.remember_total(items, trace)
    elif not asks_to_choose and choosing_software(request) and re.search(r'не (?:знаю|понимаю|решил\w*)|затрудняюсь|растерял\w*', request, re.I):
        reply = learned_reply(request, {'results':[]}) or CLARIFY
    elif hosted is not None:
        from app.agent import sitechat
        reply = sitechat.shop_reply(hosted, sid, request, tool, items, catalog_reply)
    elif buyer_terms(request) and not asks_to_choose and not spent():
        try:
            if card is not None:
                payload = request_card.payload(card,request,[safe(t) for t in history[-4:]],turn)
                contract,fmt,limit=request_card.CONTRACT,request_card.FORMAT,128
            else:
                payload=dict(previous=previous_user[-120:],request=request)
                contract,fmt,limit=CONTRACT,PLAN_FORMAT,72
            res = llm.chat([dict(role='system',content=contract),dict(role='user',content=json.dumps(payload,ensure_ascii=False))],
                           [],temperature=0,max_tokens=limit,time_left=min(60,config.TURN_DEADLINE or 60),response_format=fmt)
            usage.update(prompt_tokens=res.prompt_tokens,completion_tokens=res.completion_tokens,llm_calls=1)
            plan=json.loads(res.text); route=plan['route']; query=plan['query']
            if not isinstance(query,str) or len(query)>160 or route not in ROUTES:
                raise ValueError('invalid route')
            ask='none'
            if card is not None:
                query,ask=request_card.apply(card,plan,payload)
                if route=='clarify' or (route=='catalog' and not query):
                    reply=request_card.question(card,ask,turn)
            store.add_step(sid,turn,'llm',res.model,{},dict(route=route),res.prompt_tokens,res.completion_tokens,res.seconds)
            if route=='catalog' and query.strip():
                out=tool('search_products',dict(query=query))
                if card is not None:
                    narrowed=request_card.narrow(card,out,tool)
                    reply=(f"Для {out['products'][0]['title']} в проверенных вариантах не подтверждены указанные условия. Какое условие можно изменить?"
                           if narrowed is not out and not narrowed.get('products') else catalog_reply(narrowed))
                    out=narrowed
                else:reply=catalog_reply(out)
                exact = (out.get('first_product') or {}) if out.get('match')=='all' and out.get('total')==1 else {}
                compact_order.bind_search(items, exact.get('offers',[]), exact.get('product_id'))
                items['compact_products'] = [p['title'] for p in out.get('products', [])[:4]] if out.get('match') == 'all' else []
            elif route=='total': reply=total_reply(raw,items.get('compact_offers',[]),tool)
            elif route=='help' and (query.strip() or card is not None):
                reply = buyer_help(payload['request'],query or payload['request'])
            elif route=='human': reply=CONFIRM
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            trace.append(dict(kind='error',name='compact',output=type(exc).__name__))
            store.add_step(sid,turn,'error','compact',{},dict(error=type(exc).__name__))
    elif card is not None and card['fields'] and not asks_to_choose:
        # A terse answer (e.g. a bare count) need not qualify as a search term.
        # Keep the facts already recorded; ask only about an unresolved dimension.
        reply=request_card.question(card,'none',turn)
    selection_dialog.note(items,reply,turn)
    store.update_session(sid,facts=ctx.facts,items=items)
    store.add_usage(sid,usage['prompt_tokens'],usage['completion_tokens'],usage['llm_calls'],usage['tool_calls'])
    usage['cost_rub']=cost(usage['prompt_tokens'],usage['completion_tokens'])
    meta=dict(cards=[],choices=[],events=ctx.events,usage=usage)
    store.add_message(sid,'assistant',reply,meta)
    return dict(session_id=sid,reply=reply,trace=trace,**meta)
