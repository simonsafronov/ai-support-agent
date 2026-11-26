// Чат покупателя: отправка сообщения, поток шагов агента, карточки товаров, кнопки выбора.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const rub = (v) => { const n = Number(v); return n.toLocaleString("ru-RU", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 }) + " ₽"; };
const rid = () => Math.random().toString(16).slice(2, 10) + Date.now().toString(16);

const state = {
  session: localStorage.getItem("session") || "",
  client: localStorage.getItem("client") || (() => { const c = rid(); localStorage.setItem("client", c); return c; })(),
  page: "", busy: false, steps: 0, tokens: 0, cost: 0, seen: 0,
};

const TOOL = {
  search_products: "Поиск в каталоге", get_product: "Варианты лицензий", browse_categories: "Категории каталога",
  find_analogs: "Аналоги", kb_search: "База знаний", note_facts: "Память о клиенте", calc_total: "Расчет суммы",
  make_quote: "Черновик КП",
  handoff_to_manager: "Передача менеджеру",
};
const WAIT = {
  search_products: "Ищу в каталоге", get_product: "Сверяю варианты лицензий", browse_categories: "Смотрю категории",
  find_analogs: "Подбираю аналоги", kb_search: "Смотрю базу знаний", note_facts: "Записываю детали",
  calc_total: "Считаю сумму",
  make_quote: "Готовлю КП", handoff_to_manager: "Передаю менеджеру",
};

