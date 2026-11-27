// Чат покупателя: отправка сообщения, поток шагов агента, карточки товаров, кнопки выбора.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const rub = (v) => { const n = Number(v); return n.toLocaleString("ru-RU", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 }) + " ₽"; };
// ссылка из ответа сервера: только http(s) и адреса этого сайта, иначе ссылки нет
const safeUrl = (u) => /^https?:\/\//i.test(u || "") || /^\/(?!\/)/.test(u || "") ? u : "";
// метка браузера: по ней сервер отдает переписку только ее владельцу, поэтому она не угадывается
const rid = () => crypto.randomUUID ? crypto.randomUUID()
  : [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
// хранилище браузера бывает запрещено: тогда чат работает, но диалог не переживет перезагрузку
const store = (() => {
  try { const s = localStorage, k = "__t"; s.setItem(k, k); s.removeItem(k); return s; } catch (e) {
    const m = new Map();
    return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
  }
})();
const BROKEN = "Связь прервалась. Отправьте сообщение еще раз.";
const IDLE_MS = 60000;  // сервер шлет отметку хода раз в 20 секунд: минута тишины значит, что связь пропала
const WAIT_MS = 180000;  // столько после перезагрузки ждем ответ на сообщение, отправленное до нее

const state = {
  session: store.getItem("session") || "",
  client: store.getItem("client") || (() => { const c = rid(); store.setItem("client", c); return c; })(),
  page: "", pricesAt: 0, busy: false, steps: 0, tokens: 0, cost: 0, seen: 0, last: 0, callAfter: false,
  gen: 0, ctrl: null,  // поколение диалога и прерывание хода: ответ хода из прежнего диалога в новый не попадает
};
const starters = $("starters");  // подсказки живут в журнале под приветствием и возвращаются туда в новом диалоге

const TOOL = {
  search_products: "Поиск в каталоге", get_product: "Варианты лицензий", browse_categories: "Категории каталога",
  find_analogs: "Аналоги", kb_search: "База знаний", note_facts: "Память о клиенте", calc_total: "Расчет суммы",
  make_quote: "Черновик КП", match_spec: "Сверка списка с каталогом",
  handoff_to_manager: "Передача менеджеру",
};
const WAIT = {
  search_products: "Ищу в каталоге", get_product: "Сверяю варианты лицензий", browse_categories: "Смотрю категории",
  find_analogs: "Подбираю аналоги", kb_search: "Смотрю базу знаний", note_facts: "Записываю детали",
  calc_total: "Считаю сумму", match_spec: "Сверяю список с каталогом",
  make_quote: "Готовлю КП", handoff_to_manager: "Передаю менеджеру",
};
// подписи аргументов в «Ходе работы»: что не названо здесь (номера, контакты, сводка), клиенту не показывается
const ARG = {
  query: "запрос", vendor: "производитель", quantity: "количество", term_months: "срок", license_type: "тип лицензии",
  org_type: "организация", user_type: "покупатель", version: "версия", platform: "платформа", text: "в названии",
  only_available: "только в продаже", russian_only: "только отечественное", product_type: "тип",
  items: "позиции", discount_percent: "скидка", customer: "заказчик", reason: "причина",
};

// измерения вариантов, которые агент предлагает уточнить (clarify из get_product)
const DIM = { users_max: "число пользователей", nodes: "число устройств", qty: "количество", lang: "язык" };

function arg(k, v) {
  if (k === "term_months") return v ? v + " мес." : "бессрочная";
  if (k === "discount_percent") return v + "%";
  if (k === "items") return Array.isArray(v) ? `${v.length} поз., ${v.reduce((n, i) => n + (Number(i.qty) || 0), 0)} шт.` : "";
  if (typeof v === "boolean") return v ? "да" : "";
  return typeof v === "object" ? "" : String(v);
}

// Разметка ответа: жирный, заголовки, ссылки http(s), строки списка с отступом
function inline(s) {
  return s.replace(/^#{1,4}\s+(.+)$/, "<b>$1</b>").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:)]|$)/g, "$1$2")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|https?:\/\/[^\s<«»]*[^\s<«».,;:!?)]/g,
      (m, t, u) => `<a href="${u || m}" target="_blank" rel="noopener">${t || m}</a>`);
}

function text(s) {
  let html = "", block = true;
  for (const line of esc(s).split("\n")) {
    const li = line.match(/^\s*(\d+[.)]|[-•*])\s+(.*)$/);
    if (li) html += `<span class="li">${/\d/.test(li[1]) ? li[1] : "•"} ${inline(li[2])}</span>`;
    else html += (block ? "" : "\n") + inline(line);
    block = !!li;
  }
  return html;
}

