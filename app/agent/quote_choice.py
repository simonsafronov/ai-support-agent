"""A buyer-authorised, single-line quote spanning multiple compact turns.
Only a displayed, still valid catalog variant can finish a pending request.
"""
import re

from app.agent import compact_order, consult, consult_catalog as catalog, task_selection
from app.catalog import search
from app.quote import kp

KEY = 'quote_choice_v1'
DOC = re.compile(r'\b(?:кп|коммерческ\w* предложени\w*|коммерческое(?=\s+на\s+))\b', re.I)
ASK = re.compile(r'\b(?:нуж\w*|прошу|просим|хочу|хотим|запрашива\w*|можно|можете|пришл\w*|присла\w*|отправ\w*|подготов\w*|состав\w*|сдела\w*|оформ\w*|сформир\w*|дайте)\b', re.I)
OTHER = re.compile(r'\b(?:сч[её]т\w*|оплат\w*|партн[её]р\w*|оператор\w*|ошибк\w*|активац\w*|возврат\w*|тендер\w*|скидк\w*|отсроч\w*|поддержк\w*)\b', re.I)
CANCEL = re.compile(r'\b(?:отмен\w*|передум\w*|не надо|не нужно|не нужен|не хочу|не оформ\w*|не готовьте|не присылайте)\b', re.I)
CODE = re.compile(r'\b(?:артикул(?:у|а|ом)?|sku)\s*[:№#-]?\s*([\w][\w./-]{1,79})',re.I)
CODE_TOKEN = re.compile(r'(?<!\w)[A-Za-zА-Яа-я0-9]+(?:[-./][A-Za-zА-Яа-я0-9]+)+(?!\w)')
YES = re.compile(r'(?:да(?:,? (?:подходит|оформляйте|подтверждаю))?|подтверждаю|подходит|согласен|оформляйте)',re.I)
NUMBER = re.compile(r'\b(?:'+'|'.join(consult.NUMBERS)+r')\b',re.I)
ORD = {'первый':0,'первую':0,'второй':1,'вторую':1,'третий':2,'третью':2}


def requested(text):
    return bool(DOC.search(text) and (ASK.search(text) or DOC.fullmatch(text.strip('.! '))) and not OTHER.search(text))


def _params(text):
    s = NUMBER.sub(lambda m:str(consult.NUMBERS[m[0].lower()]),text.lower().replace('ё','е'))
    # Keep compound numerals intact or ask again. Never turn «тридцать семь» into 7.
    ambiguous = re.search(r'\b(?:около|примерно|между|до|от|минимум|максимум|порядка|более|менее)\s+\d|\d\s*(?:[-–—/,+]|или|до)\s*\d|\d[.,]\d|(?:\b\d+|\bсто|\bтысяч\w*|(?:дцать|десят|сот|ста))\s+\d+\b|(?<!\w)-\s*\d',s)
    counts = re.findall(r'(?<![\w-])(\d+)\s+(?:(?:годов|месячн|именн)\w*\s+)*(мест\w*|пользовател\w*|лицензи\w*|шт\.?|экземпляр\w*)\b',s)
    qty = int(counts[0][0]) if len(counts)==1 and len(counts[0][0])<=5 and not ambiguous else None
    bad = bool(ambiguous or counts and not qty)
    months = consult.parse(text).get('months')
    if re.search(r'\b(?:месячн\w*|на месяц)\b',s): months=1
    return {'qty':qty,'seats':bool(counts and counts[0][1].startswith(('мест','пользовател'))),'months':months,'bad':bad}


def _code_rows(code):
    return catalog._read(lambda db:[r[0] for r in db.execute('SELECT id FROM offers WHERE vendor_code IS NOT NULL AND vendor_code=? COLLATE NOCASE ORDER BY id LIMIT 65',(code,))])


