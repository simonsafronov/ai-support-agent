"""Обход источников базы знаний и сборка SQLite с полнотекстовым поиском.

    python -m app.kb.crawl fetch    # обход с кешем сырых HTML в data/raw/kb/
    python -m app.kb.crawl build    # data/raw/kb/ -> data/db/kb.sqlite

Источники (заданы заказчиком):
  docs  — https://docs.softline.com/ru-ru, справочный центр «База Знаний Softline»
          (Nuxt, контент отдается сервером без JS; sitemap и robots.txt у портала нет,
          поэтому обход по ссылкам от главной);
  store — https://store.softline.ru/delivery_info/ и служебные страницы магазина
          из шапки и подвала. Каталог и карточки товаров не обходятся.

Темп: один поток, не чаще одного запроса в DELAY секунд к одному домену.
На 403/429 или страницу с проверкой на робота обход домена останавливается.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.kb.search import DB_PATH, index_text

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw" / "kb"
MANIFEST = RAW_DIR / "manifest.jsonl"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
DELAY = 2.0
BLOCK_STATUSES = {403, 429}
CHALLENGE_MAX_BYTES = 30_000  # страница проверки на робота короткая; обычные страницы от 380 КБ
BLOCK_TITLE_RE = re.compile(
    r"captcha|капча|вы не робот|are you a robot|access denied|доступ ограничен|"
    r"just a moment|ddos-guard|checking your browser",
    re.I,
)

DOCS_HOST = "docs.softline.com"
DOCS_START = "https://docs.softline.com/ru-ru"
DOCS_PREFIX = "/ru-ru"
DOCS_LIMIT = 400
# Порядок обхода разделов портала на случай, если страниц больше лимита: сначала разделы
# о покупках в магазине (личный кабинет, учетная запись), затем справка по другим сервисам.
DOCS_SECTION_RANK = {
    "lk": 0,
    "id": 1,
    "subscriptions": 2,
    "payment": 3,
    "checkout": 4,
    "allsoft": 5,
    "vendors": 6,
    "merchant-portal": 7,
}

STORE_HOST = "store.softline.ru"
STORE_BASE = "https://store.softline.ru"
STORE_LIMIT = 40
# Служебные страницы из шапки и подвала магазина (delivery_info и главная) и из
# sitemap/frontPages.xml. Каталог, производители, блог, корзина сюда не входят.
STORE_SEEDS = [
    "/delivery_info/",
    "/b2b/",
    "/tenders/",
    "/contacts/",
    "/about/",
    "/sale-rules/",
    "/recomendations_rules/",
    "/privacy-policy/",
    "/personal-data-consent/",
    "/intellectual-property/",
    "/specials/",
]
# Со служебных страниц переходим по ссылкам из основного контента, только внутри
# этих разделов (условия акций, подстраницы служебных разделов).
STORE_FOLLOW_PREFIXES = ("/b2b/", "/tenders/", "/contacts/", "/about/", "/delivery_info/", "/specials/")
STORE_FOLLOW_DEPTH = 2  # /specials/ -> /specials/?page=2 -> /specials/detail/...


class DomainBlocked(Exception):
    """Домен ответил отказом или проверкой на робота: обход домена прекращается."""


@dataclass
class Page:
    url: str
    status: int
    html: str
    final_url: str
    fetched_at: str
    from_cache: bool


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def load_manifest() -> dict[str, dict]:
    """Последняя запись по каждому url из manifest.jsonl."""
    records: dict[str, dict] = {}
    if MANIFEST.exists():
        with MANIFEST.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    records[rec["url"]] = rec
    return records


class Fetcher:
    """Последовательная загрузка с паузой на домен и кешем на диске."""

    def __init__(self, delay: float = DELAY) -> None:
        self.delay = delay
        self.manifest = load_manifest()
        self.last_request: dict[str, float] = {}
        self.client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            follow_redirects=True,
            timeout=40.0,
        )
        self.network_requests = 0

    def close(self) -> None:
        self.client.close()

    def _cached(self, url: str) -> Page | None:
        rec = self.manifest.get(url)
        if not rec or rec["status"] in BLOCK_STATUSES or rec["status"] <= 0 or rec["status"] >= 500:
            return None  # отказ, сетевая ошибка и 5xx при следующем запуске запрашиваются заново
        html = ""
        if rec.get("path"):
            path = ROOT / rec["path"]
            if not path.exists():
                return None
            html = path.read_text(encoding="utf-8", errors="replace")
        return Page(url, rec["status"], html, rec.get("final_url", url), rec["fetched_at"], True)

    def _record(self, url: str, status: int, path: Path | None, final_url: str, size: int) -> dict:
        rec = {
            "url": url,
            "status": status,
            "fetched_at": _now(),
            "path": str(path.relative_to(ROOT)) if path else None,
            "final_url": final_url,
            "bytes": size,
        }
        self.manifest[url] = rec
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with MANIFEST.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def _wait(self, host: str) -> None:
        last = self.last_request.get(host)
        if last is not None:
            pause = self.delay - (time.monotonic() - last)
            if pause > 0:
                time.sleep(pause)

    def get(self, url: str) -> Page:
        """Страница из кеша или из сети. DomainBlocked на 403/429/проверку на робота."""
        cached = self._cached(url)
        if cached:
            return cached
        host = urlsplit(url).netloc
        response = None
        error = ""
        for attempt in (1, 2):  # один повтор на сетевую ошибку или 5xx
            self._wait(host)
            self.last_request[host] = time.monotonic()  # пауза считается между началами запросов
            self.network_requests += 1
            try:
                response = self.client.get(url)
                error = ""
            except httpx.HTTPError as exc:
                response = None
                error = f"{type(exc).__name__}: {exc}"
            if response is not None and response.status_code < 500:
                break
            if attempt == 1:
                time.sleep(5.0)
        if response is None:
            rec = self._record(url, 0, None, url, 0)
            print(f"  сеть: {url}: {error}", file=sys.stderr)
            return Page(url, 0, "", url, rec["fetched_at"], False)

        status = response.status_code
        final_url = str(response.url)
        body = response.content
        html = body.decode(response.encoding or "utf-8", errors="replace")
        challenge = (status != 200 or len(body) < CHALLENGE_MAX_BYTES) and BLOCK_TITLE_RE.search(_title_of(html))
        if status in BLOCK_STATUSES or challenge:
            self._record(url, status if status in BLOCK_STATUSES else 403, None, final_url, len(body))
            raise DomainBlocked(f"{host}: статус {status}, заголовок «{_title_of(html)[:80]}»")
        path = None
        if status == 200 and urlsplit(final_url).netloc == host:
            path = RAW_DIR / host / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".html")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        rec = self._record(url, status, path, final_url, len(body))
        return Page(url, status, html if path else "", final_url, rec["fetched_at"], False)


def load_robots(fetcher: Fetcher, base: str) -> urllib.robotparser.RobotFileParser:
    """robots.txt домена; если файла нет, разрешено все."""
    parser = urllib.robotparser.RobotFileParser()
    page = fetcher.get(base + "/robots.txt")
    is_robots = page.status == 200 and "<html" not in page.html[:500].lower()
    parser.parse(page.html.splitlines() if is_robots else [])
    return parser


HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)""", re.I)