function text(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

function add(cls, html) {
  const d = document.createElement("div");
  d.className = cls; d.innerHTML = html; $("log").append(d);
  $("log").scrollTop = $("log").scrollHeight;
  return d;
}

function card(c) {
  if (c.type === "offer") {
    const price = c.price ? `<div class="p">${rub(c.price)}</div>` : `<div class="p req">Цена по запросу</div>`;
    return `<div class="v">${esc(c.vendor || "")}</div><div class="t">${esc(c.title)}</div>${price}
      <div class="row"><span>${c.vendor_code ? "Артикул " + esc(c.vendor_code) : ""}${c.available ? ' <span class="badge">· в продаже</span>' : ""}</span>
      ${c.url ? `<a class="go" href="${esc(c.url)}" target="_blank" rel="noopener">Открыть в магазине</a>` : ""}</div>`;
  }
  let price = "Цена по запросу";
  if (c.price_min) price = c.price_min === c.price_max ? rub(c.price_min) : `от ${rub(c.price_min)}`;
  return `<div class="v">${esc(c.vendor || "")}</div><div class="t">${esc(c.title)}</div><div class="p${c.price_min ? "" : " req"}">${price}</div>
    <div class="row"><span>Вариантов: ${c.n_offers ?? ""}${c.in_ru_registry ? ' <span class="badge">· реестр отечественного ПО</span>' : ""}</span>
    ${c.url ? `<a class="go" href="${esc(c.url)}" target="_blank" rel="noopener">Открыть в магазине</a>` : ""}</div>`;
}

function showReply(r) {
  if (r.reply) add(r.from === "manager" ? "msg bot mgr" : "msg bot", text(r.reply));
  for (const c of r.cards || []) add("card", card(c));
  for (const e of r.events || []) {
    if (e.type === "quote") add("card doc", `<div class="t">Черновик КП ${esc(e.number)}</div><div class="row"><span>${e.total ? "Итого " + rub(e.total) : ""}</span><a class="go" href="${esc(e.url)}" target="_blank" rel="noopener">Открыть PDF</a></div>`);
    if (e.type === "handoff") add("msg sys", `Обращение передано менеджеру, номер ${esc(e.id)}`);
  }
  if ((r.choices || []).length) {
    const d = add("choices", r.choices.map((c) => `<button type="button">${esc(c)}</button>`).join(""));
    d.querySelectorAll("button").forEach((b) => (b.onclick = () => { d.remove(); send(b.textContent); }));
  }
}

function summary(name, out) {
  if (!out || typeof out !== "object") return "";
  if (out.error) return "Ошибка: " + out.error;
  if (name === "search_products" || name === "find_analogs") {
    const p = out.products || [];
    return p.length ? `Найдено: ${out.total ?? p.length}. Первые: ${p.slice(0, 3).map((x) => x.title).join("; ")}` : "Ничего не найдено";
  }
  if (name === "get_product") {
    const c = (out.clarify || []).slice(0, 3).join(", ");
    return `Подходит вариантов: ${out.matching ?? "?"} из ${out.total_offers ?? "?"}${c ? ". Уточнить: " + c : ""}`;
  }
  if (name === "browse_categories") return (out.categories || []).slice(0, 4).map((x) => x.name).join("; ") || "Категорий не найдено";
  if (name === "kb_search") return (out.results || []).slice(0, 2).map((x) => x.title || x.heading).join("; ") || "В базе знаний нет ответа";
  if (name === "note_facts") return Object.entries(out.saved || {}).map(([k, v]) => `${k}: ${v}`).join("; ");
  if (name === "calc_total") return out.total ? `Итого ${rub(out.total)}` : "";
  if (name === "make_quote") return out.ok ? `${out.number}, итого ${rub(out.total)}` : out.error || "";
  if (name === "handoff_to_manager") return `Обращение ${out.ticket}`;
  return "";
}

function step(cls, name, args, out) {
  const ol = $("steps");
  ol.querySelector(".empty")?.remove();
  const li = document.createElement("li");
  li.className = cls;
  const a = args && Object.keys(args).length ? Object.entries(args).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ") : "";
  li.innerHTML = `<div class="n">${esc(name)}</div>${a ? `<div class="a">${esc(a)}</div>` : ""}${out ? `<div class="o">${esc(out)}</div>` : ""}`;
  ol.append(li); ol.scrollTop = ol.scrollHeight;
  return li;
}

function meter(u) {
  if (!u) return;
  state.steps += (u.llm_calls || 0) + (u.tool_calls || 0);
  state.tokens += (u.prompt_tokens || 0) + (u.completion_tokens || 0);
  state.cost += u.cost_rub || 0;
  $("m1").textContent = state.steps;
  $("m2").textContent = state.tokens.toLocaleString("ru-RU");
  $("m3").textContent = rub(state.cost);
}

async function send(message) {
  message = (message || "").trim();
  if (!message || state.busy) return;
  state.busy = true; $("b").disabled = true; $("starters").hidden = true;
  document.querySelectorAll(".choices").forEach((e) => e.remove());
  add("msg user", esc(message));
  step("turn", "Клиент: " + (message.length > 60 ? message.slice(0, 60) + "…" : message));
  const typing = add("typing", "<i></i><i></i><i></i><span>Думаю</span>");
  const pending = [];
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: state.session || null, client_id: state.client, page_url: state.page }),
    });
    if (r.status === 429) throw new Error("слишком много сообщений подряд, подождите минуту");
    if (!r.ok) throw new Error("сервер ответил " + r.status);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "tool") {
          typing.querySelector("span").textContent = WAIT[ev.name] || "Работаю";
          pending.push(step("", TOOL[ev.name] || ev.name, ev.args));
        } else if (ev.type === "reply") {
          typing.remove();
          if (ev.session_id) { state.session = ev.session_id; localStorage.setItem("session", ev.session_id); }
          let k = 0;
          for (const t of ev.trace || []) {
            if (t.kind === "tool" && pending[k]) { const s = summary(t.name, t.output); if (s) pending[k].insertAdjacentHTML("beforeend", `<div class="o">${esc(s)}</div>`); k++; }
            if (t.kind === "guard" && t.name === "price") step("guard", "Проверка цен", null, "В черновике ответа была сумма не из каталога: " + (t.output || []).join(", ") + ". Ответ переписан.");
            if (t.kind === "guard" && t.name === "wording") step("guard", "Проверка текста", null, "В черновике ответа были служебные имена полей. Ответ переписан.");
            if (t.kind === "error") step("guard", "Сбой", null, String(t.output));
          }
          showReply(ev); meter(ev.usage);
        }
      }
    }
  } catch (e) {
    typing.remove();
    add("msg sys", "Не удалось получить ответ: " + esc(e.message) + ". Отправьте сообщение еще раз.");
  }
  state.busy = false; $("b").disabled = false; $("t").focus();
}

