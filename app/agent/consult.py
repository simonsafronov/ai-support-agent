"""Compact multi-turn consultation: buyer facts and catalog evidence remain separate."""
import re

from app.agent import compact_order as order, consult_catalog as cards, consult_named
from app.catalog import search
from app.quote import kp

NUMBERS = {'одного':1,'одной':1,'двух':2,'трех':3,'четырех':4,'пяти':5,'шести':6,'семи':7,'восьми':8,'девяти':9,'десяти':10,'один':1,'одна':1,'одно':1,'одну':1,'два':2,'две':2,'три':3,'четыре':4,'пять':5,'шесть':6,'семь':7,'восемь':8,'девять':9,'десять':10,'двенадцать':12,'двенадцати':12,'двадцать':20}
UNITS = {'authors':r'автор','readers':r'читател|читают','people':r'человек|сотрудник|инженер|пользовател|мест|именны|именная|лицензи','devices':r'устройств|компьютер','shifts':r'смен'}
QUOTE_ACTION = re.compile(r'\b(?:сделайте|составьте|сформируйте|оформите|подготовьте|пришлите|нужно|нужен)\s+(?:нам\s+|мне\s+)?(?:кп\b|(?:коммерческое )?предложение\b)',re.I)
STOP_WRITE = re.compile(r'\b(?:не|без|если|вместо|отмен\w*|образец|пример|как|зачем|почему)\b',re.I)
OTHER = re.compile(r'\b(?:скидк|отсроч|тендер|рассроч|возврат|ндс|продл|докуп|оплат|счет|активац|ключ|упд|парол|вход)\w*',re.I)


def parse(text):
    s=text.casefold().replace('ё','е'); num=re.sub(r'\b(?:'+'|'.join(NUMBERS)+r')\b',lambda m:str(NUMBERS[m[0]]),s)
    num=re.sub(r'\b(20) ([1-9])\b',lambda m:str(int(m[1])+int(m[2])),num)
    delta={}
    for key,unit in UNITS.items():
        for m in re.finditer(r'\b([1-9]\d{0,4})\s+(?:(?:годов|действующ)\w+\s+)?(?:'+unit+r')\w*\b',num):delta[key]=int(m[1])
    correction=re.search(r'\bне\s+\d+\s+(?:мест\w*|человек\w*|пользовател\w*|сотрудник\w*|лицензи\w*),?\s+а\s+([1-9]\d{0,4})\b',num)
    if correction:delta['people']=int(correction[1])
    if re.search(r'\b(?:один|одна)\b.{0,16}(?:работаю|пользуюсь)|(?:работаю|пользуюсь)\s+(?:только\s+)?(?:один|одна)\b',s):delta['people']=1
    for m in re.finditer(r'(?:одновременн\w*|пик\w*)\D{0,32}\b([1-9]\d{0,4})\b|\b([1-9]\d{0,4})\s+одновременн',num):delta['peak']=int(m[1] or m[2])
    if re.search(r'\bгод\b|годов\w*',s):delta['months']=12
    years=re.search(r'\b([1-9]\d?)\s+(?:год(?:а|ов)?|лет)\b',num)
    if years:delta['months']=12*int(years[1])
    months=re.search(r'\b([1-9]\d?)\s+месяц',num)
    if months:delta['months']=int(months[1])
    after_unit=re.search(r'(?:число (?:мест|пользователей)|пользователей|мест)\s+(?:по-прежнему\s+|теперь\s+)?([1-9]\d{0,4})\b',num)
    if after_unit:delta['people']=int(after_unit[1])
    if 'сервер' in s and not re.search(r'без сервер|не .*сервер',s):delta['mode']='server'
    elif re.search(r'облак.{0,24}(?:запре|нельзя)|(?:запре\w*|без|нельзя).{0,20}облак|офлайн|без интернет|интернет.{0,14}не всегда',s):delta['mode']='offline'
    elif re.search(r'(?:нуж\w*|хочу|бер[еу]м)\s+облак',s):delta['mode']='cloud'
    for clause in re.split(r'[;.!?\n]',s):
        for name,pat in [('windows',r'windows'),('macos',r'\bmac(?:os)?\b|макос'),('linux',r'linux')]:
            if re.search(pat,clause) and not re.search(r'(?:'+pat+r').{0,55}(?:нельзя|запрещ|не использу)|(?:кроме|без)\s+(?:'+pat+r')',clause):delta['os']=name
    for org,pat in [('академическ',r'для (?:школы|вуза|учебного заведения)'),('коммерческ',r'коммерческ\w+ организац'),('некоммерческ',r'некоммерческ\w+ организац')]:
        if re.search(pat,s):delta['org']=org
    goal=cards.feature(s)
    if goal:delta['goal']=goal
    return delta


