"""Compact multi-line quote: reconcile every row with spec.match before issuing one draft."""
import re
from collections import Counter

from app.agent import spec
from app.catalog import search as catalog
from app.quote import kp

_STATE = 'compact_quote_list'
_HEADER = re.compile(r'(?:прошу|нуж(?:но|ен)|пришлите|составьте|сформируйте|подготовьте)?\s*'
                     r'(?:кп|коммерческое предложение)(?:\s+(?:по|на)\s+(?:(?:этот|этому|следующему)\s+)?'
                     r'(?:списк\w*|позици\w*|спецификаци\w*))?\s*:?', re.I)
_EDIT = re.compile(r'(?:строк[ауе]|позици[яию])\s*№?\s*(\d{1,3})\s*[:—–]\s*(.+)', re.I)
_ISSUE = 'Сформируйте КП по проверенному списку'
# Require a purchase count: seats, months, release numbers and digits in a SKU describe the offer.
_COUNT = re.compile(r'(?<![\w.,/+-])([0-9]{1,6})\s*(?:шт(?:ук[аи]?)?|экз(?:емпляр(?:а|ов)?)?|копи[юий]|лицензи[йиюя]+)\.?\s*$', re.I)
_TIMES = re.compile(r'(?<!\S)[xх×]\s*([0-9]{1,6})\s*$', re.I)
_COLUMN = re.compile(r'(?:\t|\s[—–-]\s|,\s+|[;|]|\bкол(?:ичество|-во)\s*[:=])\s*([0-9]{1,6})\s*$', re.I)
_BULLET = re.compile(r'^\s*(?:\d{1,3}[.)]\s*|[-•*]\s+)')
_COUNT_LABEL = re.compile(r'(?:^|[\s,;|])кол(?:ичество|-во)\s*[:=]?\s*$', re.I)
_VERSION = re.compile(r'(?:версия|редакция|релиз|version|release|v\.)\s*$', re.I)
_WORD = re.compile(r'\d+(?:[.,]\d+)+|\w+', re.UNICODE)


def _parse_row(line):
    text = _BULLET.sub('', line.strip())
    count = _COUNT.search(text) or _TIMES.search(text) or _COLUMN.search(text)
    if count and count.re is _COLUMN and _VERSION.search(text[:count.start()]):
        count = None
    qty = int(count[1]) if count else None
    title = _COUNT_LABEL.sub('', text[:count.start()]).rstrip(' ,;:-–—|') if count else text
    # Reuse spec's SKU recognition, not its preview-only assumption of quantity one.
    title = spec._LABEL.sub('', title).strip()
    invalid = len(text) > spec.LINE_MAX or len(title) > 160 or not re.search('[а-яёa-z]', title, re.I)
    return {'line': text[:spec.LINE_MAX], 'query': '' if invalid else title,
            'article': next(iter(spec._articles(title)), ''), 'qty': qty if qty and qty <= kp.MAX_QTY else None}


def _conflict(row, hit):
    code = hit['vendor_code'].casefold()
    query = row['query'].casefold()
    contained = re.search(r'(?<![\w/-])' + re.escape(code) + r'(?![\w/-])', query) if code else None
    if not contained:
        return True
    supplied = query[:contained.start()] + ' ' + query[contained.end():]
    words = lambda s: set(_WORD.findall(s.casefold().replace('ё', 'е')))
    # Join the exact offer back to its parent, never enrich from another search hit
    # or from all variants of the family (which would accept the wrong edition).
    offer = catalog.get_offer(hit['offer_id'])
    if (offer.get('vendor_code') or '').casefold() != code:
        return True
    identity = ' '.join(str(offer.get(k) or '') for k in
                        ('product_title', 'name', 'vendor', 'version', 'platform', 'license_type'))
    return not words(supplied) <= words(identity)