function add(cls, html) {
  const d = document.createElement("div");
  d.className = cls; d.innerHTML = html; $("log").append(d);
  scrollEnd();
  return d;
}

function scrollEnd() { $("log").scrollTop = $("log").scrollHeight; }

// Название без производителя в начале: он уже написан строкой выше
function bare(c) {
  const t = String(c.title || ""), v = String(c.vendor || "");
  const cut = v && t.toLowerCase().startsWith(v.toLowerCase()) ? t.slice(v.length).replace(/^[\s,.:-]+/, "") : "";
  return cut || t;
}

function card(c) {
  const url = safeUrl(c.url), title = bare(c);
  const go = url ? `<a class="go" href="${esc(url)}" target="_blank" rel="noopener">Открыть в магазине</a>` : "";
  if (c.type === "offer") {
    const d = c.details && typeof c.details === "object" ? Object.entries(c.details) : [];
    const pick = "Беру: " + title + (c.vendor_code ? ", артикул " + c.vendor_code : "");  // по артикулу агент найдет именно этот вариант
    return `<div class="v">${esc(c.vendor || "")}</div><div class="t">${esc(title)}</div>
      ${d.length ? `<div class="d">${d.map(([k, v]) => esc(k) + ": " + esc(v)).join(" · ")}</div>` : ""}
      <div class="row"><div>${c.price ? `<div class="p">${rub(c.price)}</div>` : `<div class="p req">Цена по запросу</div>`}
      <span>${[c.vendor_code ? "Артикул " + esc(c.vendor_code) : "", c.available ? '<span class="badge">в продаже</span>' : ""].filter(Boolean).join(" · ")}</span></div>
      <span class="acts"><button type="button" class="take" data-say="${esc(pick)}">Выбрать</button>${go}</span></div>`;
  }
  let price = "Цена по запросу";
  if (c.price_min) price = c.price_min === c.price_max ? rub(c.price_min) : `от ${rub(c.price_min)}`;
  return `<div class="v">${esc(c.vendor || "")}</div><div class="t">${esc(title)}</div>
    <div class="row"><div><div class="p${c.price_min ? "" : " req"}">${price}</div>
    <span>Вариантов: ${esc(c.n_offers ?? "")}${c.in_ru_registry ? ' · <span class="badge">реестр отечественного ПО</span>' : ""}</span></div>
    <span class="acts">${go}</span></div>`;
}

// Черновик КП: новая версия того же номера заменяет прежнюю карточку, ссылка остается только у новой
function quote(e) {
  for (const old of document.querySelectorAll(".card.doc:not(.old)")) {
    if (old.dataset.n !== String(e.number)) continue;
    old.classList.add("old");
    old.querySelector(".go")?.remove();
    old.querySelector(".acts").textContent = "Заменен версией ниже";
  }
  const url = safeUrl(e.url);
  const d = add("card doc", `<div class="t">Черновик ${esc(e.number)}</div><div class="row"><span class="sum">${e.total ? "Итого " + rub(e.total) : ""}</span>
    <span class="acts">${url ? `<a class="go" href="${esc(url)}" target="_blank" rel="noopener">Открыть PDF</a>` : ""}</span></div>`);
  d.dataset.n = e.number;
}

// Список позиций клиента, сопоставленный с каталогом: строка клиента, что нашлось, количество, цена, сумма, статус
const STATUS = { "точно": "ok", "проверьте": "check", "цена по запросу": "req", "нет в каталоге": "none", "не разобрано": "none" };