def _qty(facts,evidence):
    """Return a billable count only when the offer confirms the relevant license metric."""
    if re.search(r'\b(?:если|кроме|за исключением|не разреш\w*|не предостав\w*|не бесплат\w*|не включен\w*)\b',evidence):
        return None,'В карточке есть исключения или условия. Нельзя подтвердить покрытие лицензией без их проверки.'
    author_metric = (re.search(r'(?:чтени\w*|читател\w*|просмотр)[^.]{0,45}бесплат', evidence)
                     and re.search(r'только автор|лицензи\w* для автор', evidence))
    if author_metric and not facts.get('authors'):
        return None,'Сколько сотрудников создают или изменяют документы, а сколько только читают? По карточке чтение бесплатное.'
    if 'authors' in facts or 'readers' in facts:
        if not facts.get('authors') or not author_metric:
            return None,'В карточке нужно подтвердить, какие роли оплачиваются; число сотрудников не подставляю в КП.'
        if facts.get('people') and facts.get('readers') and facts['people']!=facts['authors']+facts['readers']:
            return None,'Число авторов и читателей не совпало с общим количеством. Какие роли и количества брать для КП?'
        return facts['authors'],'Оплачиваются авторы; чтение по этой карточке бесплатное.'
    if 'одновременн' in evidence or facts.get('peak') or facts.get('shifts'):
        if 'одновременн' not in evidence:return None,'Право на одновременные подключения не подтверждено карточкой. Число смен само по себе не уменьшает число лицензий.'
        return facts.get('peak'),'Сколько человек будут работать одновременно в самый загруженный момент?' if not facts.get('peak') else 'Основание — пиковое число одновременных подключений.'
    if 'именн' in evidence or 'за пользователя' in evidence or 'одного пользователя' in evidence or 'за одного пользователя' in evidence or 'за 1 лицензию' in evidence:
        return facts.get('people'),'Именная лицензия привязана к человеку, а не к устройству.' if 'именн' in evidence else 'Количество считается по пользователям.'
    return None,'Единица лицензирования не подтверждена. Количество людей нельзя автоматически считать количеством лицензий.'


def _draft(text):
    if STOP_WRITE.search(text) or not QUOTE_ACTION.search(text):return False
    # Do not silently accept an extra commercial condition, replace a SKU or promise a service.
    remaining=QUOTE_ACTION.sub('',text.casefold()).strip(' ,.!')
    additional = r'(?:и отдельно (?:укажите|покажите) (?:поддержку|установку)|и скажите,? сколько стоит (?:установка|поддержка))'
    return not remaining or bool(re.fullmatch(r'(?:на (?:нужное|это) количество)?\s*(?:'+additional+r')?',remaining))


def _reset_selection(items):
    for k in ('compact_selected','compact_lines','compact_qty','compact_offers','compact_product_id','compact_filter_qty','compact_pending_quote'):
        items.pop(k,None)