def page_links(html: str, base_url: str, host: str, keep_query: bool = False) -> list[str]:
    """Ссылки страницы на тот же домен, без якорей, в порядке появления."""
    seen: dict[str, None] = {}
    for href in HREF_RE.findall(html):
        href = href.strip().replace("&amp;", "&")
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        parts = urlsplit(urljoin(base_url, href))
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != host:
            continue
        path = parts.path or "/"
        seen[urlunsplit(("https", host, path, parts.query if keep_query else "", ""))] = None
    return list(seen)


DOCS_EMPTY_RE = re.compile(
    r'<main class="container">\s*(<!---->)?\s*</main>|<article[^>]*class="article"[^>]*>\s*(<!---->)?\s*</article>'
)


def is_docs_soft_404(html: str) -> bool:
    """Портал отдает 200 и на несуществующий адрес: заголовок пустой или «undefined», статьи нет."""
    title = _title_of(html).lower()
    return not title or title.startswith("undefined") or bool(DOCS_EMPTY_RE.search(html))


def crawl_docs(fetcher: Fetcher, limit: int = DOCS_LIMIT) -> dict:
    robots = load_robots(fetcher, f"https://{DOCS_HOST}")
    counter = 0
    queue: list[tuple[int, int, int, str]] = [(-1, 0, counter, DOCS_START)]
    seen = {DOCS_START}
    stats = {"found": 1, "fetched": 0, "ok": 0, "not_found": 0, "errors": 0, "left": 0, "blocked": ""}
    while queue and stats["fetched"] < limit:
        _, depth, _, url = heapq.heappop(queue)
        if not robots.can_fetch(USER_AGENT, url):
            continue
        try:
            page = fetcher.get(url)
        except DomainBlocked as exc:
            stats["blocked"] = str(exc)
            stats["left"] = len(queue) + 1
            return stats
        stats["fetched"] += 1
        if page.status != 200 or not page.html:
            stats["errors"] += 1
            continue
        if url != DOCS_START and is_docs_soft_404(page.html):
            stats["not_found"] += 1
            continue
        stats["ok"] += 1
        print(f"  docs {stats['fetched']:>3} {'кеш ' if page.from_cache else 'сеть'} {url}")
        for link in page_links(page.html, url, DOCS_HOST):
            link = link.rstrip("/")
            path = urlsplit(link).path
            if link in seen or not (path == DOCS_PREFIX or path.startswith(DOCS_PREFIX + "/")):
                continue
            if re.search(r"\.(xml|pdf|png|jpe?g|svg|zip|docx?|xlsx?)$", path, re.I):
                continue
            seen.add(link)
            stats["found"] += 1
            segments = path.split("/")
            section = segments[2] if len(segments) > 2 else ""
            counter += 1
            heapq.heappush(queue, (DOCS_SECTION_RANK.get(section, 8), depth + 1, counter, link))
    stats["left"] = len(queue)
    return stats


