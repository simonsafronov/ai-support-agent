"""Read-only shortlisting and explicit selection of a displayed catalog offer.
A category is a navigation aid, not evidence that a product meets a technical spec.
"""
import re

from app.agent import compact_order, consult, consult_catalog
from app.catalog import search
from app.quote import kp
from app.kb.search import query_terms

# Buyer tasks map to category searches, not product names, offer IDs or prices.
SECTIONS = (
    (r'резервн\w* копи|б[эе]кап|backup', 'резервного копирования'),
    (r'архиватор|(?:упаков|упакова|сжима|сжа)\w* (?:файлы|папки|архив)', 'архиваторы'),
    (r'(?:ретуш\w*|обработ\w*) (?:цифров\w+ )?(?:фото|снимк)|фоторедактор', 'обработки цифрового фото'),
    (r'почтов\w+ клиент|(?:читать|принимать) (?:корпоративную )?почту', 'почтовые клиенты'),
)
PICK=re.compile(r'(?:выбираю|беру|возьм[её]м|откройте|покажите|подходит)?\s*(?:(?:вариант|позиция)\s*(?:№\s*)?)?(первый|первую|второй|вторую|третий|третью|[1-3])(?:\s+(?:вариант|позицию))?',re.I)
ORDINAL={'первый':1,'первую':1,'второй':2,'вторую':2,'третий':3,'третью':3,'1':1,'2':2,'3':3}
COUNT=re.compile(r'(?:нужно\s+|возьм[её]м\s+|количество\s*[:—-]?\s*)?([1-9]\d{0,4})\s*(?:шт\.?|штук(?:и|у)?|экземпляр(?:а|ов)?|лицензи[ийюя]+)',re.I)
DOCUMENT=re.compile(r'(?:(?:пожалуйста[, ]+)?(?:подготовьте|сделайте|составьте|пришлите|оформите|сформируйте|нужно|хочу|можно|можете (?:подготовить|прислать))\s+(?:(?:нам|мне|получить)\s+)?)?(?:кп|коммерческое предложение)(?:\s+(?:по (?:этой|выбранной) позиции|по (?:этому|выбранному) варианту|на (?:это|выбранное) количество))?(?:,? пожалуйста)?\??',re.I)
KEY='task_shortlist'


def _filters(text):
    f=consult.parse(text);out={}
    if f.get('months'):out['term_months']=f['months']
    if f.get('mode'):out['mode']=f['mode']
    if f.get('os'):out['platform']={'windows':'Windows','linux':'Linux','macos':'Mac'}[f['os']]
    if f.get('org'):out['org_type']={'коммерческ':'Коммерческая','некоммерческ':'Некоммерческая','академическ':'Академическая'}.get(f['org'],f['org'])
    if re.search(r'нов\w+ лиценз',text,re.I):out['license_type']='Новая'
    if re.search(r'только в наличии',text,re.I):out['only_available']=True
    return out


def _fits(o,filters,units):
    # The search layer deliberately accepts unspecified values in some dimensions.
    # That is not sufficient evidence to present them as matching buyer constraints.
    for k,v in filters.items():
        if k=='mode':
            full=o if 'description' in o else search.get_offer(o['offer_id'])
            desc=str(full.get('description') or '').casefold()
            evidence={'offline':r'(?:работ\w*|использ\w*)\s+(?:офлайн|без (?:доступа к )?интернет)',
                      'server':r'(?:развертывани\w*|установк\w*) на (?:своем|собственном|локальном) сервере',
                      'cloud':r'облачн\w+ сервис|программное обеспечение как услуга'}
            if not re.search(evidence[v],desc.replace('ё','е')) or re.search(r'\b(?:не|если|кроме)\b',desc):return False
        if k=='only_available' and not o.get('available'):return False
        if k=='term_months' and o.get(k)!=v:return False
        if k in ('org_type','license_type','platform') and not re.search(r'(?<!\w)'+re.escape(str(v)),str(o.get(k) or ''),re.I):return False
    return not units or not kp.tier_problem(o,units)


def _offer_line(o):
    bits=[o['name']]
    for k,label in (('vendor_code','артикул'),('platform','ОС'),('org_type','организация'),('license_type','лицензия'),('version','версия'),('term_months','срок, мес.'),('user_type','покупатель'),('users_max','пользователей до'),('nodes_min','узлов от'),('nodes','узлов до'),('qty_min','количество от'),('qty_max','количество до'),('price_for','единица цены')):
        if o.get(k) is not None:bits.append(f'{label}: {o[k]}')
    price=o.get('price');bits.append(f'{price:g} ₽' if price and not kp.price_to_confirm(o) else 'цена требует подтверждения')
    if o.get('available') is False:bits.append(compact_order.UNAVAILABLE)
    return '; '.join(bits)