def handle(request, history, items, tool):
    """Return None for non-consultation paths. No customer utterances are stored here."""
    if not request or len(request)>800:return None
    text=request.casefold().replace('ё','е'); prev=items.get('consult') or {}; delta=parse(text)
    if order.COMPANY.search(request):return None
    # A quoted subject is a binding, not a suggestion from the router. Match the
    # requested property before quantity handling (e.g. GB are not seats).
    groups=cards.quoted_products(request)
    prop=consult_named.property_of(request)
    if prop and (not OTHER.search(text) or prop[0] in ('upgrade','renew')):
        named=groups or [(p['title'],[p]) for p in cards.mentions(request) if p.get('product_id')]
        if named:
            # A property question may switch the conversation to another vendor or edition.
            # Do not carry the previous quote's quantity into that new subject.
            bound=[p for _,group in named for p in group]
            current=prev.get('pid') or items.get('compact_product_id')
            if len(bound)!=1 or str(current)!=str(bound[0]['product_id']):
                _reset_selection(items);items.pop('consult',None)
                if len(named)==1 and len(bound)==1:
                    items['consult']={'pid':str(bound[0]['product_id']),'title':bound[0]['title']}
            return consult_named.reply(request,named,prop,tool)
    if OTHER.search(text):return None
    if re.search(r'\b(?:от|между)\s+\d+\s+(?:до|и)\s+\d+|\b\d+\s*[-–]\s*\d+\s+(?:лиценз|мест|пользоват)', text):
        _reset_selection(items)
        items.pop('consult',None)
        return 'Для точного расчёта нужно одно количество. Какое значение использовать?'
    if re.search(r'^\s*(?:рассчитайте|посчитайте)\s+\d+\s*шт\.',text) or order.QUANTITY.fullmatch(text.strip('.! ')) or re.match(r'^(?:выбир|выбрать|беру |добавьте)',text):return None
    explicit_quote=bool(QUOTE_ACTION.search(text));calc=bool(re.search(r'рассчита|расчет|посчита|стоимост|во сколько|обойд[её]тся|сколько стоит|цен[ауые]\b|повторите.{0,20}итог',text))
    if prev.get('goal')=='pdf_form' and re.search(r'сохранил\w*|значение на месте|получилось',text) and not re.search(r'\bне\b',text):
        items.pop('consult',None);return 'Если после повторного открытия заполненное поле осталось на месте, задача решена. Покупать редактор для этого не нужно.'
    if not (delta or explicit_quote or calc or (prev and re.search(r'сменщик|поддержк|установк',text))):return None
    if explicit_quote and not groups and not prev and not delta:
        return None  # use the ordinary order handler with its selected SKU and pending lines
    if re.search(r'продл|добавить.{0,12}мест|\bили\b|сравн',text) and not delta.get('goal'):return None
    # Last buyer-owned product mention is a reference, not authorization to order arbitrary IDs.
    if groups and (len(groups)!=1 or len(groups[0][1])!=1):
        _reset_selection(items); items.pop('consult',None)
        variants=[p['title'] for _,ps in groups for p in ps]
        return ('Уточните одну редакцию: '+', '.join(variants[:4])+'.' if variants else 'Не подтвердил указанное название в каталоге. Какой у продукта артикул?')
    found=groups[0][1] if groups else cards.mentions(request)
    if not found and not prev.get('pid') and not prev.get('goal'):
        for h in reversed(history[:-1][-3:]):
            found=cards.mentions(h)
            if found:break
    if len(found)>1:
        _reset_selection(items);items.pop('consult',None)
        return None
    selected=items.get('compact_selected') or {}; pid=str(selected.get('product_id') or items.get('compact_product_id') or '')
    if found and found[0].get('family'):
        variants=found[0]['variants'];mode=delta.get('mode',prev.get('mode'))
        matched=[v for v in variants if cards.deployment({'name':v['title']})==mode] if mode else []
        if len(matched)!=1: return 'Какой вариант размещения нужен: '+', '.join(v['title'] for v in variants)+'?'
        found=matched
    fresh=found and str(found[0]['product_id'])!=str(prev.get('pid') or pid)
    facts=dict(prev) if not fresh else {}
    if fresh and prev:_reset_selection(items)
    if found:facts.update(pid=str(found[0]['product_id']),title=found[0]['title'])
    elif pid and not facts.get('pid'):facts.update(pid=pid,title=selected.get('product_title',''))
    elif selected and not facts.get('pid') and selected.get('offer_id'):
        actual=search.get_offer(selected['offer_id']);facts.update(pid=str(actual['product_id']),title=actual['product_title'])
    if not (facts.get('pid') or delta.get('goal') or facts.get('goal')):return None
    if any(k in delta and prev.get(k)!=delta[k] for k in ('mode','people','authors','readers','peak','os','months','goal','org','devices','shifts')):
        _reset_selection(items)
    facts.update(delta);items['consult']=facts
    ids=[]
    if facts.get('pid'):
        ids=cards.variants(facts['pid'])
        if facts.get('mode') and facts.get('title'):
            for sib in cards.siblings(facts['title']):
                if str(sib['product_id'])!=facts['pid']:ids+=cards.variants(sib['product_id'])
    elif facts.get('goal'):ids=cards.discover(facts['goal'])
    candidates=[]
    for entry in ids[:48]:
        o=search.get_offer(entry['offer_id']); ev=cards.text_of(o)
        if facts.get('goal') and not cards.supported(o,facts['goal']):continue
        if facts.get('months') and o.get('term_months')!=facts['months']:continue
        mode=cards.deployment(o)
        if facts.get('mode') and not (mode==facts['mode'] or facts['mode']=='offline' and 'без интернет' in ev):continue
        platform=(o.get('platform') or ' '.join((o.get('params') or {}).get('Платформа',[]))).lower()
        if facts.get('os') and facts['os'] not in platform:continue
        candidates.append(o)
    if not candidates:
        _reset_selection(items);return 'Среди проверенных вариантов нет позиции, для которой подтверждены все ваши ограничения'+(' ('+', '.join(str(facts[k]) for k in ('os','mode') if facts.get(k))+')' if facts.get('os') or facts.get('mode') else '')+'. Не буду предлагать неподтверждённую замену.'
    if len(candidates)!=1 or len(ids)>=17:
        _reset_selection(items)
        items['compact_offers']=[{k:o.get(k) for k in ('offer_id','product_id','name','vendor_code','price','qty_min','qty_max','qty_pricing','available','price_for','org_type')} for o in candidates[:3]]
        return 'Среди проверенных позиций: '+ '; '.join(dict.fromkeys(o.get('vendor_code') or o['name'] for o in candidates[:3]))+'. Для расчёта нужен конкретный вариант и его условия.'
    o=candidates[0]; ev=cards.text_of(o); title=o['product_title'];sku=o.get('vendor_code') or title
    if facts.get('pid') or not facts.get('goal'):
        facts.update(pid=str(o['product_id']),title=title)
    # Record the verified position through the normal tool journal (UI and price audit).
    verified=tool('get_product',{'product_id':str(o['product_id'])})
    if not any(v.get('offer_id')==o['offer_id'] for v in verified.get('offers',[])):
        return 'Нашёл продукт, но не подтвердил конкретную позицию в его карточке. Уточните артикул.'
    if facts.get('goal')=='pdf_form':
        return f'В карточке {title} подтверждено заполнение полей PDF. Попробуйте заполнить поле в имеющемся просмотрщике, сохранить копию и открыть её заново. Если значение сохранилось, покупка не нужна.'
    if (facts.get('people')==1 and re.search(r'\b(?:сменщик|второй (?:пользователь|сотрудник))\b',text)
            and re.search(r'появил|добавил|присоедин',text) and not re.search(r'\b(?:вместо|не)\b',text)):
        facts['people']=2
    qty,why=_qty(facts,ev+' '+str(o.get('price_for') or ''));description=(o.get('description') or '').strip(); evidence=(' В карточке: '+description) if 0<len(description)<=400 else ''
    # A request for a number of seats is not proof that a particular SKU grants
    # those rights. Confirm the intended configuration without inventing a total.
    if qty is None and (calc or explicit_quote) and not any(facts.get(k) for k in ('authors','readers','shifts','peak')):
        if not facts.get('people'):
            missing='сколько мест нужно' + (' и на какой срок' if not facts.get('months') else '')
            return f'{title} ({sku}): {missing}? До уточнения итог не считаю.'
        term=f", срок в месяцах: {facts['months']}" if facts.get('months') else ''
        unit = f"В карточке {sku}: {o['price']:g} ₽ за позицию. " if o.get('price') and not kp.price_to_confirm(o) and not o.get('qty_min') and not o.get('qty_max') and not o.get('org_type') else ''
        return f"{title}: количество мест — {facts['people']}{term}. {unit}Сколько мест покрывает одна позиция, не подтверждено; итог по местам не называю."
    if qty is None:
        if facts.get('goal') and not (calc or explicit_quote):
            suffix = ' Платформа: '+platform+'.' if platform else ''
            return f'{title} ({sku}).{evidence}'+suffix+(' Для какой ОС нужна программа?' if not facts.get('os') else '')
        return f'{title} ({sku}).{evidence} '+why
    org=str(o.get('org_type') or '').lower()
    if org and not (facts.get('org') and re.search(r'\b'+facts['org'],org)):
        return f'{sku}: цена относится к категории «{o["org_type"]}». Для какой организации нужен расчёт?'
    problem=kp.tier_problem(o,qty)
    if problem:return f'{sku}: выбранное количество {qty} не подходит под ступень цены; КП не формирую.'
    if kp.price_to_confirm(o) or not o.get('price'):return f'{sku}, {qty} шт. Цена требует подтверждения; итог не называю.'
    items['compact_offers']=[o];items['compact_selected']=o;items['compact_product_id']=str(o['product_id']);order._accept(items,o,qty)
    extra=' Услуга отдельно; её стоимость не подтверждена и в сумму не включена.' if re.search(r'(?:поддержк|установк)\w* отдельно',ev) else ''
    if explicit_quote:
        # Natural-language lines can authorize this draft only when the whole
        # remaining clause consists of the bound name, a count and a term.
        clause=QUOTE_ACTION.sub('',text,count=1)
        if groups and len(groups)==1:
            clause=re.sub(r'[«"]'+re.escape(groups[0][0].casefold())+r'[»"]',' ',clause,count=1)
        num_clause=re.sub(r'\b(?:'+'|'.join(NUMBERS)+r')\b',lambda m:str(NUMBERS[m[0]]),clause)
        clean_line=groups and re.fullmatch(r'\s*(?:на\s+)?\s*'+str(qty)+r'\s+(?:годов\w+\s+)?(?:мест\w*|лицензи\w*|пользовател\w*)\s*(?:на год|на 12 месяцев)?[. !]*',num_clause)
        if _draft(request) or (clean_line and not STOP_WRITE.search(text)):
            return order._quote(items,tool)+f' {sku}, {qty} шт. '+why+extra
        if STOP_WRITE.search(text):return 'Не сформировал КП: уточните, нужен ли документ на выбранную позицию.'
        items['consult_confirm_quote']=[{'offer_id':o['offer_id'],'qty':qty}]
        return f'Для КП проверена позиция {title}, {sku}: {qty} шт.'+(f" Срок: {facts['months']} мес." if facts.get('months') else '')+' Сформировать черновик только на эту позицию, без дополнительных условий? Заказ не создаётся.'
    if calc:
        out=tool('calc_total',{'items':[{'offer_id':o['offer_id'],'qty':qty}]}); line=(out.get('lines') or [{}])[0]
        if not line.get('sum'):return 'Не получил подтверждённого итога для этой позиции.'
        return f'{title}, {sku}: {qty} шт., итого {line["sum"]:g} ₽. '+why+extra+(' '+order.UNAVAILABLE if not o.get('available') else '')
    return f'{title}, {sku}: {qty} шт. '+why+evidence+extra
