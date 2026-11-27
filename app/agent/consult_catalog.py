"""Read-only, bounded lookups for the compact consultation. No web calls or new index."""
import re
import sqlite3
from contextlib import closing

from app import config

# Buyer vocabulary -> capability phrases. A feature in the buyer text is NOT evidence
# that an offer has the feature. Both sides must match; ambiguous cases ask for clarification.
FEATURES = {
    'speech': (r'транскриб|транскрип|расшифров\w* (?:аудио|запис|реч)', r'транскриб|транскрип|расшифров\w* (?:аудио|запис|реч)'),
    'ocr': (r'распозна\w* (?:текст|скан)|\bocr\b', r'распозна\w*|\bocr\b'),
    'pdf_edit': (r'(?:править|редактир\w*|изменять)\b.{0,70}\bpdf\b|\bpdf\b.{0,45}(?:редактир|править)', r'редактир\w*.{0,35}(?:текст|pdf)|редактор pdf'),
    'pdf_form': (r'заполн\w*.{0,45}(?:поле|форм).{0,40}pdf|pdf.{0,45}заполн\w*.{0,20}(?:поле|форм)', r'заполн\w*.{0,50}(?:пол[ея]|форм)'),
}


def feature(text):
    return next((k for k,(needle,_) in FEATURES.items() if re.search(needle,text,re.I)),None)


def words(text):
    return re.findall(r'[\w-]+',text.casefold().replace('ё','е'))


def deployment(offer):
    evidence=text_of(offer)
    if re.search(r'требу\w* передач|\bоблачн|\bоблако\b|\bsaas\b', evidence): return 'cloud'
    if re.search(r'\bсервер\w*\b|on.prem', evidence): return 'server'
    if re.search(r'офлайн|без интернет|локальн', evidence): return 'offline'
    return None


def text_of(offer):
    return ' '.join(str(offer.get(k) or '') for k in ('product_title','name','description')).casefold().replace('ё','е')


def supported(offer, goal):
    text=text_of(offer)
    if not re.search(FEATURES[goal][1],text): return False
    # Negative statements cannot certify a capability. Prefer a missed recommendation.
    if re.search(r'(?:не (?:поддерживает|позволяет|умеет)|без (?:функции )?редактир|только просмотр)',text): return False
    return True


def _read(fn):
    try:
        with closing(sqlite3.connect(config.CATALOG_DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.row_factory=sqlite3.Row
            budget=iter(range(1600)); db.set_progress_handler(lambda: int(next(budget,None) is None),1000)
            return fn(db)
    except sqlite3.Error:
        return []  # Never treat an interrupted or incomplete scan as proof of absence.


def same_name_token(left, right):
    from app.kb.search import stem
    if left==right or (len(left)>=4 and len(right)>=4 and stem(left)==stem(right)):
        return True
    # Brand names can be parsed as verbs by a stemmer. A case ending appended to
    # the complete catalog token is stronger evidence than a fuzzy edit distance.
    return len(right)>=4 and re.fullmatch(re.escape(right)+r'(?:а|у|е|ом|ы|и)',left) is not None


def quoted_products(text):
    """Resolve each quoted catalog name independently. Prefix retrieval is only a shortlist;
    stems must then agree in order. An explicit unknown name must never reuse the old selection.
    """
    from app.kb.search import stem
    phrases = re.findall(r'[«"]([^»"\n]{2,100})[»"]', text)[:3]
    def read(db):
        groups = []
        for phrase in phrases:
            tokens = words(phrase)
            if not tokens or len(tokens)>12:
                groups.append((phrase, [])); continue
            terms = tuple(stem(w) for w in tokens)
            expr = ' AND '.join('"'+t+'"*' for t in terms if t)
            rows = db.execute('SELECT p.product_id,p.title FROM products_fts f JOIN products p ON p.product_id=f.product_id WHERE products_fts MATCH ? ORDER BY rank LIMIT 33',(expr,)).fetchall()
            if len(rows)>=33:
                groups.append((phrase, [])); continue
            near=[]; exact=[]
            for r in rows:
                name=words(r['title'])
                if len(name)>=len(tokens) and all(same_name_token(a,b) for a,b in zip(tokens,name)):
                    (exact if len(name)==len(tokens) else near).append(dict(r))
            groups.append((phrase,exact or near))
        return groups
    return _read(read) if phrases else []


def mentions(text):
    """Indexed candidate search; keep only titles/SKUs literally mentioned by this buyer."""
    tokens=[w for w in words(text) if len(w)>=3][:48]
    if not tokens:return []
    def read(db):
        from app.kb.search import stem
        expr=' OR '.join('"'+t+'"*' for t in dict.fromkeys(stem(w) for w in tokens) if t)
        rows=db.execute('SELECT p.product_id,p.title FROM products_fts f JOIN products p ON p.product_id=f.product_id WHERE products_fts MATCH ? ORDER BY rank LIMIT 48',(expr,)).fetchall()
        normalized=' '+' '.join(words(text))+' '
        exact=[dict(r) for r in rows if ' '+' '.join(words(r['title']))+' ' in normalized]
        if exact: return exact
        source=words(text)
        inflected=[]
        for r in rows:
            name=words(r['title']); n=len(name)
            if n and any(all(same_name_token(a,b) for a,b in zip(source[i:i+n],name)) for i in range(len(source)-n+1)):
                inflected.append(dict(r))
        if inflected: return inflected
        # A family name can precede the deployment choice. Allow only a literal
        # family whose remaining title part is an explicit deployment qualifier.
        families={}
        for row in rows:
            family=re.sub(r'\s+(?:облако|сервер|cloud|server)$','',row['title'],flags=re.I)
            if family!=row['title'] and len(family)>=4 and ' '+' '.join(words(family))+' ' in normalized:
                families.setdefault(family,[]).append(dict(row))
        if len(families)==1:
            name,variants=next(iter(families.items()))
            if len(variants)>1:return [{'family':name,'variants':variants}]
        return []
    return _read(read)


def discover(goal):
    """Small shortlist, never an exhaustive negative answer for a large catalog."""
    stems={'speech':('транскри','расшифров'),'ocr':('распознав','ocr'),'pdf_edit':('pdf',),'pdf_form':('pdf',)}[goal]
    def read(db):
        where=' OR '.join('(lower(name) LIKE ? OR description LIKE ?)' for _ in stems)
        params=[v for stem in stems for v in ('%'+stem+'%','%'+stem+'%')]
        # LIKE in stock SQLite doesn't casefold Cyrillic. Add the capitalized spelling.
        for stem in stems:
            where+=' OR name LIKE ? OR description LIKE ?';params.extend(['%'+stem.capitalize()+'%']*2)
        return [dict(r) for r in db.execute('SELECT id AS offer_id FROM offers WHERE '+where+' LIMIT 32',params)]
    return _read(read)


def variants(product_id):
    return _read(lambda db:[dict(r) for r in db.execute('SELECT id AS offer_id FROM offers WHERE product_id=? LIMIT 17',(str(product_id),))])


def siblings(title):
    # Same family only; never substitute an unrelated server or a similarly named vendor.
    stem=re.sub(r'\s+(?:облако|облачн\w*|сервер\w*|cloud|server)\s*$', '',title,flags=re.I)
    if not stem or stem==title: return []
    term=' AND '.join('"'+w+'"' for w in words(stem))
    return _read(lambda db:[dict(r) for r in db.execute('SELECT p.product_id,p.title FROM products_fts f JOIN products p ON p.product_id=f.product_id WHERE products_fts MATCH ? LIMIT 16',(term,)) if words(r['title'])[:len(words(stem))]==words(stem)])
