"""Named product questions: bind the subject first, then retrieve the requested property.
Model prose, an incidental KB hit or a sibling edition cannot establish a product right.
"""
import re
from urllib.parse import urlsplit

from app.agent import consult_catalog as catalog
from app.catalog import search
from app.kb.search import buyer_source, stem
from app.agent.compact_knowledge import _public_copy

# Intent labels describe properties, not benchmark products or expected answers.
PROPERTIES = (
    ('upgrade', r'докуп|расширить|добавить.{0,20}(?:мест|пользовател)', 'расширение лицензии', r'расширени|докуп|добавлен'),
    ('renew', r'продл|пролонг', 'продление подписки', r'продлен|продли'),
    ('memory', r'оперативн\w* памят|\bозу\b|системн\w* требован|минимум.{0,15}(?:гб|gb)', 'системные требования память', r'памят|\bозу\b|требован'),
    ('compat', r'откро\w*|совместим|без потери|сохраня\w* сло|импорт\w* формат', 'совместимость форматов', r'совместим|открыва|импорт|формат|сло[еёя]'),
    ('compare', r'сравн|отлич|разниц', 'сравнение возможностей', r'печати|экспорт|формат|цвет|функци'),
    ('plans', r'тариф|в какой редакции', 'тарифы различия', r'тариф|включ[её]н|шаблон'),
    ('stock', r'в наличии|коробочн|физическ\w* носител', 'форма поставки', r'коробочн|электронн|носител'),
)


def property_of(text):
    return next((p for p in PROPERTIES if re.search(p[1],text,re.I)),None)


def _tokens(text):
    return tuple(stem(w) for w in catalog.words(text))


def _has(subject, text):
    tokens=_tokens(text); n=len(subject)
    return n and any(tokens[i:i+n]==subject for i in range(len(tokens)-n+1))


def _official(item,title,pattern):
    try: addr=urlsplit(item.get('url',''))
    except ValueError: return False
    snippet=item.get('snippet','')
    return (all(isinstance(item.get(k),str) for k in ('heading','title','snippet'))
            and buyer_source(item.get('url'),item.get('source'),item['heading']) and _public_copy(item)
            and addr.scheme=='https' and addr.netloc in ('store.softline.ru','docs.softline.com')
            and not re.search(r'[\s<>]',addr.geturl()) and snippet and len(snippet)<700
            and not snippet.rstrip().endswith(('…','...'))
            and _has(_tokens(title),item.get('title','')+' '+item.get('heading','')+' '+snippet)
            and re.search(pattern,snippet,re.I))


def _material(product,prop,tool):
    """Only material about this product AND property can reach reply and browser trace."""
    name=product['title']; key,_,terms,pattern=prop
    rows=tool('kb_search',{'query':name+' '+terms,'buyer_help':True},
              product_filter=lambda row:_official(row,name,pattern)).get('results',[])
    pieces=[]
    if rows:
        hit=rows[0];pieces.append((hit['snippet'],'Источник: '+hit['url']))
    data=tool('get_product',{'product_id':product['product_id']}); offers=data.get('offers') or []
    # Keep the prose tied to its SKU. Never synthesize a product-wide promise from one SKU.
    for entry in offers[:3]:
        o=search.get_offer(entry['offer_id']); desc=o.get('description') or ''
        if o.get('product_id')!=product['product_id'] and str(o.get('product_id'))!=str(product['product_id']):continue
        if not 0<len(desc)<=480 or not re.search(pattern,desc,re.I):continue
        source='Карточка '+str(o.get('vendor_code') or o['name'])
        if key=='stock':source+=' — '+('по фиду в наличии' if o['available'] else 'по фиду нет в наличии')
        pieces.append((desc,source));break
    return pieces


def reply(text,groups,prop,tool):
    key=prop[0]; flat=[]
    for phrase,products in groups:
        if not products:return f'Не удалось однозначно найти «{phrase}». Какой у него артикул или полное название с редакцией?'
        if len(products)>1 and key!='plans':return 'Уточните редакцию: '+', '.join(p['title'] for p in products[:4])+'.'
        flat.extend(products)
    if not flat:return None
    if key=='compare' and len(flat)<2:return f'С каким вторым продуктом и по какому свойству сравнить {flat[0]["title"]}?'
    if len(flat)>4:return 'Нашёл несколько редакций. Какие из них сравнить?'
    lines=[]; snippets=[]; seen=set()
    for product in flat:
        evidence=_material(product,prop,tool)
        snippets.extend(s for s,_ in evidence)
        unique=[entry for entry in evidence if entry not in seen];seen.update(unique)
        detail='\n'.join(s+'\n'+src for s,src in unique)
        lines.append(product['title']+': '+ (detail or ('см. общий источник выше.' if evidence else 'в проверенных материалах это свойство не подтверждено.')))
    extra={
        'renew':'Для проверки продления именно вашей подписки нужен номер подписки или заказа, не лицензионный ключ. Добавление мест — отдельная операция.',
        'upgrade':'Речь о дополнительных местах к действующей лицензии. Совпадение дат окончания и доплату нужно подтвердить по номеру подписки или заказа; не присылайте ключ.',
        'compat':'Для требуемой редакции проверьте открытие копии файла и сохранность нужных элементов до покупки.',
        'compare':'Отсутствие свойства в описании не доказывает, что продукт его не поддерживает. Неперечисленные различия не подтверждены.',
        'plans':'Порядок старшинства и переход между тарифами не предполагаю без таблицы производителя.',
        'stock':'Электронная и коробочная поставки — разные позиции. Если коробочная позиция не показана, её наличие не подтверждено; срок и резерв не обещаю.',
        'memory':'Для какой версии и платформы проверяем требования? Минимум для запуска не гарантирует комфортную скорость работы.',
    }[key]
    if key=='memory':
        ram=re.findall(r'(\d+(?:[.,]\d+)?)\s*(?:гб|gb)\b',text,re.I)
        mins={v for snippet in snippets for v in re.findall(r'(?:миним\w*|не менее)[^.;\d]{0,38}(\d+(?:[.,]\d+)?)\s*(?:гб|gb)\b',snippet,re.I)}
        if len(mins)==len(ram)==1:
            minimum=mins.pop(); meets=float(ram[0].replace(',','.'))>=float(minimum.replace(',','.'))
            extra=f'По объёму памяти {ram[0]} ГБ '+('соответствует' if meets else 'не соответствует')+f' указанному минимуму {minimum} ГБ. '+extra
    return '\n'.join([*lines,extra])
