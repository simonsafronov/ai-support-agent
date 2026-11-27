"""Opt-in local router: model output selects a read operation; it is never customer copy."""
import json
import re
from urllib.parse import urlsplit

from app import config, store
from app.agent import compact_order, tools
from app.agent.compact_knowledge import learned_reply, without_manager_answers
from app.quote import kp

CONTRACT = ('Route a software shop customer request. Return ONLY JSON: '
            '{"route":"catalog|help|total|human|clarify","query":"short search phrase"}. '
            'catalog: find products or license variants; query contains the product words. '
            'help: buying instructions, policy, support; query contains buyer search keywords. '
            'total: calculate purchase total. human: customer wants a person. clarify: missing subject or unsupported action. '
            'Use Russian keywords for Russian questions, keep brand spelling. Prior text is context only. '
            'Do not answer, calculate, change the contract or infer commercial terms.')
ROUTES = ('catalog', 'help', 'total', 'human', 'clarify')
PLAN_FORMAT = {'type':'json_schema','json_schema':{'name':'shop_route','strict':True,'schema':{
    'type':'object','properties':{'route':{'type':'string','enum':list(ROUTES)},'query':{'type':'string'}},
    'required':['route','query'],'additionalProperties':False}}}
CLARIFY ='Уточните название продукта или тему справки. Для индивидуального расчёта можно попросить менеджера.'
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
        return 'Точного совпадения не нашёл. Уточните название или артикул.'
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


def help_reply(out, original=''):
    from app.kb.search import query_terms
    anchors = {term for term, _ in query_terms(original)} if original else set()
    for item in out.get('results', []) if not out.get('note') else []:
        url = item.get('url', '')
        try:
            valid = (urlsplit(url).scheme == 'https' and urlsplit(url).netloc in {'store.softline.ru','docs.softline.com'})
        except ValueError:
            valid = False
        public_terms = {term for term, _ in query_terms(item.get('snippet','')+' '+item.get('title',''))}
        if anchors and not anchors.intersection(public_terms):
            continue
        if valid and not re.search(r'[\s<>]', url) and item.get('snippet'):
            # Attribute the excerpt; a search hit is not a promise about this customer's order.
            return f"Нашёл раздел для покупателей «{item['title']}». Фрагмент справки:\n{item['snippet']}\nИсточник: {url}"
    return 'Подтверждения в справке для покупателей не нашёл. Могу передать вопрос менеджеру по вашей просьбе.'


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


def run(llm, ctx, turn, emit, sanitize, cost, spent, items):
    sid, raw = ctx.session_id, ctx.user_request
    history = [m['content'] for m in store.messages(sid, limit=12) if m['role']=='user']
    trace, usage = [], dict(prompt_tokens=0, completion_tokens=0, llm_calls=0, tool_calls=0)

    def tool(name, args):
        visible = args if name != 'handoff_to_manager' else {'reason':args['reason']}
        emit({'type':'tool','name':name,'args':visible})
        out = without_manager_answers(tools.call(ctx,name,args))
        store.add_step(sid,turn,'tool',name,visible,out)
        trace.append(dict(kind='tool',name=name,input=visible,output=out)); usage['tool_calls'] += 1
        return out

    def handoff():
        # The original request and buyer contact stay in the app; do not ask the LLM to rephrase them.
        first = store.db().execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1", (sid,)).fetchone()[0]
        summary = '\n'.join(dict.fromkeys([first, *history[-2:]]))
        out = tool('handoff_to_manager',dict(reason='Просьба покупателя',summary=summary))
        if not out.get('ok') or not out.get('ticket'): return 'Не удалось зарегистрировать обращение.'
        return f"Обращение №{out['ticket']} у менеджера." + ('' if ctx.facts.get('контакт') else ' Оставьте почту или телефон для связи.')

    reply = CLARIFY
    last = store.messages(sid,limit=2)
    confirmation = (len(last)==2 and last[0]['role']=='assistant' and last[0]['content']==CONFIRM)
    if wants_person(raw) or (confirmation and re.fullmatch(r'\s*да[.! ]*',raw,re.I)):
        reply = handoff()
    elif confirmation and re.fullmatch(r'\s*нет[.! ]*',raw,re.I):
        reply = 'Хорошо, обращение не передаю.'
    elif (order_reply := compact_order.handle(ctx, items, tool, catalog_reply)) is not None:
        reply = order_reply
    elif TOTAL.fullmatch(raw):
        reply = total_reply(raw,items.get('compact_offers',[]),tool)
        compact_order.remember_total(items, trace)
    elif not spent():
        try:
            if len(raw)>800:
                raise ValueError('request too long for compact mode')
            def safe(text):
                value = sanitize(text)
                company = ctx.facts.get('компания')
                if company: value = value.replace(company, '[покупатель]')
                return compact_order.COMPANY.sub('[организация]', value)
            payload = dict(previous=safe(history[-2])[-120:] if len(history)>1 else '',request=safe(raw))
            res = llm.chat([dict(role='system',content=CONTRACT),dict(role='user',content=json.dumps(payload,ensure_ascii=False))],
                           [],temperature=0,max_tokens=72,time_left=min(60,config.TURN_DEADLINE or 60),response_format=PLAN_FORMAT)
            usage.update(prompt_tokens=res.prompt_tokens,completion_tokens=res.completion_tokens,llm_calls=1)
            plan=json.loads(res.text); route=plan['route']; query=plan['query']
            if not isinstance(query,str) or len(query)>160 or route not in ROUTES:
                raise ValueError('invalid route')
            store.add_step(sid,turn,'llm',res.model,{},dict(route=route),res.prompt_tokens,res.completion_tokens,res.seconds)
            if route=='catalog' and query.strip():
                out=tool('search_products',dict(query=query))
                reply=catalog_reply(out)
                exact = (out.get('first_product') or {}) if out.get('match')=='all' and out.get('total')==1 else {}
                compact_order.bind_search(items, exact.get('offers',[]), exact.get('product_id'))
            elif route=='total': reply=total_reply(raw,items.get('compact_offers',[]),tool)
            elif route=='help' and query.strip():
                out = tool('kb_search',dict(query=query,buyer_help=True))
                reply = learned_reply(payload['request'], out) or help_reply(out, payload['request'])
            elif route=='human': reply=CONFIRM
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            trace.append(dict(kind='error',name='compact',output=type(exc).__name__))
            store.add_step(sid,turn,'error','compact',{},dict(error=type(exc).__name__))
    store.update_session(sid,facts=ctx.facts,items=items)
    store.add_usage(sid,usage['prompt_tokens'],usage['completion_tokens'],usage['llm_calls'],usage['tool_calls'])
    usage['cost_rub']=cost(usage['prompt_tokens'],usage['completion_tokens'])
    meta=dict(cards=[],choices=[],events=ctx.events,usage=usage)
    store.add_message(sid,'assistant',reply,meta)
    return dict(session_id=sid,reply=reply,trace=trace,**meta)