function specTable(e) {
  const rows = e.rows || [], counts = { "строк": rows.length };
  rows.forEach((r) => (counts[r.status] = (counts[r.status] || 0) + 1));
  const body = rows.map((r) => {
    const url = safeUrl(r.url), name = esc(r.name || "");
    const notes = [r.vendor_code ? "Артикул " + esc(r.vendor_code) : "", r.qty_assumed ? "количество не указано, взято 1" : "", esc(r.hint || "")].filter(Boolean).join(" · ");
    const found = r.name ? `<div class="t">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${name}</a>` : name}</div>` : "";
    return `<tr><td class="pos"><div class="q">${esc(r.line)}</div>${found}${notes ? `<div class="d">${notes}</div>` : ""}</td>
      <td class="num" data-l="Кол-во">${esc(r.qty)}</td>
      <td class="num" data-l="Цена за шт.">${r.price ? rub(r.price) : r.status === "цена по запросу" ? "по запросу" : "—"}</td>
      <td class="num" data-l="Сумма">${r.sum ? rub(r.sum) : "—"}</td>
      <td class="st ${STATUS[r.status] || "none"}">${esc(r.status)}</td></tr>`;
  }).join("");
  return `<div class="t">Сверка списка с каталогом</div>
    <div class="d">${Object.entries(counts).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ")}</div>
    <div class="wrap"><table><thead><tr><th>Строка клиента и что нашлось</th><th class="num">Кол-во</th><th class="num">Цена за шт.</th><th class="num">Сумма</th><th>Статус</th></tr></thead>
    <tbody>${body}</tbody></table></div>
    ${e.total ? `<div class="row"><span>Итого по строкам "точно"</span><span class="sum">${rub(e.total)}</span></div>` : ""}`;
}

// Кнопка в шапке показывает, что обращение у менеджера: "wait" ждет ответа, "in" менеджер уже ответил
function callState(s) {
  const b = $("call");
  b.textContent = s === "in" ? "Менеджер в чате" : s === "wait" ? "Менеджер подключится" : "Позвать менеджера";
  b.disabled = !!s; b.dataset.state = s;
}

// Строка под карточками с ценой: на какое время цены каталога. Время приходит с /api/health
const MSK = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function pricesLine() {
  if (!state.pricesAt) return "";
  const p = Object.fromEntries(MSK.formatToParts(new Date(state.pricesAt * 1000)).map((x) => [x.type, x.value]));
  return `Цены каталога на ${p.day}.${p.month} ${p.hour}:${p.minute} МСК`;
}

function showReply(r, restoring = false) {
  const log = $("log"), start = log.children.length;
  if (r.reply) {
    const mgr = r.from === "manager";
    const ok = r.reviewed ? '<span class="ok">проверено менеджером</span>' : "";
    add(mgr ? "msg bot mgr" : "msg bot", `<span class="who">${mgr ? "Менеджер Softline" : "ИИ-консультант"}${ok}</span>${text(r.reply)}`);
    if (mgr) callState("in");
  }
  for (const c of r.cards || []) add("card", card(c)).querySelector(".take")?.addEventListener("click", (e) => send(e.target.dataset.say));
  if ((r.cards || []).some((c) => c.price || c.price_min)) add("fresh", pricesLine());  // пустая строка заполнится, когда придет время с /api/health
  for (const e of r.events || []) {
    if (e.type === "spec") add("card spec", specTable(e));
    if (e.type === "quote") quote(e);
    if (e.type === "handoff") {
      add("msg sys", Number.isInteger(e.sla_minutes)
        ? `Обращение № ${esc(e.id)} у менеджера. В рабочее время отвечаем в течение ${e.sla_minutes} мин, ответ придет в этот чат.`
        : `Обращение передано менеджеру, номер ${esc(e.id)}`);
      if (!$("call").dataset.state) callState("wait");
    }
  }
  if (r.review) add("msg sys review", esc(r.review.text));  // режим "ответ проверяет менеджер": срок вместо текста
  if ((r.choices || []).length) {
    const d = add("choices", r.choices.map((c) => `<button type="button">${esc(c)}</button>`).join(""));
    d.querySelectorAll("button").forEach((b) => (b.onclick = () => { d.remove(); send(b.textContent); }));
  }
  // ответ выше окна: журнал встает на его начало, а не на последние кнопки
  const first = log.children[start];
  if (!restoring && first && log.scrollHeight - first.offsetTop > log.clientHeight) log.scrollTop = first.offsetTop - 16;
}

function summary(name, out) {
  if (!out || typeof out !== "object") return "";
  if (out.error) return "Не получилось, агент продолжил без этого шага";
  if (name === "search_products" || name === "find_analogs") {
    const p = out.products || [];
    return p.length ? esc(`Найдено: ${out.total ?? p.length}. Первые: ${p.slice(0, 3).map((x) => x.title).join("; ")}`) : "Ничего не найдено";
  }
  if (name === "get_product") {
    const c = (out.clarify || []).slice(0, 3).map((k) => DIM[k] || ARG[k] || k).join(", ");
    return esc(`Подходит вариантов: ${out.matching ?? "?"} из ${out.total_offers ?? "?"}${c ? ". Уточнить: " + c : ""}`);
  }
  if (name === "browse_categories") return esc((out.categories || []).slice(0, 4).map((x) => x.name).join("; ")) || "Категорий не найдено";
  if (name === "kb_search") {
    const found = (out.results || []).slice(0, 2).map((x) => {
      const t = esc(x.title || x.heading), u = /^https:\/\//.test(x.url || "") ? x.url : "";
      return u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${t}</a>` : t;
    });
    return found.length ? "Нашлось: " + found.join("; ") : "В базе знаний нет ответа";
  }
  if (name === "note_facts") return esc(Object.entries(out.saved || {}).map(([k, v]) => `${k}: ${v}`).join("; "));
  if (name === "calc_total") return out.total ? `Итого ${rub(out.total)}` : "";
  if (name === "match_spec") {
    const counts = Object.entries(out.counts || {}).map(([k, v]) => `${k} ${v}`).join(", ");
    return (counts ? counts + ". " : "") + (out.total ? `Итого по найденным точно ${rub(out.total)}` : "Точных совпадений нет");
  }
  if (name === "make_quote") return out.ok ? esc(`${out.number}, итого ${rub(out.total)}`) : "";
  if (name === "handoff_to_manager") return out.ticket ? `Обращение № ${esc(out.ticket)}` : "";
  return "";
}

