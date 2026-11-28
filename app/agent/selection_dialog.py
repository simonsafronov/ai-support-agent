"""Consume a short answer to the last shopping question. No inferred SKU or write actions."""
import re

from app.agent import compact_order, consult, task_selection

KEY='shopping_question'
PURPOSE='Для какой задачи нужна программа? Опишите, какой результат хотите получить.'
ASK_PURPOSE=re.compile(r'для какой задачи|что (?:именно )?хотите (?:с её помощью )?сделать|какой результат .*получить|что нужно сделать с помощью программы',re.I)
UNDECIDED=re.compile(r'(?=.*(?:не знаю|не понимаю|затрудняюсь|растерял\w*))(?=.*(?:выбрать|подобрать|нужн\w+ программ|покупать))',re.I)
SECURE=re.compile(r'антивирус|вредонос|вирус|(?:защит|обезопас|безопасност)\w*.{0,25}(?:комп|ноут|\bпк\b|устройств)|(?:ноут|комп|\bпк\b).{0,30}защит',re.I)
BACKUP=re.compile(r'б[эе]кап|backup|резервн\w* коп|копи\w* на случай (?:сбо|полом)',re.I)
PDF=re.compile(r'\b(?:pdf|пдф)\b',re.I)
CHANGE=re.compile(r'\b(?:лучше|теперь|передумал\w*|на самом деле)\b|[,;]\s*(?:(?:а|нуж\w*)\s+|(?=для\s))',re.I)
NO=re.compile(r'\b(?:не|нет|не нужен|отмен\w*)\b',re.I)


def note(items,reply,turn):
    if ASK_PURPOSE.search(reply) and KEY not in items:
        items[KEY]={'field':'purpose','turn':turn,'filters':{}}


def _ask(items,turn,field,filters,text):
    items[KEY]={'field':field,'turn':turn,'filters':filters}
    return text


def handle(request,items,tool,turn):
    prev=items.pop(KEY,None)
    if not prev or prev.get('turn')!=turn-1:prev={}
    s=request.casefold().strip(); field=prev.get('field'); shortlist=items.get(task_selection.KEY,{})
    if shortlist.get('turn')!=turn-1:shortlist={}
    if UNDECIDED.search(s) and not (SECURE.search(s) or PDF.search(s) or BACKUP.search(s)):
        return _ask(items,turn,'purpose',{},PURPOSE)
    if not field and not shortlist:return None
    if re.match(r'^(?:выбираю|беру|добавьте|артикул)\s',s) or compact_order.QUANTITY.fullmatch(s.rstrip('.! ')):
        return None  # an explicit SKU/quantity belongs to the existing selection flow
    # Business/support questions, concrete product names, and explicit actions go
    # through the ordinary router. A clarification must never intercept a document.
    if re.search(r'кп\b|коммерческ\w* предложен|\bсч[её]т|артикул|[«"]|статус заказ|менеджер|\bключ\w*\b|активац|поддержк|возврат|оплат',s):return None
    positive=CHANGE.split(s)[-1].strip(' ,:—') if NO.search(s) and CHANGE.search(s) else s
    negated=bool(NO.search(positive))
    new= 'protection' if SECURE.search(positive) else 'backup' if BACKUP.search(positive) else 'pdf' if PDF.search(positive) else None
    if negated and new:
        compact_order.bind_search(items,[]);items.pop(task_selection.KEY,None)
        return _ask(items,turn,'purpose',{},'Понял, это направление исключаю. Что нужно вместо него: работа с файлами, восстановление данных или другая задача?')
    if not new and shortlist:return None  # the numbered page owns its filter and selection replies
    if field and re.search(r'\b(?:отмена|передумал|закрыли вопрос)\b',s) and not new:return 'Хорошо, прекращаю подбор. Ничего не оформляю.'
    if not new and not field:return None
    filters=dict(prev.get('filters',{}));filters.update(task_selection._filters(positive))
    goal=new or field
    if new and field not in (None,'purpose',new):filters=task_selection._filters(positive)
    if new and shortlist:
        items.pop(task_selection.KEY,None);compact_order.bind_search(items,[])
    if goal=='protection':
        home=bool(re.search(r'домаш|личн\w*|для себя|для дома',positive)); work=bool(re.search(r'офис|организац|корпоратив|рабоч\w* (?:мест|комп|ноут)',positive))
        if home!=work:
            category='антивирусы для дома' if home else 'антивирусы для организаций'
            reply=task_selection.start_section(category,positive,items,tool,turn,filters)
            if reply:return 'Для защиты от вредоносных программ: '+reply
        if field=='protection' and not new:
            label=', '.join(str(v) for v in filters.values())
            return _ask(items,turn,goal,filters,'Сохранил задачу: защита устройства'+(' ('+label+')' if label else '')+'. Уточнение не разобрал. Для себя или для организации?')
        return _ask(items,turn,goal,filters,'Речь о защите устройства. Антивирус подбираем для личного компьютера или для рабочих мест организации? Если нужна не защита от вирусов, уточните угрозу.')
    if goal=='backup':
        reply=task_selection.start_section('резервного копирования',positive,items,tool,turn,filters)
        if reply:return reply
        return _ask(items,turn,goal,filters,'Для резервной копии важен источник данных. Что нужно восстанавливать: отдельные файлы, весь компьютер или сервер?')
    if goal=='pdf':
        if consult.cards.feature(positive) or re.search(r'заполн\w*|редакт\w*|править|изменить',positive):
            question=positive if 'pdf' in positive else positive+' в PDF'
            if 'platform' in filters:question+=' на '+filters['platform']
            answer=consult.handle(question,[],items,tool)
            if answer:return answer
        return _ask(items,turn,goal,filters,'Что требуется от PDF: заполнить готовое поле, изменить существующий текст или только открыть файл?')
    if field and any(re.search(pattern,s) for pattern,_ in task_selection.SECTIONS):return None
    return _ask(items,turn,field or 'purpose',filters,'Не понял последнее уточнение. Это про защиту устройства, копии данных, работу с документом или другую операцию? Ничего не оформляю.')