def _show(state,tool):
    offers=[];f=state['filters'];count=state.get('units');by_product=[]
    for product in state['products']:
        data=tool('get_product',dict(product_id=product['product_id'],**{k:v for k,v in f.items() if k!='mode'},**({'quantity':count} if count else {})))
        pool=[o for o in data.get('offers',[]) if _fits(o,f,count)]
        if pool:by_product.append(pool)
    # A small, varied page; don't present three volume tiers of one product if
    # alternatives exist. Option numbers refer only to this exact page.
    for index in range(6):
        offers.extend(pool[index] for pool in by_product if len(pool)>index)
        if len(offers)>=3:break
    offers=offers[:3];state['page']=[o['offer_id'] for o in offers]
    state['displayed']=[_offer_line(o) for o in offers]
    state.pop('selected',None);state.pop('facet',None)
    if not offers:return 'В просмотренных карточках нет варианта, где подтверждены все указанные параметры. Можно изменить срок или платформу; отсутствие товара во всём каталоге не установлено.'
    axes=[('license_type','Нужна сама лицензия, продление или поддержка?'),('org_type','Для какого типа организации нужна лицензия?'),('platform','На какой ОС будет работать программа?'),('term_months','На какой срок нужна лицензия?')]
    varying=[(k,question,list(dict.fromkeys(str(o[k]) for o in offers if o.get(k) is not None))) for k,question in axes if k not in f]
    state.pop('facet',None);question='Какой вариант подходит?'
    if varying:
        key,prompt,values=max(varying,key=lambda v:len(v[2]))
        if len(values)>1:
            state['facet']={'key':key,'values':values};question=prompt+' В этих позициях: '+', '.join(values)+'.'
    if not count and any(o.get('qty_min',0) or o.get('qty_max',0) for o in offers):
        question='Сколько единиц планируете приобрести? Количество может изменить ценовую ступень.'
    return ('Раздел «'+state['category']+'». Позиции для сравнения, не подтверждение всех технических требований:\n'+
            '\n'.join(f'{i}. {_offer_line(o)}' for i,o in enumerate(offers,1))+'\n'+question+' Можно выбрать номер позиции из списка.')


def start_section(topic,raw,items,tool,turn,prior_filters=None):
    cats=tool('browse_categories',{'query':topic}).get('categories',[])
    terms={word for word,_ in query_terms(topic)}
    matching=[c for c in cats if terms and terms<={word for word,_ in query_terms(c['name'])}]
    if len(matching)==1:
        products=tool('search_products',{'query':'','category_id':matching[0]['id']}).get('products',[])[:4]
        compact_order.bind_search(items,[]);items.pop('compact_products',None)
        state=items[KEY]=dict(turn=turn,category=matching[0]['name'],products=[{'product_id':p['product_id']} for p in products],filters={**(prior_filters or {}),**_filters(raw)})
        # Count purchased offer units, not people, nodes or installations.
        amounts=list(re.finditer(r'\b([1-9]\d{0,4})\s+(шт\b|экземпляр\w*|лицензи\w*)',raw,re.I))
        uncertain=re.search(r'\b(?:около|примерно|между|до|от|или|минимум|максимум)\s+\d|\d\s*[-–—]\s*\d',raw,re.I)
        if len(amounts)==1 and not uncertain:
            unit=amounts[0];state['units']=int(unit[1]);state['licenses']=unit[2].lower().startswith('лиценз')
        return _show(state,tool)


