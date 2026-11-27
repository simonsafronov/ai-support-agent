"""Buyer-reported partner stock is an internal escalation, not a supplier inquiry."""
import re

from app import store

PARTNER = re.compile(r'\bпартн[её]р\w*\b', re.I)
ABSENT = re.compile(r'нет в наличии|не в наличии|нет (?:в (?:вашем |нашем )?каталоге|на (?:вашем |нашем )?складе)|'
                    r'(?:у вас|в вашем (?:магазине|каталоге)|на вашем складе)[^.!?;]{0,55}\b(?:нет|отсутствует|закончился)|'
                    r'нет у (?:вас|нас)|(?:товар|позиция|лицензия|программа|версия) отсутствует|'
                    r'(?:не наш[её]л|не нашли|отсутствует) (?:в (?:вашем )?каталоге|у вас)|в каталоге не наш[её]л|'
                    r'(?:этого товара|этой позиции|такого продукта|нужной лицензии) нет|точного совпадения не наш[её]л|не узнаю это название в каталоге', re.I)
CLAIM = re.compile(r'\b(?:есть|имеется|доступен|доступна|доступно|в наличии|прода[её]тся|продают|прода[её]т)\b',re.I)
UNCERTAIN = re.compile(r'\b(?:если|вдруг|ли|может|возможно|наверное|вероятно|допустим|предположим|гипотетически|якобы|другой|другая|другое|другую)\b',re.I)
DENIED = re.compile(r'\b(?:не\s+(?:переда|отправ|обращ|нуж|надо|хочу|созда|регистрир|завод)\w*|без (?:менеджера|обращения|передачи))\b',re.I)
NAMES = re.compile(r'[«"]([^«»"\n]{2,100})[»"]')
REASON = 'Внутренняя проверка отсутствующей позиции'
CLARIFY = 'Речь о той же позиции, которой нет в нашем каталоге или в наличии? Уточните название или артикул. Обращение пока не создано.'


def classify(text, recent):
    """None = ordinary flow. Only affirmative, same-subject reports may create a ticket."""
    if not PARTNER.search(text): return None
    if not re.search(r'\bесть\b|наличи|склад|остатк|прода[её]|доступн|наш[её]л|нашла|\bимеется\b|\bнет\b',text,re.I): return None
    clauses=re.split(r'[.!?;\n]|,?\s+(?:а|но|зато)\s+|,\s*(?=у вас\b|(?:у\s+)?(?:вашего\s+)?партн[её]р)',text.lower())
    source=[part for part in clauses if PARTNER.search(part)]
    partner_question=any(PARTNER.search(sentence) or ABSENT.search(sentence) for sentence in re.findall(r'[^.!?;\n]+\?',text))
    if (partner_question or UNCERTAIN.search(text) or DENIED.search(text)
            or any(re.search(r'\b(?:не|нет|отсутств\w*|законч\w*)\b',part) for part in source)
            or not any(CLAIM.search(part) for part in source)):
        return 'no_action'
    local=[part for part in clauses if not PARTNER.search(part) and ABSENT.search(part)]
    if any(re.search(r'нет (?:скидки|отсрочки|проблем|вопросов|опыта)',p) for p in local):
        return 'clarify'
    if not local:
        # Only the immediately preceding buyer/assistant exchange; a stale product
        # or mixed availability on a list of alternatives is not a missing item.
        if len(recent)!=2 or [m['role'] for m in recent]!=['user','assistant']:return 'clarify'
        user,assistant=[m['content'] for m in recent]
        if assistant.count('•')>1 or re.search(r'по фиду:\s*в наличии|по фиду в наличии',assistant,re.I):return 'clarify'
        if not ABSENT.search(user) and not ABSENT.search(assistant):return 'clarify'
        local=[user,assistant]
    before={v.casefold() for v in NAMES.findall(' '.join(local))}; now={v.casefold() for v in NAMES.findall(' '.join(source))}
    if now and not now<=before:return 'clarify'
    return 'report'


def is_stock_ticket(sid, ticket=None):
    ticket = ticket or store.open_handoff(sid)
    if not ticket:
        return False
    if ticket['reason'] == REASON:
        return True
    # An existing queue entry keeps its original reason. Bind this routing decision
    # to its ID, not to the lifetime of the customer's history or subsequent tickets.
    session = store.find_session(sid)
    return session is not None and session['items'].get('partner_stock_ticket') == ticket['id']


def receipt(sid,text,ticket):
    previous=store.messages(sid,limit=2)
    kind=classify(text,previous)
    if (not kind and len(previous)==2 and previous[-1]['content']==CLARIFY
            and re.match(r'^\s*(?:да\b|именно\b|верно\b)',text,re.I)
            and not re.search(r'\b(?:нет,|друг\w*|не (?:та|тот|эта|переда\w*))|\?',text,re.I)
            and not DENIED.search(text) and not UNCERTAIN.search(text)):
        # The previous message asserted partner stock; this reply confirms the missing
        # local item. Reject a different named product rather than linking two tickets.
        old=set(x.casefold() for x in NAMES.findall(previous[0]['content']))
        new=set(x.casefold() for x in NAMES.findall(text))
        if not old or not new or old==new:kind='report'
    if not kind:return None
    session=store.find_session(sid)
    if not session:return None
    if ticket:
        if kind=='report' and ticket['reason']!=REASON:
            session['items']['partner_stock_ticket']=ticket['id']
            store.update_session(sid,items=session['items'])
        return None  # caller's normal single-ticket queue receipt
    store.add_message(sid,'user',text)
    events=[];trace=[]
    if kind=='report':
        prior=next((m['content'] for m in previous if m['role']=='user'),'')
        summary='Со слов покупателя; не проверено.\n'+'\n'.join(dict.fromkeys(v for v in (prior,text) if v))
        hid,created=store.handoff(sid,REASON,summary,'')
        if created:events.append({'type':'handoff','id':hid,'reason':REASON})
        result={'ok':True,'ticket':hid};args={'reason':REASON}
        turn=store.db().execute("SELECT count(*) FROM messages WHERE session_id=? AND role='user'",(sid,)).fetchone()[0]
        store.add_step(sid,turn,'tool','handoff_to_manager',args,result)
        trace.append({'kind':'tool','name':'handoff_to_manager','input':args,'output':result})
        reply=f'Обращение №{hid} передано внутреннему менеджеру. Наличие у партнёра не подтверждено. Ответ придёт сюда; партнёру ничего не отправляем.'
    elif kind=='clarify':reply=CLARIFY
    else:reply='Наличие у партнёра не подтверждено. Никому не отправляю запрос и не создаю обращение.'
    usage=dict(prompt_tokens=0,completion_tokens=0,llm_calls=0,tool_calls=len(trace),cost_rub=0)
    store.add_usage(sid,0,0,0,len(trace));meta=dict(cards=[],choices=[],events=events,usage=usage)
    store.add_message(sid,'assistant',reply,meta)
    return dict(session_id=sid,reply=reply,trace=trace,handoff_status=store.handoff_status(sid),**meta)