function step(cls, name, args, out = "", tool = "") {
  const ol = $("steps");
  ol.querySelector(".empty")?.remove();
  const li = document.createElement("li");
  li.className = cls;
  const a = tool === "note_facts" ? "" : Object.entries(args || {}).filter(([k]) => ARG[k])
    .map(([k, v]) => [ARG[k], arg(k, v)]).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(" · ");
  li.innerHTML = `<div class="n">${esc(name)}</div>${a ? `<div class="a">${esc(a)}</div>` : ""}${out ? `<div class="o">${out}</div>` : ""}`;
  ol.append(li); ol.scrollTop = ol.scrollHeight;
  return li;
}

const said = (m) => "Клиент: " + (m.length > 60 ? m.slice(0, 60) + "…" : m);

// Шаги хода в «Ходе работы»: итоги к уже показанным вызовам инструментов, проверки и сбои
function trace(list, pending = []) {
  let k = 0;
  for (const t of list || []) {
    if (t.kind === "tool") {
      const li = pending[k++] || step("", TOOL[t.name] || "Шаг агента", t.args, "", t.name);
      const s = summary(t.name, t.output);
      if (s) li.insertAdjacentHTML("beforeend", `<div class="o">${s}</div>`);
    }
    if (t.kind === "guard" && t.name === "price") step("guard", "Проверка цен", null, esc("В черновике ответа была сумма не из каталога: " + (t.output || []).join(", ") + ". Ответ переписан."));
    if (t.kind === "guard" && t.name === "wording") step("guard", "Проверка текста", null, "В черновике ответа были служебные имена полей. Ответ переписан.");
    if (t.kind === "error") step("guard", "Сбой", null, esc(t.output));
  }
}

function meter(u, total = false) {
  if (total) state.steps = state.tokens = state.cost = 0;
  if (!u) return;
  state.steps += (u.llm_calls || 0) + (u.tool_calls || 0);
  state.tokens += (u.prompt_tokens || 0) + (u.completion_tokens || 0);
  state.cost += u.cost_rub || 0;
  $("m1").textContent = state.steps;
  $("m2").textContent = state.tokens.toLocaleString("ru-RU");
  $("m3").textContent = rub(state.cost);
}

// Поле ввода растет по тексту до 120px и возвращается к 40px, когда текст ушел
function fit() {
  const t = $("t");
  t.style.height = "auto";
  t.style.height = Math.min(Math.max(t.scrollHeight + 2, 40), 120) + "px";
}