def handle(text,items,tool,turn):
    raw=text.strip().rstrip('.! ').strip();s=raw.casefold();state=items.get(KEY)
    if state and state.get('turn')!=turn-1:
        items.pop(KEY,None);state=None
    if state:state['turn']=turn
    if not s:return None
    topics=[name for pattern,name in SECTIONS if re.search(pattern,s)]
    # Also use the buyer's own activity words when they name an indexed category.
    # A partial FTS fallback or a match only in a parent path is not enough.
    generic=re.search(r'\b(?:программ[ауые]|софт|решение|по) для ([а-яё -]{5,70})',s)
    generic_terms=re.split(r'[,.;]|\s+(?:под|на|с|для|чтобы)\s+',generic[1])[0].strip() if generic else ''
    topic=topics[0] if len(topics)==1 else (generic_terms if not topics else '')
    if topic and ('«' not in raw and '"' not in raw) and not re.search(r'\b(?:не|кроме|без|кп)\b|сч[её]т|возврат|ошибк|продлен|активац|коммерческ\w* предложен',s):
        if not generic and consult_catalog.mentions(raw):
            items.pop(KEY,None);return None  # explicit product takes precedence over its category
        result=start_section(topic,raw,items,tool,turn)
        if result:return result
    if not state:return None
    match=PICK.fullmatch(raw);qty=COUNT.fullmatch(raw)
    if match:
        index=ORDINAL[match[1].lower()]-1
        if index>=len(state['page']):
            state.pop('selected',None);return 'Такой позиции в последнем списке нет. Укажите показанный номер.'
        state['selected']=state['page'][index]
    elif qty:
        state['units']=int(qty[1]);state['licenses']='лиценз' in s
        if not state.get('selected'):return _show(state,tool)
    elif DOCUMENT.fullmatch(raw):
        if not state.get('selected'):return 'Какую из показанных позиций включить в КП? Черновик ещё не создан.'
    elif (facet:=state.get('facet')) and s in [v.casefold() for v in facet['values']]:
        value=next(v for v in facet['values'] if v.casefold()==s)
        state['filters'][facet['key']]=int(value) if facet['key']=='term_months' else value
        return _show(state,tool)
    elif (delta:=_filters(raw)) and len(s.split())<=6 and not re.search(r'\b(?:не|без|или|кроме)\b|[«"?]',raw,re.I):
        state['filters'].update(delta)
        if not state.get('selected') or not _fits(search.get_offer(state['selected']),state['filters'],state.get('units')):
            return _show(state,tool)
    else:
        if (re.search(r'\b(?:отмен\w*|передумал\w*|вместо|другая задача|новая задача|не нужен|не нужна|не надо)\b',s)
                or re.search(r'[«"?]|сч[её]т|цен[ауые]|поддержк|ошибк|партн[её]р',s)
                or consult_catalog.mentions(raw) or consult_catalog.feature(raw)):
            items.pop(KEY,None);return None
        # An unparsed interjection cannot change the confirmed category, filters,
        # units or selection, and cannot approve a pending document.
        facet=state.get('facet');detail=', '.join(str(v) for v in state['filters'].values())
        return (f"Продолжаем раздел «{state['category']}»"+(' ('+detail+')' if detail else '')+
                '. Последнее сообщение не распознал как выбор. '+
                ('Вы имели в виду '+ ' или '.join(facet['values'])+'?' if facet else 'Уточните номер позиции или какое условие изменить.'))
    o=search.get_offer(state['selected']);units=state.get('units')
    if (not o or 'error' in o or not _fits(o,state['filters'],units)
            or _offer_line(o)!=state['displayed'][state['page'].index(state['selected'])]):
        state.pop('selected',None);return 'Выбранная позиция не подтверждается для этих условий. '+_show(state,tool)
    if units and state.get('licenses') and not re.search(r'за (?:1|одну) лицензию\b',str(o.get('price_for') or ''),re.I):
        return ('Выбрана позиция: '+o['name']+'. По карточке: '+str(o.get('price_for') or 'единица цены не указана')+
                '. Число лицензий не подставляю как число комплектов. Если нужна именно эта позиция, сколько штук включать в КП?')
    if not units:return 'Выбрана позиция: '+o['name']+'. Сколько единиц именно этой позиции нужно для КП?'
    if DOCUMENT.fullmatch(raw):
        if o.get('org_type') and not state['filters'].get('org_type'):
            return f"У этой цены ограничение по организации: {o['org_type']}. Для какого типа организации готовить КП?"
        if not o.get('price') or kp.price_to_confirm(o):return 'Итог по выбранной позиции требует подтверждения цены. КП с неподтверждённой суммой не создаю.'
        # No candidate or seat count can reach this write without an explicit
        # selection, a number of offer units, and a separate document request.
        confirmed={'compact_selected':o,'compact_qty':units,'compact_lines':[{'offer_id':o['offer_id'],'qty':units}]}
        return compact_order._quote(confirmed,tool)
    return 'Для КП выбрано: '+_offer_line(o)+f'. Количество выбранной позиции: {units} шт. Документ ещё не создан.'