$("call").onclick = async () => {  // клиент сам переключается на менеджера: модель для этого не нужна
  if (state.busy) return;
  state.busy = true; $("starters").hidden = true;
  try {
    const r = await fetch("/api/chat/handoff", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.session || null, client_id: state.client, page_url: state.page }),
    });
    if (!r.ok) throw new Error(r.status === 429 ? "слишком много сообщений подряд, подождите минуту" : "сервер ответил " + r.status);
    const ev = await r.json();
    state.session = ev.session_id; localStorage.setItem("session", ev.session_id);
    step("turn", "Клиент нажал: позвать менеджера");
    showReply(ev);
  } catch (e) {
    add("msg sys", "Не удалось позвать менеджера: " + esc(e.message) + ". Нажмите еще раз.");
  }
  state.busy = false;
};
$("f").onsubmit = (e) => { e.preventDefault(); const v = $("t").value; $("t").value = ""; send(v); };
$("t").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("f").requestSubmit(); } };
$("starters").querySelectorAll("button").forEach((b) => (b.onclick = () => send(b.textContent)));
$("new").onclick = () => {
  state.session = ""; localStorage.removeItem("session");
  state.steps = state.tokens = state.cost = state.seen = 0; meter({});
  $("log").innerHTML = ""; $("steps").innerHTML = ""; $("starters").hidden = false;
  step("turn", "Новый диалог. Память о клиенте сохранена.");
  hello();
};

function hello() {
  add("msg bot", "Здравствуйте. Помогу подобрать программное обеспечение и лицензии, отвечу на вопросы об оплате и получении. Какой продукт или задача вас интересует?");
}

// Витрина: поиск по каталогу и выбор страницы товара
async function goods(q) {
  const r = await (await fetch("/api/products?q=" + encodeURIComponent(q))).json();
  $("goods").innerHTML = (r.products || []).map((p) => `<li data-url="${esc(p.url || "")}" data-title="${esc(p.title)}">
    <div class="v">${esc(p.vendor || "")}</div><div class="t">${esc(p.title)}</div>
    <div class="m"><span>${p.price_min ? "от " + rub(p.price_min) : "цена по запросу"}</span><span>вариантов: ${p.n_offers}</span>${p.in_ru_registry ? '<span class="badge">реестр отечественного ПО</span>' : ""}</div></li>`).join("")
    || '<li class="empty">Ничего не найдено. Измените запрос.</li>';
  $("goods").querySelectorAll("li[data-url]").forEach((li) => (li.onclick = () => {
    state.page = li.dataset.url; $("ctxt").textContent = "Клиент на странице: " + li.dataset.title; $("ctx").classList.add("on"); $("t").focus();
  }));
}
$("sf").onsubmit = (e) => { e.preventDefault(); goods($("sq").value.trim()); };
$("ctxoff").onclick = () => { state.page = ""; $("ctx").classList.remove("on"); };

// Ответ менеджера приходит в этот же чат: пока страница открыта, чат раз в несколько секунд спрашивает о новых
async function answersPoll() {
  if (!state.session || state.busy || document.hidden) return;
  try {
    const r = await fetch(`/api/session/${state.session}/answers?client_id=${encodeURIComponent(state.client)}&after=${state.seen}`);
    if (!r.ok) return;
    for (const m of (await r.json()).answers || []) {
      if (m.id <= state.seen) continue;
      state.seen = m.id;
      showReply({ reply: m.content, from: "manager" });
      step("turn", "Менеджер ответил в чат");
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
  } catch (e) { /* подпись не загрузилась: чат работает и без нее */ }
}
model(); setInterval(model, 30000);

(async () => {
  goods("");
  if (state.session) {
    try {
      // переписку сервер отдает только владельцу диалога: вместе с номером диалога уходит метка этого браузера
      const r = await fetch("/api/session/" + encodeURIComponent(state.session) + "?client_id=" + encodeURIComponent(state.client));
      if (r.status === 404) { state.session = ""; localStorage.removeItem("session"); }
      const s = r.ok ? await r.json() : {};
      if ((s.messages || []).length) {
        $("starters").hidden = true;
        for (const m of s.messages) {
          if (m.from === "manager") state.seen = m.id;
          m.role === "user" ? add("msg user", esc(m.content)) : showReply({ reply: m.content, from: m.from, cards: m.cards, events: m.events });
        }
        return;
      }
    } catch (e) { /* история не загрузилась: начинаем с приветствия */ }
  }
  hello();
})();