function lock(on) {
  state.busy = on; $("b").disabled = on;
  if (on || !state.callAfter) return;
  state.callAfter = false;  // клиент звал менеджера, пока агент отвечал
  if (!$("call").dataset.state) call();
}

function saveSession(id, gen) {
  if (!id || gen !== state.gen) return;
  state.session = id; store.setItem("session", id);
}

async function send(message) {
  message = (message || "").trim();
  if (!message || state.busy) return;
  const gen = state.gen, ctrl = state.ctrl = new AbortController();
  let idle = setTimeout(() => ctrl.abort(), IDLE_MS), got = false;
  lock(true); starters.hidden = true;
  document.querySelectorAll(".choices").forEach((e) => e.remove());
  document.querySelectorAll(".msg.user.failed").forEach((e) => { if (e.textContent === message) e.remove(); });
  const mine = add("msg user", esc(message));
  step("turn", said(message));
  $("say").textContent = "Консультант отвечает";
  const typing = add("typing", '<i></i><i></i><i></i><span>Думаю</span>');
  typing.setAttribute("aria-hidden", "true");
  const pending = [];
  const handle = (line) => {
    const ev = JSON.parse(line);
    if (ev.type === "session") saveSession(ev.session_id, gen);
    else if (ev.type === "tool") {
      typing.querySelector("span").textContent = WAIT[ev.name] || "Работаю";
      pending.push(step("", TOOL[ev.name] || "Шаг агента", ev.args, "", ev.name));
    } else if (ev.type === "reply") {
      typing.remove(); got = true;
      saveSession(ev.session_id, gen);
      trace(ev.trace, pending);
      showReply(ev); meter(ev.usage);
      if (ev.review) step("turn", "Ответ ушел на проверку менеджеру");
      $("say").textContent = ev.review ? "Ответ проверяет менеджер" : "Ответ получен";
    }
  };
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
      body: JSON.stringify({ message, session_id: state.session || null, client_id: state.client, page_url: state.page }),
    });
    const refused = (text) => Object.assign(new Error(text), { refused: true });  // сервер ответил отказом
    if (r.status === 429) throw refused("Слишком много сообщений подряд. Подождите минуту и отправьте еще раз.");
    if (!r.ok) throw refused("На сервере сбой. Отправьте сообщение еще раз или позовите менеджера.");
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (gen !== state.gen) return;
      if (done) break;
      clearTimeout(idle); idle = setTimeout(() => ctrl.abort(), IDLE_MS);
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (line) handle(line);
      }
    }
    if (buf.trim()) handle(buf.trim());  // последняя строка без перевода строки
    if (!got) throw new Error("поток оборвался до ответа");
  } catch (e) {
    if (gen !== state.gen) return;  // клиент начал новый диалог: прерванный ход молча забыт
    typing.remove(); mine.classList.add("failed");
    step("guard", e.refused ? "Сервер не принял сообщение" : "Сбой связи", null, "Ответ не получен");
    $("say").textContent = "";
    // сеть, обрыв потока и долгая тишина дают одно сообщение; отказ сервера назван своими словами
    add("msg sys", e.refused ? esc(e.message) : BROKEN);
    if (!$("t").value.trim()) { $("t").value = message; fit(); }  // текст вернулся в поле: отправить еще раз одним нажатием
  } finally {
    clearTimeout(idle);
  }
  if (gen !== state.gen) return;
  state.ctrl = null; lock(false); $("t").focus();
}

// Клиент сам переключается на менеджера: модель для этого не нужна
async function call() {
  if (state.busy) {  // агент еще отвечает: менеджера позовем сразу после ответа
    if (!state.callAfter) { state.callAfter = true; add("msg sys", "Позову менеджера, как только консультант закончит ответ."); }
    return;
  }
  const gen = state.gen;
  lock(true); starters.hidden = true;
  try {
    const r = await fetch("/api/chat/handoff", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.session || null, client_id: state.client, page_url: state.page }),
    });
    if (!r.ok) throw new Error(r.status === 429 ? "слишком много запросов подряд, подождите минуту" : "на сервере сбой");
    const ev = await r.json();
    if (gen !== state.gen) return;
    saveSession(ev.session_id, gen);
    step("turn", "Клиент нажал: позвать менеджера");
    showReply(ev);
    if (!$("call").dataset.state) callState("wait");
  } catch (e) {
    if (gen !== state.gen) return;
    add("msg sys", "Не удалось позвать менеджера: " + (e instanceof TypeError ? "нет связи" : esc(e.message)) + ". Нажмите еще раз.");
  }
  if (gen === state.gen) lock(false);
}