def is_store_service_url(url: str) -> bool:
    """Служебная страница магазина (в базу знаний идут только они)."""
    path = urlsplit(url).path
    return path in STORE_SEEDS or path.startswith(STORE_FOLLOW_PREFIXES)


def crawl_store(fetcher: Fetcher, limit: int = STORE_LIMIT) -> dict:
    robots = load_robots(fetcher, STORE_BASE)
    queue = [(STORE_BASE + path, 0) for path in STORE_SEEDS]
    seen = {url for url, _ in queue}
    stats = {"found": len(queue), "fetched": 0, "ok": 0, "errors": 0, "left": 0, "blocked": "", "robots_skipped": []}
    while queue and stats["fetched"] < limit:
        url, depth = queue.pop(0)
        if not robots.can_fetch(USER_AGENT, url):
            stats["robots_skipped"].append(url)
            continue
        try:
            page = fetcher.get(url)
        except DomainBlocked as exc:
            stats["blocked"] = str(exc)
            stats["left"] = len(queue) + 1
            return stats
        stats["fetched"] += 1
        if page.status != 200 or not page.html:
            stats["errors"] += 1
            print(f"  store {stats['fetched']:>2} статус {page.status} {url} -> {page.final_url}")
            continue
        stats["ok"] += 1
        print(f"  store {stats['fetched']:>2} {'кеш ' if page.from_cache else 'сеть'} {url}")
        if depth >= STORE_FOLLOW_DEPTH:
            continue
        content = extract(page.html, "store")
        # пагинация списка акций лежит вне текста страницы, ее ссылки берем из разметки
        pagination = re.findall(r'href="(/specials/\?page=\d+)"', page.html)
        for link in content.links + pagination:
            parts = urlsplit(urljoin(url, link))
            path = parts.path if parts.path.endswith("/") else parts.path + "/"
            # из адресов с параметрами нужна только пагинация списка акций
            query = parts.query if path == "/specials/" and re.fullmatch(r"page=\d+", parts.query) else ""
            if parts.netloc.lower() != STORE_HOST or (parts.query and not query):
                continue
            link = STORE_BASE + path + (f"?{query}" if query else "")
            if link in seen or not path.startswith(STORE_FOLLOW_PREFIXES):
                continue
            if re.search(r"\.[a-z0-9]{2,5}/$", path, re.I):  # файлы, не страницы
                continue
            seen.add(link)
            stats["found"] += 1
            queue.append((link, depth + 1))
    stats["left"] = len(queue)
    return stats


