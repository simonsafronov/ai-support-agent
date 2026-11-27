"""Offline visual harness, not imported by the application. See docs/tgdesk.md."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]


def fixture(tmp):
    # Never load a real .env or let project imports see inherited settings.
    if (ROOT / '.env').exists():
        raise SystemExit('Use a clean checkout without .env')
    os.environ.clear()
    os.environ.update(PATH='/usr/bin:/bin', HOME=str(tmp), APP_DB=str(tmp/'app.sqlite'),
                      CATALOG_DB=str(tmp/'catalog.sqlite'), KB_DB=str(tmp/'kb.sqlite'), CARDS_DB=str(tmp/'cards.sqlite'),
                      QUOTES_DIR=str(tmp/'quotes'), BRIDGE_DIR=str(tmp/'bridge'), FEED_URL='', TG_CHAT='',
                      TG_TOKEN='123456789:local-preview-only', TG_MANAGER_IDS='700000001', PRODUCT_CARDS='0')
    sys.path.insert(0, str(ROOT))
    from app import store
    from reportlab.pdfgen.canvas import Canvas
    sid = store.get_session('preview', 'fictional-browser')['id']
    store.add_message(sid, 'assistant', 'Подготовил предложение на 12 рабочих мест.')
    store.add_message(sid, 'user', 'Можно разделить оплату на два месяца?')
    hid = store.add_handoff(sid, 'Условия оплаты', 'Студия «Лист» согласует закупку лицензий.', 'example@example.invalid')
    quotes = tmp/'quotes'; quotes.mkdir(); number = 'КП-20261127-0042'; path = quotes/(number+'.pdf')
    pdf = Canvas(str(path)); pdf.drawString(70,700,'Local preview only'); pdf.save()
    store.add_quote(sid, number, 'Студия «Лист»', [], 100, str(path))
    d = {'auth_date':str(int(time.time())), 'user':json.dumps({'id':700000001}), 'query_id':'local_preview'}
    secret = hmac.new(b'WebAppData', os.environ['TG_TOKEN'].encode(), hashlib.sha256).digest()
    d['hash'] = hmac.new(secret, '\n'.join(f'{k}={d[k]}' for k in sorted(d)).encode(), hashlib.sha256).hexdigest()
    return hid, urlencode(d)


def browser(output):
    from playwright.sync_api import sync_playwright
    info = json.load(sys.stdin); origin = info['origin']
    with sync_playwright() as pw:
        # BROWSER-OK: isolated browser for our loopback-only fixture, no external requests.
        proc = pw.chromium.launch(executable_path='/usr/bin/chromium', args=['--disable-background-networking','--disable-sync'])
        ctx = proc.new_context(viewport={'width':390,'height':844}, device_scale_factor=2, locale='ru-RU', timezone_id='Europe/Moscow', service_workers='block')
        foreign = []; errors = []
        def local_only(route):
            if route.request.url.startswith(origin+'/'):
                route.continue_()
            else:
                foreign.append(route.request.url.split(':')[0]); route.abort()
        ctx.route('**/*', local_only)
        page = ctx.new_page(); page.on('pageerror', lambda exc: errors.append(str(exc)))
        url = origin + '/tgdesk?' + urlencode({'handoff':info['hid']})
        page.goto(url); page.get_by_text('Откройте обращение кнопкой', exact=False).wait_for()
        page.goto('about:blank'); page.goto(url+'#'+urlencode({'tgWebAppData':info['init']}))
        page.get_by_role('heading', name='Условия оплаты').wait_for()
        with page.expect_download() as dl:
            page.get_by_role('button', name='Скачать Коммерческое предложение', exact=False).click()
        assert dl.value.suggested_filename.endswith('.pdf')
        # Fresh authenticated launch, no stale download notification in the image.
        page.goto('about:blank'); page.goto(url+'#'+urlencode({'tgWebAppData':info['init']}))
        page.get_by_label('Ответ в чат клиента').fill('Уточню график платежей и вернусь с ответом сегодня.')
        page.get_by_label('Ответ в чат клиента').blur(); page.evaluate('window.scrollTo(0,0)'); page.evaluate('document.fonts.ready')
        assert '#' not in page.url and not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(output),full_page=True)
        page.set_viewport_size({'width':1024,'height':768})
        assert page.get_by_role('button',name='Отправить ответ').is_enabled() and not foreign and not errors
        proc.close()


def preview(output, browser_python):
    with tempfile.TemporaryDirectory(prefix='tgdesk-preview-') as directory:
        hid, raw = fixture(Path(directory))
        import uvicorn
        from app.web.server import app
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
            server=uvicorn.Server(uvicorn.Config(app,lifespan='off',log_level='error',access_log=False))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
            try:
                for _ in range(100):
                    if server.started: break
                    time.sleep(.05)
                if not server.started: raise RuntimeError('Local server did not start')
                subprocess.run([browser_python,'-IB',__file__,'--browser',str(output)],check=True,timeout=60,
                               input=json.dumps({'origin':origin,'hid':hid,'init':raw}),text=True)
            finally:
                server.should_exit=True; thread.join(10)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('output',type=Path); parser.add_argument('--browser-python',default=sys.executable)
    parser.add_argument('--browser',action='store_true'); args=parser.parse_args()
    if args.browser: browser(args.output)
    else: preview(args.output,args.browser_python)