def _identify(text):
    """None: no subject; []: explicit but not reliably identified. Never fuzzy-bind a write."""
    marked=CODE.findall(text)
    alphanumeric=[w for w in re.findall(r'(?<![\w.-])[A-Za-z][A-Za-z0-9]{2,}(?![\w.-])',text) if any(c.isdigit() for c in w)]
    detected=list(dict.fromkeys(marked or CODE_TOKEN.findall(text)+alphanumeric))
    codes=[(code,_code_rows(code)) for code in detected]
    codes=[(code,ids) for code,ids in codes if marked or ids]
    groups=catalog.quoted_products(text)
    products=groups[0][1] if len(groups)==1 else (catalog.mentions(text) if not groups else [])
    pids={str(p['product_id']) for p in products if 'product_id' in p}
    if codes:
        if len(codes)!=1 or groups and not pids:return []
        return [i for i in codes[0][1] if not pids or str(search.get_offer(i).get('product_id')) in pids]
    if detected or len(groups)>1:return []
    if not pids:return [] if groups else None
    if len(pids)>6:return []
    return catalog._read(lambda db:[r[0] for r in db.execute('SELECT id FROM offers WHERE product_id IN ('+','.join('?' for _ in pids)+') ORDER BY id LIMIT 65',tuple(pids))])


def _options(state):
    options=[]
    for ident in state['ids']:
        o=search.get_offer(ident)
        if o.get('error') or state.get('months') and o.get('term_months')!=state['months']:continue
        if not task_selection._fits(o,state.get('filters',{}),state.get('qty')):continue
        options.append(o)
    return options


def _line(o):
    parts=[o['name'],o.get('vendor_code'),o.get('version'),o.get('license_type'),o.get('org_type'),o.get('user_type'),o.get('platform')]
    for key,label in [('qty_min','количество от'),('qty_max','до'),('users_max','пользователей до'),('nodes_min','узлов от'),('nodes','узлов до')]:
        if o.get(key) is not None:parts.append(f'{label}: {o[key]}')
    if o.get('term_months') is not None:parts.append(f"{o['term_months']} мес.")
    parts.extend([o.get('price_for'),f"{o['price']:g} ₽" if o.get('price') and not kp.price_to_confirm(o) else 'цена требует подтверждения'])
    if o.get('available') is False:parts.append(compact_order.UNAVAILABLE)
    return '; '.join(str(v) for v in parts if v)


def _show(state, options):
    page=options[:3];state['page']={str(o['offer_id']):_line(o) for o in page}
    if not page:return 'Для запрошенных параметров позиция не подтвердилась. Уточните точный артикул или исправьте срок; КП не создано.'
    qty=state.get('qty');term=state.get('months')
    summary='Для КП: '+(f'{qty} шт.' if qty else 'количество не подтверждено')+(f', срок {term} мес.' if term else '')+'.'
    rows='\n'.join(f'{i}. {_line(o)}' for i,o in enumerate(page,1))
    if len(options)>1:question='Какую редакцию включить? Подтвердите номер показанной строки или её артикул.'
    elif not qty:question='Сколько единиц этой позиции включить в КП?'
    else:question='Подтвердите этот вариант для КП.'
    return f'{summary}\n{rows}\n{question}'


def _reset(items):
    items.pop(KEY,None);items.pop('task_shortlist',None);items.pop('consult_confirm_quote',None)
    items.pop('consult',None);consult._reset_selection(items)