# --------------------------------------------------------------------------- #
# Разбор HTML: основной контент страницы в виде блоков (заголовок, абзац, таблица)
# --------------------------------------------------------------------------- #

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_TAGS = {
    "p", "div", "ul", "ol", "li", "section", "article", "blockquote", "pre", "dl", "dt", "dd",
    "figure", "figcaption", "details", "summary", "address", "hr",
}
SKIP_TAGS = {
    "script", "style", "noscript", "svg", "form", "nav", "footer", "header", "aside",
    "button", "select", "textarea", "iframe", "template",
}
SPOILER_LEVEL = 5  # вопрос в раскрывающемся блоке (FAQ) считается подзаголовком
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "track"}

PROFILES = {
    # containers: (тег, класс) основного контента по убыванию приоритета, берется первый
    # непустой; skip: классы блоков, которые не контент.
    "docs": {
        # статья; на страницах разделов статьи нет, там полезен блок контактов раздела
        "containers": [("article", "article"), ("div", "contacts-info")],
        "skip": {"feedback", "article-nav", "article__nav", "related", "toc", "article-toc"},
        "crumbs": "breadcrumbs__outer",
        "title_suffix": " — База Знаний Softline",
    },
    "store": {
        "containers": [("main", "b-page__main")],
        "skip": {
            "b-pure-content-navigation", "b-breadcrumbs", "breadcrumbs", "b-sidebar-nav",
            "b-social-share", "b-modal", "b-popup",
            # подписка на рассылку, слайдер партнеров, отзывы, пометка «Реклама», пагинация, кнопки
            "b-give-insight", "b-brands-slider", "b-reviews-slider", "b-teaser", "b-teaser-popup",
            "b-store-pagination", "button",
            # на странице акции: карточки товаров с ценами и карусель других акций
            "b-page-actions-detailed__goods", "b-page-actions-detailed__more",
        },
        "crumbs": "",
        "title_suffix": "",
    },
}


@dataclass
class Content:
    title: str = ""
    crumbs: list[str] = field(default_factory=list)
    blocks: list[tuple[str, int, str]] = field(default_factory=list)  # (kind h|p|t, level, text)
    links: list[str] = field(default_factory=list)  # href из основного контента
    nav_share: float = 0.0  # доля текста внутри ссылок на страницы того же сайта


_INVISIBLE = dict.fromkeys((0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD))  # нулевой ширины, мягкий перенос


def _clean(text: str) -> str:
    """Текст блока: без невидимых символов и лишних пробелов; переводы строк остаются."""
    text = text.replace("\xa0", " ").translate(_INVISIBLE)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(r" +([,.;:!?»)])", r"\1", text)