function submit() {  // пока агент отвечает, набранный текст остается в поле и не отправляется
  const v = $("t").value;
  if (state.busy || !v.trim()) return;
  $("t").value = ""; fit(); send(v);
}
$("call").onclick = call;
$("f").onsubmit = (e) => { e.preventDefault(); submit(); };
$("t").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); } };
$("t").addEventListener("input", fit);
starters.querySelectorAll("button").forEach((b) => (b.onclick = () => send(b.dataset.text || b.textContent)));  // data-text: сценарий длиннее подписи, например список позиций
$("new").onclick = () => {
  state.gen++; state.ctrl?.abort(); state.ctrl = null; state.callAfter = false; lock(false);
  state.session = ""; store.removeItem("session");
  state.seen = state.last = 0; meter({}, true);
  $("log").innerHTML = ""; $("steps").innerHTML = ""; callState("");
  step("turn", "Новый диалог. Память о клиенте сохранена.");
  hello();
};

function hello() {
  showReply({ reply: "Здравствуйте. Помогу подобрать программное обеспечение и лицензии, отвечу на вопросы об оплате и получении. Какой продукт или задача вас интересует?" });
  $("log").append(starters); starters.hidden = false;
}

// Витрина: поиск по каталогу и выбор страницы товара
async function goods(q) {
  try {
    const r = await (await fetch("/api/products?q=" + encodeURIComponent(q))).json();
    $("goods").innerHTML = (r.products || []).map((p) => `<li><button type="button" class="pick" data-url="${esc(p.url || "")}" data-title="${esc(p.title)}">
      <span class="v">${esc(p.vendor || "")}</span><span class="t">${esc(p.title)}</span>
      <span class="m"><span>${p.price_min ? "от " + rub(p.price_min) : "цена по запросу"}</span><span>вариантов: ${esc(p.n_offers)}</span>${p.in_ru_registry ? '<span class="badge">реестр отечественного ПО</span>' : ""}</span></button></li>`).join("")
      || '<li class="empty">Ничего не найдено. Измените запрос.</li>';
  } catch (e) {
    $("goods").innerHTML = '<li class="empty">Каталог не загрузился. Обновите страницу.</li>';
  }
  $("goods").querySelectorAll(".pick").forEach((b) => (b.onclick = () => {
    state.page = b.dataset.url; $("ctxt").textContent = "Клиент на странице: " + b.dataset.title; $("ctx").classList.add("on"); $("t").focus();
  }));
}
$("sf").onsubmit = (e) => { e.preventDefault(); goods($("sq").value.trim()); };
$("ctxoff").onclick = () => { state.page = ""; $("ctx").classList.remove("on"); };

// Ответ менеджера и проверенный менеджером ответ агента приходят в этот же чат: пока страница открыта,
// чат раз в несколько секунд спрашивает о новых
async function answersPoll() {
  if (!state.session || state.busy || document.hidden) return;
  const gen = state.gen;
  try {
    const r = await fetch(`/api/session/${encodeURIComponent(state.session)}/answers?client_id=${encodeURIComponent(state.client)}&after=${state.seen}`);
    if (!r.ok) return;
    const found = (await r.json()).answers || [];
    if (gen !== state.gen) return;  // пока шел запрос, клиент начал новый диалог
    for (const m of found) {
      if (m.id <= state.seen) continue;
      state.seen = state.last = Math.max(state.last, m.id);
      document.querySelectorAll("#log .msg.review").forEach((e) => e.remove());  // проверка закончилась
      showReply(m.reviewed ? { ...m, reply: m.content } : { reply: m.content, from: "manager" });
      step("turn", m.reviewed ? "Менеджер проверил и отправил ответ" : "Менеджер ответил в чат");
    }
  } catch (e) { /* связь пропала: спросим на следующем круге */ }
}
setInterval(answersPoll, 4000);

