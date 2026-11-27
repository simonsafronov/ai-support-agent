'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => Object.assign(document.createElement(tag), {textContent:text, className:cls || ''});
  const handoff = new URLSearchParams(location.search).get('handoff');
  const base = `/api/tgdesk/handoffs/${handoff}`;
  // Native Telegram launches the WebView with tgWebAppData in the fragment. It is already
  // the signed query string after ONE outer decoding; do not parse/serialize its user JSON.
  const webapp = window.Telegram?.WebApp;
  const raw = webapp?.initData || new URLSearchParams(location.hash.slice(1)).get('tgWebAppData');
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  if (webapp) webapp.ready();
  else if (window.TelegramWebviewProxy) window.TelegramWebviewProxy.postEvent('web_app_ready', '{}');
  else if (window.external?.notify) window.external.notify(JSON.stringify({eventType:'web_app_ready', eventData:{}}));
  else if (window.parent !== window) window.parent.postMessage(JSON.stringify({eventType:'web_app_ready', eventData:{}}), 'https://web.telegram.org');

  const status = {new:'Новое', taken:'В работе', answered:'Есть ответ', returned:'Возвращено агенту'};
  const role = {user:'Клиент', assistant:'Консультант', manager:'Менеджер'};
  const date = new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const feedback = (text, error=false) => { $('feedback').textContent=text; $('feedback').classList.toggle('error', error); };
  function lock(text) {
    $('card').hidden=true; $('composer').hidden=true; $('notice').textContent=text; feedback('');
  }
  async function api(path='', options={}) {
    const r = await fetch(base + path, {...options, cache:'no-store', credentials:'omit', redirect:'error',
      headers:{Authorization:'tma '+raw, ...options.headers}});
    if (!r.ok) {
      const text = {401:'Откройте карточку заново из Telegram.',403:'Этому аккаунту доступ не предоставлен.',
        404:'Запрошенные данные не найдены.',409:'Ответ недоступен: обращение уже обработано.'}[r.status] || 'Не удалось выполнить запрос.';
      if ([401,403].includes(r.status)) lock(text);
      throw new Error(text);
    }
    return r;
  }
  async function download(doc, button) {
    button.disabled=true;
    try {
      const res = await api(`/documents/${doc.kind}/${doc.id}`);
      const url = URL.createObjectURL(await res.blob());
      const link = Object.assign(document.createElement('a'), {href:url,download:doc.number+'.pdf'});
      document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),60000);
      feedback('Документ передан на скачивание.');
    } catch(e) {feedback(e.message,true);} finally {button.disabled=false;}
  }
  function docButton(doc) {
    const btn = el('button','','doc'); btn.type='button';
    const name = doc.kind==='quote'?'Коммерческое предложение':'Демонстрационный счёт';
    const label=el('span',''); label.append(el('strong',name),el('small',doc.number));
    btn.append(el('span','PDF','file-mark'),label,el('span','↓','download'));
    btn.setAttribute('aria-label',`Скачать ${name}: ${doc.number}`);
    btn.onclick=()=>download(doc,btn); return btn;
  }
  function message(m) {
    const item=el('li','',m.role), head=el('div','','message-head');
    head.append(el('b',role[m.role]),el('time',date.format(new Date(m.ts*1000))));
    item.append(head,el('p',m.text,'message-body')); return item;
  }
  function render(c) {
    $('ref').textContent=`Обращение № ${c.id}`; $('state').textContent=status[c.status] || c.status;
    $('subject').textContent=c.reason || 'Обращение клиента';
    $('request').textContent=c.request || 'Нет сообщения перед передачей менеджеру.';
    $('extra').hidden=!(c.summary || c.contact); $('summary').textContent=c.summary;
    $('contact').textContent=c.contact ? 'Контакт: '+c.contact : '';
    $('doc-count').textContent=c.documents.length; $('docs').replaceChildren(...c.documents.map(docButton));
    if (!c.documents.length) $('docs').append(el('p','В диалоге пока нет документов.','muted'));
    $('msg-count').textContent=c.messages.length; $('history').replaceChildren(...c.messages.map(message));
    if (!c.messages.length) $('history').append(el('li','В истории пока нет сообщений.'));
    $('card').hidden=false; $('composer').hidden=!c.can_answer; $('notice').textContent='';
  }
  const load=async()=>render(await (await api()).json());
  let sending=false;
  function updateButton() { $('send').disabled=sending || !$('answer').value.trim(); }
  $('answer').oninput=updateButton;
  $('composer').onsubmit=async ev=>{
    ev.preventDefault(); const answer=$('answer').value.trim(); if (!answer || sending) return;
    sending=true; updateButton(); let sent=false;
    try {
      await api('/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer})});
      sent=true; $('answer').value=''; $('composer').hidden=true; feedback('Ответ отправлен в чат клиента.'); await load();
    } catch(e) { feedback(sent?'Ответ отправлен. Не удалось обновить историю.':e.message,true); }
    finally { sending=false; updateButton(); }
  };
  if (!raw || !/^[1-9]\d{0,18}$/.test(handoff || '')) lock('Откройте обращение кнопкой в личном чате с ботом.');
  else load().catch(e=>lock(e.message));
})();