class _Extractor(HTMLParser):
    def __init__(self, profile: dict, container: tuple[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.profile = profile
        self.container = container
        self.content = Content()
        self.in_container = False
        self.container_tag = ""
        self.container_depth = 0
        self.done = False
        self.skip_tag = ""
        self.skip_depth = 0
        self.in_crumbs = False
        self.in_crumb = False
        self.buf: list[str] = []
        self.plain_len = 0  # знаков текста вне ссылок-якорей «#...»
        self.heading_level = 0
        self.prefix = ""
        self.anchor_local = False
        self.anchor_site = False
        self.text_chars = 0
        self.site_link_chars = 0
        self.table_depth = 0
        self.rows: list[str] = []
        self.cells: list[str] = []
        self.in_title = False
        self.title_buf: list[str] = []
        self.pre_depth = 0

    # -- служебное ---------------------------------------------------------- #
    def _flush(self) -> None:
        text = _clean("".join(self.buf))
        plain = self.plain_len
        self.buf, self.plain_len = [], 0
        if not text:
            return
        prefix, self.prefix = self.prefix, ""
        if self.table_depth:
            self.cells.append(text.replace("\n", " "))
            return
        if self.heading_level:
            self.content.blocks.append(("h", self.heading_level, text.replace("\n", " ")))
            return
        if plain == 0:  # блок целиком из ссылок-якорей: оглавление, «наверх»
            return
        self.content.blocks.append(("p", 0, prefix + text))

    def _end_row(self) -> None:
        self._flush()
        if self.cells:
            self.rows.append(" | ".join(self.cells))
        self.cells = []

    # -- события разбора ---------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self.title_buf and not self.in_container:
            self.in_title = True  # первый <title> страницы; у svg-иконок бывают свои
            return
        if self.done:
            return
        classes = set((dict(attrs).get("class") or "").split())
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            if self.in_crumbs and any(c.startswith("breadcrumbs__link") for c in classes):
                self.in_crumb = True
                self.content.crumbs.append("")
            return
        if not self.in_container:
            want_tag, want_class = self.container
            if tag == want_tag and want_class in classes:
                self.in_container, self.container_tag, self.container_depth = True, tag, 1
            return
        if tag == self.container_tag:
            self.container_depth += 1
        if tag in VOID_TAGS:
            if tag == "br":
                self.buf.append("\n")
            elif tag == "hr":
                self._flush()
            return
        if tag == "button" and any(c.startswith("spoiler__trigger") for c in classes):
            self._flush()
            self.heading_level = SPOILER_LEVEL
            return
        is_crumbs = bool(self.profile["crumbs"]) and self.profile["crumbs"] in classes
        if tag in SKIP_TAGS or is_crumbs or classes & self.profile["skip"]:
            self._flush()
            self.skip_tag, self.skip_depth, self.in_crumbs = tag, 1, is_crumbs
            return
        if tag in HEADINGS:
            self._flush()
            self.heading_level = HEADINGS[tag]
        elif tag == "table":
            self._flush()
            self.table_depth += 1
        elif tag == "tr":
            self._end_row()
        elif tag in ("td", "th"):
            self._flush()
        elif tag == "li":
            self._flush()
            self.prefix = "- "
        elif tag in BLOCK_TAGS:
            self._flush()
            self.pre_depth += tag == "pre"
        elif tag == "span" and classes:
            self.buf.append(" ")  # соседние оформленные span: «заголовок» + «описание» карточки
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            self.anchor_local = href.startswith("#")
            self.anchor_site = href.startswith("/") and not href.startswith("//")
            if href and not self.anchor_local:
                self.content.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
            return
        if self.done:
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag, self.in_crumbs, self.in_crumb = "", False, False
            return
        if not self.in_container:
            return
        if tag in HEADINGS or (tag == "button" and self.heading_level == SPOILER_LEVEL):
            self._flush()
            self.heading_level = 0
        elif tag == "table":
            self._end_row()
            self.table_depth = max(0, self.table_depth - 1)
            if self.table_depth == 0 and self.rows:
                self.content.blocks.append(("t", 0, "\n".join(self.rows)))
                self.rows = []
        elif tag == "tr":
            self._end_row()
        elif tag in ("td", "th"):
            self._flush()
        elif tag == "li":
            self._flush()
            self.prefix = ""
        elif tag in BLOCK_TAGS:
            self._flush()
            if tag == "pre":
                self.pre_depth = max(0, self.pre_depth - 1)
        elif tag == "span":
            self.buf.append(" ")
        elif tag == "a":
            self.anchor_local = self.anchor_site = False
        if tag == self.container_tag:
            self.container_depth -= 1
            if self.container_depth == 0:
                self._flush()
                self.in_container, self.done = False, True

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_buf.append(data)
        elif self.skip_tag:
            if self.in_crumbs and self.in_crumb:
                self.content.crumbs[-1] += data
        elif self.in_container:
            # перевод строки в исходнике не разрыв абзаца: разрывы дают только <br> и <pre>
            self.buf.append(data if self.pre_depth else re.sub(r"\s+", " ", data))
            size = len(re.sub(r"[\W_]+", "", data))
            if not self.anchor_local:
                self.plain_len += size
            if not self.heading_level:
                self.text_chars += size
                self.site_link_chars += size if self.anchor_site else 0


def extract(html: str, source: str) -> Content:
    """Основной контент страницы: заголовок, хлебные крошки, блоки текста, ссылки."""
    profile = PROFILES[source]
    for container in profile["containers"]:
        parser = _Extractor(profile, container)
        parser.feed(html)
        parser.close()
        if any(kind != "h" for kind, _, _ in parser.content.blocks):
            break
    content = parser.content
    content.nav_share = parser.site_link_chars / parser.text_chars if parser.text_chars else 0.0
    content.crumbs = [c for c in (_clean(c).replace("\n", " ") for c in content.crumbs) if c]
    h1 = next((text for kind, level, text in content.blocks if kind == "h" and level == 1), "")
    page_title = _clean("".join(parser.title_buf)).replace("\n", " ")
    if profile["title_suffix"] and page_title.endswith(profile["title_suffix"]):
        page_title = page_title[: -len(profile["title_suffix"])]
    content.title = h1 or page_title
    return content


# --------------------------------------------------------------------------- #
# Нарезка на куски
# --------------------------------------------------------------------------- #

CHUNK_MIN = 600
CHUNK_MAX = 1200
SENTENCE_RE = re.compile(r"(?<=[.!?…;])\s+")


def _split_long(text: str, limit: int, repeat_first_line: bool = False) -> list[str]:
    """Блок длиннее limit режется по строкам, затем по предложениям, затем по словам."""
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    head = lines[0] if repeat_first_line and len(lines) > 2 and len(lines[0]) < limit // 3 else ""
    units: list[str] = []
    for line in lines[1:] if head else lines:
        if len(line) <= limit:
            units.append(line)
            continue
        for sentence in SENTENCE_RE.split(line):
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                units.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                units.append(sentence)
    parts: list[str] = []
    current = head
    for unit in units:
        if current and len(current) + 1 + len(unit) > limit:
            if current != head:
                parts.append(current)
            current = head if len(head) + 1 + len(unit) <= limit else ""
        current = f"{current}\n{unit}" if current else unit
    if current and current != head:
        parts.append(current)
    return parts


def _pack(paragraphs: list[tuple[str, str]]) -> list[str]:
    """Абзацы одного раздела -> куски до CHUNK_MAX знаков по границам абзацев.

    Раздел длиннее CHUNK_MAX делится на ровные части, а не на «полный кусок и хвост».
    """
    pieces: list[str] = []
    for kind, text in paragraphs:
        pieces.extend(_split_long(text, CHUNK_MAX, repeat_first_line=(kind == "t")))
    total = sum(len(piece) + 1 for piece in pieces) - 1
    target = total / math.ceil(total / CHUNK_MAX)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and (len(current) + 1 + len(piece) > CHUNK_MAX or len(current) >= target):
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def chunk_content(content: Content) -> list[tuple[str, str]]:
    """[(heading, text)]: heading — цепочка «раздел > заголовок страницы > подзаголовки»."""
    result: list[tuple[str, str]] = []
    chain: list[tuple[int, str]] = []
    paragraphs: list[tuple[str, str]] = []

    def heading() -> str:
        names = list(content.crumbs)
        if not chain or chain[0][0] != 1:
            names.append(content.title)
        names.extend(text for _, text in chain)
        unique: list[str] = []
        for name in names:
            if name and name.lower() not in (u.lower() for u in unique):
                unique.append(name)
        return " > ".join(unique)

    def emit() -> None:
        if paragraphs:
            for text in _pack(paragraphs):
                if len(text) >= 20:
                    result.append((heading(), text))
            paragraphs.clear()

    for kind, level, text in content.blocks:
        if kind == "h":
            emit()
            while chain and chain[-1][0] >= level:
                chain.pop()
            chain.append((level, text))
        else:
            paragraphs.append((kind, text))
    emit()
    return result


# --------------------------------------------------------------------------- #
# Сборка базы
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE docs(
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('docs', 'store')),
    fetched_at TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE chunks(
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    heading TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX chunks_doc_id ON chunks(doc_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    heading, text, tokenize = 'unicode61 remove_diacritics 2'
);
"""

SOURCES = {DOCS_HOST: "docs", STORE_HOST: "store"}
NAV_SHARE_MAX = 0.7  # страница docs, где текст почти целиком из ссылок на статьи, это оглавление


def build(db_path: Path = DB_PATH) -> dict:
    """Сырые HTML из manifest.jsonl -> SQLite (docs, chunks, chunks_fts)."""
    manifest = load_manifest()
    if not manifest:
        raise SystemExit(f"Нет {MANIFEST}: сначала python -m app.kb.crawl fetch")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(".sqlite.tmp")
    tmp_path.unlink(missing_ok=True)
    db = sqlite3.connect(tmp_path)
    db.executescript(SCHEMA)
    stats = {"docs": 0, "chunks": 0, "by_source": {"docs": 0, "store": 0}, "skipped": []}
    seen_texts: set[str] = set()
    for url, rec in manifest.items():
        source = SOURCES.get(urlsplit(url).netloc)
        if not source or rec["status"] != 200 or not rec.get("path") or url.endswith("/robots.txt"):
            continue
        if source == "store" and not is_store_service_url(url):
            continue
        path = ROOT / rec["path"]
        if not path.exists():
            stats["skipped"].append((url, "нет файла"))
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        if source == "docs" and is_docs_soft_404(html):
            stats["skipped"].append((url, "страница не найдена (soft 404)"))
            continue
        content = extract(html, source)
        if source == "docs" and content.nav_share > NAV_SHARE_MAX:
            stats["skipped"].append((url, "оглавление раздела (одни ссылки)"))
            continue
        chunks = chunk_content(content)
        text = "\n".join(t for _, _, t in content.blocks)
        digest = hashlib.sha1(text.encode()).hexdigest()
        if not chunks or digest in seen_texts:
            stats["skipped"].append((url, "нет текста" if not chunks else "дубль"))
            continue
        seen_texts.add(digest)
        doc_id = db.execute(
            "INSERT INTO docs(url, title, source, fetched_at, text) VALUES (?, ?, ?, ?, ?)",
            (url, content.title or url, source, rec["fetched_at"], text),
        ).lastrowid
        for heading, chunk_text in chunks:
            chunk_id = db.execute(
                "INSERT INTO chunks(doc_id, heading, text) VALUES (?, ?, ?)", (doc_id, heading, chunk_text)
            ).lastrowid
            # в индекс идут стемы (нижний регистр, ё -> е): токенизатор FTS5 русскую
            # морфологию и ё не сводит; исходный текст лежит в chunks
            db.execute(
                "INSERT INTO chunks_fts(rowid, heading, text) VALUES (?, ?, ?)",
                (chunk_id, index_text(heading), index_text(chunk_text)),
            )
            stats["chunks"] += 1
        stats["docs"] += 1
        stats["by_source"][source] += 1
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp_path, db_path)
    stats["bytes"] = db_path.stat().st_size
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.kb.crawl", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="обход источников с кешем сырых HTML")
    fetch.add_argument("--only", choices=["docs", "store"], help="обойти только один источник")
    fetch.add_argument("--docs-limit", type=int, default=DOCS_LIMIT)
    fetch.add_argument("--store-limit", type=int, default=STORE_LIMIT)
    sub.add_parser("build", help="собрать data/db/kb.sqlite из сырых HTML")
    args = parser.parse_args(argv)

    if args.command == "fetch":
        fetcher = Fetcher()
        report = {}
        try:
            if args.only in (None, "store"):
                report["store"] = crawl_store(fetcher, args.store_limit)
            if args.only in (None, "docs"):
                report["docs"] = crawl_docs(fetcher, args.docs_limit)
        finally:
            fetcher.close()
        report["network_requests"] = fetcher.network_requests
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    stats = build()
    skipped = stats.pop("skipped")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    for url, reason in skipped:
        print(f"  пропущено: {url}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