// Какая модель отвечает и на связи ли она: если нет, вопрос сразу уйдет менеджеру, и клиент видит это заранее
async function model() {
  try {
    const h = await (await fetch("/api/health")).json(), el = $("model");
    el.textContent = h.online ? `Отвечает модель ${h.label}. Цены и суммы проверяет код.`
      : `Модель ${h.label} сейчас не на связи: вопрос сразу уйдет менеджеру.`;
    el.classList.toggle("off", !h.online);
    el.hidden = false;
    state.pricesAt = h.prices_at || 0;
    document.querySelectorAll(".fresh:empty").forEach((e) => (e.textContent = pricesLine()));
  } catch (e) { /* подпись не загрузилась: чат работает и без нее */ }
}
model(); setInterval(model, 30000);

// История диалога: переписка, «Ход работы» и счетчики по полям trace и usage, если сервер их отдает
function history(s) {
  const msgs = (s.messages || []).filter((m) => m.id > state.last);
  const traced = "usage" in s || msgs.some((m) => "trace" in m);  // старый сервер шагов не отдает: панель остается пустой
  msgs.forEach((m, i) => {
    state.last = m.id;
    if (m.role === "user") { add("msg user", esc(m.content)); if (traced) step("turn", said(m.content)); return; }
    if (m.from === "manager" || m.reviewed) state.seen = Math.max(state.seen, m.id);  // опрос ответов их не повторит
    if (m.from === "manager" && traced) step("turn", "Менеджер ответил в чат");
    trace(m.trace);
    showReply({ reply: m.content, from: m.from, reviewed: m.reviewed, cards: m.cards, events: m.events, choices: i === msgs.length - 1 ? m.choices : [] }, true);
  });
  if (s.review) { trace(s.review.trace); showReply({ review: s.review }, true); }  // ответ на последнюю реплику на проверке
  if (s.usage) meter(s.usage, true);
  if (s.status) callState(s.status === "handoff" ? (state.seen ? "in" : "wait") : "");  // менеджер мог вернуть диалог агенту
  scrollEnd(); document.fonts?.ready.then(scrollEnd);  // шрифт догрузился и переверстал строки: журнал снова внизу
}

async function getSession(signal) {
  // переписку сервер отдает только владельцу диалога: вместе с номером диалога уходит метка этого браузера
  const r = await fetch("/api/session/" + encodeURIComponent(state.session) + "?client_id=" + encodeURIComponent(state.client), { signal });
  if (r.status === 404) { state.session = ""; store.removeItem("session"); }
  return r.ok ? r.json() : {};
}

// Страницу перезагрузили, пока агент отвечал: ход идет на сервере, ответ дорисуется, когда появится в истории
async function awaitReply(gen) {
  const typing = add("typing", '<i></i><i></i><i></i><span>Думаю</span>');
  typing.setAttribute("aria-hidden", "true");
  for (const until = Date.now() + WAIT_MS; Date.now() < until;) {
    await new Promise((ok) => setTimeout(ok, 4000));
    if (gen !== state.gen) return;
    try {
      const s = await getSession();
      if (gen !== state.gen) return;
      const msgs = s.messages || [];
      if (s.review || (msgs.length && msgs[msgs.length - 1].role !== "user")) { typing.remove(); history(s); return; }
    } catch (e) { /* связь пропала: спросим на следующем круге */ }
  }
  typing.remove(); add("msg sys", "Ответ не пришел. Отправьте сообщение еще раз.");
}

(async () => {
  goods("");
  if (!state.session) return hello();
  const gen = state.gen, ctrl = new AbortController(), cut = setTimeout(() => ctrl.abort(), 10000);
  lock(true);  // пока грузится история, новое сообщение не отправляется: иначе оно встанет выше старой переписки
  let s = {};
  try { s = await getSession(ctrl.signal); } catch (e) { /* история не загрузилась: начинаем с приветствия */ }
  clearTimeout(cut);
  if (gen !== state.gen) return;
  const msgs = s.messages || [];
  if (!msgs.length) { hello(); lock(false); return; }
  starters.hidden = true;
  history(s);
  if (msgs[msgs.length - 1].role === "user" && !s.review) await awaitReply(gen);
  if (gen === state.gen) lock(false);
})();