def _confirmation(raw,state,options):
    s=re.sub(r'^да,?\s+', '',raw,flags=re.I)
    s=re.sub(r'^(?:бер[её]м|беру|выбираю|выбираем|подтвержда[юе]м?|подходит)\s+', '',s,flags=re.I).strip(' «».!')
    s=re.sub(r'\s+(?:подходит|подтверждаю)$','',s,flags=re.I)
    if YES.fullmatch(raw):return options if len(options)==1 else []
    page=list(state.get('page',{}));ordinal=re.fullmatch(r'(?:вариант\s*№?\s*)?([1-3]|первый|первую|второй|вторую|третий|третью)(?:\s+(?:вариант|позицию))?',s,re.I)
    if ordinal:
        w=ordinal[1].lower();i=int(w)-1 if w.isdigit() else ORD[w]
        return [o for o in options if i<len(page) and str(o['offer_id'])==page[i]]
    if re.fullmatch(r'(?:годов\w*|месячн\w*)(?: (?:редакцию|вариант|лицензию))?',s,re.I):
        return [o for o in options if o.get('term_months')==(12 if s.lower().startswith('год') else 1)]
    return [o for o in options if s.casefold() in {str(o.get(k) or '').casefold() for k in ('vendor_code','name','version')} and s]


def handle(request,items,tool,turn,previous_user='',*,card_fields=None):
    raw=request.strip(' .!');state=items.pop(KEY,None); active=bool(state and state['turn']==turn-1)
    if OTHER.search(raw):return None
    if CANCEL.search(raw) or re.search(r'\b(?:если|гипотетически|возможно|вместо|без)\b|\bне\b',raw,re.I):
        if active or requested(raw):
            _reset(items);return 'Не формирую КП. Для нового расчёта укажите позицию и подтверждённое количество.'
        return None
    if requested(raw):
        ident=_identify(raw);params=_params(raw)
        if previous_user and ident and not params['bad']:
            # Carry a term/count only across an explicitly matching catalog subject.
            # Do not borrow requirements from another buyer, product or stale turn.
            previous_ids=_identify(previous_user)
            if previous_ids and set(previous_ids)==set(ident) and not OTHER.search(previous_user):
                prev=_params(previous_user)
                if not prev['bad']:
                    if not params['qty']:params.update(qty=prev['qty'],seats=prev['seats'])
                    if not params['months']:params['months']=prev['months']
        if params['bad']:
            _reset(items);return 'Не могу однозначно определить количество для КП. Укажите одно точное количество и позицию.'
        # Only prefill a preview on a fresh explicit quote request for the SAME
        # identified product. Never derive a SKU or trigger issuance from model text.
        saved=card_fields or {};bound=saved.get('product',{})
        if ident and bound and set(_identify(bound['value']) or [])==set(ident):
            current={k:v['value'] for k,v in saved.items() if v['turn']>=bound['turn']}
            counts=[k for k in ('seats','units') if k in current]
            if not params['qty'] and len(counts)==1:params.update(qty=current[counts[0]],seats=counts[0]=='seats')
            if not params['months']:params['months']=current.get('months')
        if ident is None:
            remainder=ASK.sub(' ',DOC.sub(' ',raw))
            for token in re.findall(r'[\w-]+',remainder.casefold()):
                if not (token.isdecimal() or token in consult.NUMBERS or re.fullmatch(r'на|по|нам|мне|для|эту|этот|этой|это|этому|выбранн\w*|позици\w*|вариант\w*|количеств\w*|пожалуйста|мест\w*|пользовател\w*|именн\w*|лицензи\w*|год\w*|лет|месяц\w*|месячн\w*|шт|экземпляр\w*|срок\w*|отдельно|укажите|и|нужное',token)):
                    ident=[];break  # an unrecognised new subject cannot inherit an old SKU
        if ident is None and not (items.get('compact_selected') or items.get('task_shortlist') or items.get('consult') or items.get('compact_offers')):
            prior=_identify(previous_user) if previous_user and not OTHER.search(previous_user) else None
            if prior and not re.search(r'\b(?:не|если|или|вместо)\b',previous_user,re.I):
                ident=prior;prev=_params(previous_user)
                if not prev['bad']:
                    if not params['qty']:params.update(qty=prev['qty'],seats=prev['seats'])
                    if not params['months']:params['months']=prev['months']
        if ident is None:
            # Existing selected line, task shortlist and role-based consultation own
            # their proven flow, including their existing eligibility checks.
            shortlist=items.get('task_shortlist') or {}
            if shortlist.get('turn')==turn-1 and shortlist.get('page') and not raw.endswith('?'):
                ident=[shortlist['selected']] if shortlist.get('selected') else list(shortlist['page'])
                if not params['qty']:params.update(qty=shortlist.get('units'),seats=bool(shortlist.get('licenses')))
                if not params['months']:params['months']=shortlist['filters'].get('term_months')
                if shortlist.get('selected') and shortlist.get('units') and task_selection.DOCUMENT.fullmatch(raw):return None
            else:
                if items.get('compact_selected') or shortlist or items.get('consult'):return None
                shown=items.get('compact_offers') or []
                ident=[o['offer_id'] for o in shown]
                if shown and not params['qty']:params['qty']=items.get('compact_filter_qty')
        if not ident and not params['qty'] and not params['months'] and compact_order.QUOTE.fullmatch(raw):return None
        if (ident and len(ident)==1 and consult.QUOTE_ACTION.search(raw) and catalog.quoted_products(raw)
                and _params(raw)['qty'] and params['months']==_params(raw)['months'] and not CODE.search(raw)):
            return None  # preserve the established unambiguous single-line consultation/confirmation contract
        _reset(items);state=dict(turn=turn,ids=ident or [],qty=params['qty'],seats=params['seats'],months=params['months'],filters=task_selection._filters(raw))
        if len(state['ids'])>=65:state['ids']=[]
        items[KEY]=state
        if not state['ids']:return 'Срок и точное количество, если указаны, сохранены для КП. Какой артикул или полное название с редакцией нужно включить?'
        return _show(state,_options(state))
    if not active:return None
    state['turn']=turn;options=_options(state);chosen=_confirmation(raw,state,options)
    if not state['ids']:
        ids=_identify(raw)
        if ids and len(ids)<65:
            state['ids']=ids;items[KEY]=state;return _show(state,_options(state))
        return None
    if not chosen:
        if YES.fullmatch(raw) or re.match(r'^(?:выбираю|подтверждаю|годов|месячн|вариант\s)',raw,re.I):
            items[KEY]=state;return _show(state,options)
        count=_params(raw)
        if count['bad']:state['qty']=None;items[KEY]=state;return _show(state,_options(state))
        if count['qty'] and re.fullmatch(r'(?:[\w]+\s+){0,2}(?:мест[а]?|лицензи\w*|шт\.?|экземпляр\w*)',raw,re.I):
            state.update(qty=count['qty'],seats=count['seats']); options=_options(state)
            chosen=[o for o in options if o['offer_id']==state.get('approved_id')]
            if not chosen:
                items[KEY]=state;return _show(state,options)
        else:
            return None  # subject changed: pending write expires, no quote or handoff
    if len(chosen)!=1:
        state['ids']=[o['offer_id'] for o in chosen];items[KEY]=state;return _show(state,chosen)
    o=chosen[0];oid=str(o['offer_id']);qty=state.get('qty')
    if state.get('page',{}).get(oid)!=_line(o):
        items[KEY]=state;return _show(state,options)
    state['ids']=[o['offer_id']];state['approved_id']=o['offer_id']
    if not qty:items[KEY]=state;return _show(state,[o])
    if state['seats']:
        allowed,_=consult._qty({'people':qty},catalog.text_of(o)+' '+str(o.get('price_for') or ''))
        if allowed!=qty:
            state['qty']=None;items[KEY]=state
            return 'Не подтверждено, сколько мест покрывает одна позиция. Сколько штук именно выбранного артикула включить в КП?'
    if not o.get('price') or kp.price_to_confirm(o):return 'Цена выбранного варианта требует проверки. КП с неподтверждённой суммой не сформировано.'
    items['compact_selected']=o;items['compact_product_id']=str(o['product_id']);items['compact_offers']=[o]
    error=compact_order._accept(items,o,qty)
    return error or compact_order._quote(items,tool)
