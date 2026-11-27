"""Synthetic deterministic flow CLI. No customer history, shared Qwen or external calls."""
import os,sys,tempfile,json,time,re
from pathlib import Path
import argparse, hashlib
parser=argparse.ArgumentParser(description='Standalone synthetic state-flow test; no model/network calls.')
parser.add_argument('--app',required=True,type=Path,help='isolated source snapshot containing app/')
parser.add_argument('--feed',required=True,type=Path,help='released tests/data/feed_small.xml')
parser.add_argument('--out',required=True,type=Path,help='new output directory')
a=parser.parse_args(); src=a.app.resolve();feed=a.feed.resolve();out=a.out.resolve()
if (src/'.env').exists() or not (src/'app/agent/compact.py').is_file():parser.error('isolated compact source without .env required')
if hashlib.sha256(feed.read_bytes()).hexdigest()!='33883d1eec491199f62f7a6d189f262fee688bfe23cd85265023298edee10af5':parser.error('expected pinned synthetic feed')
os.environ.clear();os.environ['PATH']='/usr/bin:/bin';os.umask(0o077);out.mkdir(exist_ok=False,parents=True);os.sched_setaffinity(0,{min(os.sched_getaffinity(0))});os.nice(15)
with tempfile.TemporaryDirectory() as z:
 d=Path(z);sys.path.insert(0,str(src));os.chdir(d)
 os.environ.update(COMPACT_LOCAL='1',LLM_PROVIDER='openai',LLM_BASE_URL='http://127.0.0.1:18347/v1',LLM_MODEL='qwen3-4b-local',LLM_API_KEY='',APP_DB=str(d/'app.sqlite'),KB_DB=str(d/'none.sqlite'),CATALOG_DB=str(d/'synthetic.sqlite'),QUOTES_DIR=str(d/'quotes'),BRIDGE_DIR=str(d/'bridge'),CARDS_DB=str(d/'cards.sqlite'),TG_TOKEN='',FEED_URL='',WEBIM_TOKEN='',PRODUCT_CARDS='0')
 def no_net(event,args):
  if event in ('socket.connect','socket.getaddrinfo','socket.bind','socket.sendto'):raise RuntimeError('network forbidden')
 sys.addaudithook(no_net)
 from app import store
 from app.catalog import ingest,search
 from app.agent import loop,compact_order
 ingest.build(feed,d/'synthetic.sqlite',verbose=False)
 class NoInference:
  def chat(self,*a,**kw):raise AssertionError('requires model')
 client='private-demo-offline'; sid=store.get_session(None,client)['id'];rows=[]
 def ask(family,q,expect,fn,why,other=None):
  start=time.monotonic(); r=loop.run_turn(other or sid,q,client_id=client,llm=NoInference());passed=bool(fn(r));rows.append(dict(family=family,prompt=q,expected=expect,actual=r['reply'],status='PASS' if passed else 'FAIL',reason=why if passed else 'Проверяемое условие не выполнено',seconds=round(time.monotonic()-start,4),tools=[t['name'] for t in r['trace'] if t['kind']=='tool']));return r
 ask('КП без исходных данных','Подготовьте КП','Запросить артикул и количество; не создавать пустой документ.',lambda r:'артикул' in r['reply'] and 'количество' in r['reply'] and not store.last_quote(sid),'Названы оба недостающих параметра, КП отсутствует.')
 # Isolated state fixture after a catalog lookup; do not claim that Qwen chose the product.
 s=store.find_session(sid);o=search.get_offer(1001);compact_order.bind_search(s['items'],[o],o.get('product_id'));store.update_session(sid,items=s['items']);sku=o['vendor_code']
 ask('Выбор артикула из показанных','Выбираю '+sku,'Подтвердить показанный артикул и спросить количество.',lambda r:sku in r['reply'] and 'Сколько' in r['reply'],'Артикул совпадает с учебной карточкой, задан один следующий вопрос.')
 ask('Неверная ценовая ступень','3 шт.','Не применять цену ступени 10–14 к 3 штукам.',lambda r:'вне диапазона' in r['reply'] and not store.last_quote(sid),'Неподходящая ступень отклонена; документ не создан.')
 ask('Коррекция количества','12 шт.','Сохранить исправленные 12 шт. именно выбранного артикула.',lambda r:store.find_session(sid)['items'].get('compact_qty')==12 and sku in r['reply'],'Проверены сохранённые количество и артикул.')
 ask('Цена и расчёт ступени','Рассчитайте 12 шт. '+sku,'Назвать правильный итог и ограничение ступени.',lambda r:'25800' in r['reply'] and '10 до 14' in r['reply'],'Итог сверён с учебной ценой 2150 × 12; условие ступени сохранено.')
 ask('КП по выбранной позиции','Подготовьте КП','Сформировать PDF на 12 шт. с верной суммой.',lambda r:bool(store.last_quote(sid)) and store.last_quote(sid)['total']==25800 and any(e['type']=='quote' for e in r['events']),'Запись, сумма и событие документа совпали.')
 ask('Демосчёт без реквизитов','Пришлите учебный счёт в этот чат','Запросить только недостающие данные и не выпустить счёт.',lambda r:'реквизиты' in r['reply'] and not store.last_invoice(sid),'Приложение объяснило, чего не хватает; документ не выпущен.')
 other=store.get_session(None,'other-private-demo')['id'];h=ask('Явный запрос оператора','Хочу поговорить со специалистом.','Передать в очередь, сообщить результат и попросить канал связи.',lambda r:bool(store.open_handoff(other)) and 'менеджер' in r['reply'] and 'почту' in r['reply'],'Одна заявка только в временной БД, ответ не обещает срок.',other)
 ask('Повтор во время ожидания','Соедините с менеджером, пожалуйста.','Не создавать второй тикет и объяснить, что просьба уже зарегистрирована.',lambda r:bool(store.open_handoff(other)) and not r['events'] and 'у менеджера' in r['reply'],'Второй заявки нет; в тексте подтверждён текущий статус.',other)
 groups=['missing_data','sku','tiers','correction_repeat','price','commercial_offer','demo_invoice','operator','waiting_operator'];defs=[];snaps=[];checks=[]
 for i,(row,family) in enumerate(zip(rows,groups),1):
  ident=f'flow-{i:02d}';defs.append(dict(id=ident,family=family,prompt=row['prompt'],expected=row['expected']))
  snap=dict(id=ident,suite='retail-v1-flow',mode='release-loop/no-inference',actual=row['actual'],seconds=row['seconds'],tools=row['tools']);snaps.append(snap)
  checks.append(dict(id=ident,snapshot_sha256=hashlib.sha256(json.dumps(snap,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),status=row['status'],reason=row['reason']))
 for name,data in [('flow-spec',defs),('flow-snapshot',snaps),('flow-verdict',checks)]: (out/(name+'.json')).write_text(json.dumps(data,indent=2,ensure_ascii=False))
 print(f'{sum(r["status"]=="PASS" for r in rows)}/{len(rows)} synthetic state-flow checks passed');sys.exit(int(any(r['status']!='PASS' for r in rows)))