def _review(rows):
    # Rows that couldn't be parsed are kept in place; no partial list can look complete.
    parsed = [(n, r) for n, r in enumerate(rows, 1) if r['query']]
    matches = spec.match([r for _, r in parsed])['lines'] if parsed else []
    indexed = {n: h for (n, _), h in zip(parsed, matches, strict=True)}
    repeated = Counter(h.get('offer_id') for h in matches if h['status'] == spec.EXACT)
    display, ready = [], []
    for n, row in enumerate(rows, 1):
        hit = indexed.get(n)
        if not hit:
            status = 'не разобрано; пришлите название или артикул'
        elif hit['status'] != spec.EXACT:
            status = hit['status'] + (': ' + hit['hint'] if hit.get('hint') else '')
            if hit['status'] == spec.CHECK and hit.get('vendor_code'):
                status += f"; кандидат {hit['vendor_code']} ({hit['name']}) — не выбран"
        elif _conflict(row, hit):
            status = 'артикул и название не совпадают; уточните редакцию и артикул'
        elif not hit.get('price'):
            status = 'цену нужно подтвердить'
        elif repeated[hit['offer_id']] > 1:
            status = 'артикул повторяется; уточните отдельные строки или пришлите новый список'
        else:
            status = f"{spec.EXACT}: {hit['name']} ({hit['vendor_code']})"
            if hit.get('available') is False:
                status += '; ' + kp.FEED_UNAVAILABLE + '; поставку нужно подтвердить'
            if row['qty']:
                ready.append({'offer_id': hit['offer_id'], 'qty': row['qty']})
        count = f"{row['qty']} шт." if row['qty'] else 'уточните количество в шт.'
        display.append(f"{n}. {row['line']} → {status}; {count}")
    return '\n'.join(display), ready


def handle(request, items, tool):
    """Sanitized buyer input; caller persists items and dispatches existing make_quote tool."""
    lines = [s.strip() for s in request.splitlines() if s.strip()]
    if len(lines) == 1 and ';' in request:
        header, sep, rest = request.partition(':')
        if sep and _HEADER.fullmatch(header.strip()):
            lines = [header.strip(), *(s.strip() for s in rest.split(';'))]
    state = items.get(_STATE)
    issue = request.strip().rstrip('.!').casefold() == _ISSUE.casefold()
    if len(lines) >= 3 and _HEADER.fullmatch(lines[0]):
        if len(lines)-1 > spec.MAX_ROWS:
            items.pop(_STATE, None)
            return f'В одном КП можно проверить до {spec.MAX_ROWS} строк. КП не сформировано.'
        for n, line in enumerate(lines[1:], 1):
            marker = re.match(r'^(\d{1,3})[.)]\s+', line)
            if marker and int(marker[1]) != n:
                items.pop(_STATE, None)
                return 'Нумерация строк неоднозначна. Пришлите весь список с номерами от 1 по порядку. КП не сформировано.'
        state = items[_STATE] = {'rows': [_parse_row(line) for line in lines[1:]]}
    elif state and (edit := _EDIT.fullmatch(request.strip())):
        n = int(edit[1])
        if not 1 <= n <= len(state['rows']):
            return f"В списке {len(state['rows'])} строк. Укажите существующий номер строки."
        state['rows'][n-1] = _parse_row(edit[2])
        state.pop('issued', None)
    elif not (state and issue):
        return None
    elif state.get('issued'):
        return f"Черновик {state['issued']} по этому списку уже сформирован. Заказ не создан."

    details, ready = _review(state['rows'])
    if len(ready) != len(state['rows']):
        return (f'КП не сформировано: нужно уточнить список.\n{details}\n'
                'Исправьте только нужную строку: «Строка N: артикул и название — количество шт.». Остальные останутся в списке.')
    if not issue:
        return f'Все строки проверены:\n{details}\nЕсли всё верно, напишите «{_ISSUE}».'
    quote = tool('make_quote', {'items': ready})
    if not quote.get('ok'):
        return 'КП по списку не сформировано. Позиции сохранены для проверки.'
    state['issued'] = quote['number']
    result = f"Черновик {quote['number']} готов по всем {len(ready)} строкам. Заказ не создан."
    if any(o.get('available') is False for o in quote.get('items', [])):
        result += ' Есть позиции не в наличии по фиду; поставку нужно подтвердить.'
    return result
