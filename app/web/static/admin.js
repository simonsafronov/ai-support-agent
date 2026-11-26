// Экран оператора: показатели, экономика, очередь обращений, диалоги с шагами агента, ответ клиенту.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 0) => Number(v).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
const rub = (v) => { if (v == null) return "нет данных"; const n = Number(v); return num(n, Number.isInteger(n) ? 0 : 2) + " ₽"; };
// Время на экране московское, как сроки реакции и расписание обновления каталога
const MSK = { timeZone: "Europe/Moscow" };
const dt = (t) => t ? new Date(t * 1000).toLocaleString("ru-RU", { ...MSK, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
const hm = (t) => new Date(t * 1000).toLocaleTimeString("ru-RU", { ...MSK, hour: "2-digit", minute: "2-digit" });
const plural = (n, [one, few, many]) => { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many; };
// Первое сообщение в таблице: обрезка по границе слова с многоточием
const cut = (s, n = 140) => s.length > n ? s.slice(0, n).replace(/\s+\S*$/, "") + "…" : s;

// Ключ оператора хранится в sessionStorage вкладки: из адресной строки он убирается сразу,
// а переходы по меню на другие экраны и обратно его не теряют
const token = (() => {
  const url = new URL(location.href), given = url.searchParams.get("token");
  if (given !== null) { url.searchParams.delete("token"); history.replaceState(null, "", url.pathname + url.search + url.hash); }
  try {
    if (given) sessionStorage.setItem("op_token", given);
    return given || sessionStorage.getItem("op_token") || "";
  } catch (e) { return given || ""; }
})();
const head = token ? { "X-Admin-Token": token } : {};
// Чтение с сервера: отказ или ответ не в JSON бросает ошибку с кодом и телом ответа
const get = async (u) => {
  const r = await fetch(u, { headers: head });
  const data = await r.json().catch(() => null);
  if (!r.ok || data === null) throw Object.assign(new Error("ответ " + r.status), { status: r.status, data });
  return data;
};
const fail = (text) => { const e = $("err"); e.textContent = text; e.hidden = !text; if (text) e.scrollIntoView({ block: "nearest" }); };
// Действие оператора: при отказе сервера или обрыве сети возвращает null и показывает причину, экран не перерисовывается
const post = async (u, body) => {
  try {
    const r = await fetch(u, { method: "POST", headers: { ...head, "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401 || r.status === 403) { fail("Не отправлено: нет прав. Откройте экран с ключом оператора."); return null; }
    if (r.status >= 500) { fail("Не отправлено: ошибка сервера, попробуйте еще раз."); return null; }
    if (!r.ok) { fail(`Не выполнено (${r.status}): ${typeof data.detail === "string" ? data.detail : "сервер отклонил запрос"}. Повторите.`); return null; }
    fail("");
    return data;
  } catch (e) {
    fail("Не выполнено: нет связи с сервером. Текст на месте, повторите отправку.");
    return null;
  }
};
// Поля и кнопки заблокированы, пока идет запрос: двойной клик не дает второго ответа клиенту
const locked = async (els, fn) => {
  if (els.some((e) => e.disabled)) return null;
  els.forEach((e) => (e.disabled = true));
  try { return await fn(); } finally { els.forEach((e) => (e.disabled = false)); }
};
const STATUS = { new: "ждет менеджера", taken: "в работе", answered: "отвечено", returned: "возвращено агенту" };
const TOOL = {  // подписи шагов как в чате покупателя
  search_products: "Поиск в каталоге", get_product: "Варианты лицензий", browse_categories: "Категории каталога",
  find_analogs: "Аналоги", kb_search: "База знаний", note_facts: "Память о клиенте", calc_total: "Расчет суммы",
  make_quote: "Черновик КП", handoff_to_manager: "Передача менеджеру",
};
const GUARD = { price: "Проверка цен", discount: "Проверка скидки", wording: "Проверка текста" };
// Допущения экономики: ключ запроса, ключ в ответе, подпись, пояснение, шаг, границы как у сервера
const FIELDS = [
  ["volume", "requests_per_half_year", "Обращений за полгода", "цифра заказчика", 1, 1, 10000000],
  ["minutes", "minutes_per_request", "Минут менеджера на обращение", "оценка заказчика на брифе", 1, 1, 600],
  ["rub_hour", "manager_rub_per_hour", "Стоимость часа менеджера, ₽", "допущение", 50, 0, 100000],
  ["card_share", "card_saves_share", "Сколько времени менеджера снимает готовая карточка, %", "допущение, от 0 до 100", 5, 0, 100],
];
const eco = {};  // допущения, которые оператор поменял на экране, в виде параметров запроса
const view = { queue: "wait", runOpen: false, rows: "all", find: "" };  // фильтры живут вне load: автообновление их не сбрасывает
let current = "", from = "", operator = false, data = null, model = {}, shown = 0;

function span(sec) {
  const m = Math.round(Math.abs(sec) / 60);
  return m < 60 ? m + " мин" : Math.floor(m / 60) + " ч " + (m % 60) + " мин";
}

function card(h, now) {
  const c = h.card, done = h.status === "answered" || h.status === "returned", left = c.sla_due - now;
  const late = !done && !h.from_run && left < 0;
  const due = done ? STATUS[h.status] : h.from_run ? "диалог проверочного прогона"
    : late ? `просрочено на ${span(left)}` : `ответить до ${left > 12 * 3600 ? dt(c.sla_due) : hm(c.sla_due)} МСК, осталось ${span(left)}`;
  const kv = (o) => Object.entries(o).map(([k, v]) => `${esc(k)}: <b>${esc(v)}</b>`).join(" · ");
  const offers = c.offers.slice(0, 3).map((o) => `${esc(o.name)}${o.qty > 1 ? ", " + num(o.qty) + " шт." : ""}: ${o.price ? rub(o.price) : "цена по запросу"}${o.list_price ? ` (со скидкой ${num(o.discount_percent)}%, по каталогу ${rub(o.list_price)})` : ""}`).join("<br>");
  const money = [c.calc_total ? `расчет ${rub(c.calc_total)}` : "", c.quote ? `${c.quote.url ? `<a href="${esc(c.quote.url)}" target="_blank" rel="noopener">черновик ${esc(c.quote.number)}</a>` : `черновик ${esc(c.quote.number)}`} на ${rub(c.quote.total)}${c.quote.url ? "" : ", КП доступен оператору"}` : ""].filter(Boolean).join(" · ");
  const client = [kv(c.client), c.client["контакт"] || h.from_run || done ? "" : '<em class="nc">контакта нет: ответьте здесь, клиент увидит ответ в чате сайта</em>'].filter(Boolean).join("<br>");
  const rows = [
    ["Нужно", esc(h.summary)],
    c.next ? ["Дальше", esc(c.next), "next"] : null,
    ["Клиент", client],
    Object.keys(c.needs).length ? ["Запрос", kv(c.needs)] : null,
    offers ? ["Позиции", offers] : null,
    money ? ["Сумма", money] : null,
  ].filter(Boolean).map(([a, b, cls]) => `<div class="row${cls ? " " + cls : ""}"><span>${a}</span><span>${b}</span></div>`).join("");
  // на диалоги проверочного прогона менеджер не отвечает: кнопок у них нет
  const buttons = operator && !done && !h.from_run ? (h.status === "new" ? `<button type="button" class="btn main" data-do="take" data-f="t${h.id}">Взять в работу</button>` : "") + `<button type="button" class="btn" data-do="return" data-f="r${h.id}">Вернуть агенту</button>` : "";
  const who = !done && c.assignee ? `<em>в работе: ${esc(c.assignee)} с ${hm(c.taken_at)} МСК</em>` : "";
  return `<article class="card ${c.priority}${done ? " done" : ""}" data-id="${esc(h.session_id)}" data-h="${h.id}">
    <div class="hd"><span class="tag ${c.priority === "high" ? "u" : ""}">срочность ${esc(c.priority_name)}</span><button type="button" class="open" data-f="o${h.id}">№ ${h.id} · ${esc(h.reason)}</button>${c.flags.map((f) => `<span class="tag h">${esc(f)}</span>`).join("")}<span class="due${late ? " late" : ""}">${dt(h.ts)} МСК · ${due}</span></div>
    ${rows}${buttons || who ? `<div class="act">${buttons}${who}</div>` : ""}</article>`;
}

// Перерисовка списка без потери фокуса клавиатуры: элемент с тем же data-f получает фокус снова
function paint(el, html) {
  const f = el.contains(document.activeElement) ? document.activeElement.dataset.f : "";
  el.innerHTML = html;
  if (f) el.querySelector(`[data-f="${CSS.escape(f)}"]`)?.focus({ preventScroll: true });
  el.querySelectorAll("[data-id]").forEach((x) => x.classList.toggle("on", x.dataset.id === current));
}

function renderQueue() {
  if (!data) return;
  const { st } = data, open = (h) => h.status === "new" || h.status === "taken";
  const live = st.handoffs.filter((h) => !h.from_run), run = st.handoffs.filter((h) => h.from_run);
  const lists = { wait: live.filter(open), all: live, run };
  const label = { wait: "Ждут ответа", all: "Все", run: "Проверочный прогон" };
  $("qseg").querySelectorAll("button").forEach((b) => { b.textContent = `${label[b.dataset.q]} ${lists[b.dataset.q].length}`; b.setAttribute("aria-pressed", b.dataset.q === view.queue); });
  const cards = (list) => list.map((h) => card(h, st.now)).join("");
  let html = cards(lists[view.queue]);
  const folded = view.queue === "wait" ? run.filter(open) : view.queue === "all" ? run : [];
  if (folded.length) {
    html += `<div class="fold"><span>Проверочный прогон: ${folded.length} ${plural(folded.length, ["обращение", "обращения", "обращений"])}. Это диалоги набора проверки агента, менеджеры на них не отвечают.</span><button type="button" class="btn" data-fold data-f="fold">${view.runOpen ? "Скрыть" : "Показать"}</button></div>`;
    if (view.runOpen) html += cards(folded);
  }
  const empty = { wait: "Сейчас никто не ждет ответа менеджера.", all: "Передач менеджеру пока нет.", run: "Обращений проверочного прогона нет." };
  paint($("hand"), html || `<div class="empty">${empty[view.queue]}</div>`);
}

const ROWS = { all: () => true, handoff: (s) => s.status === "handoff", agent: (s) => s.status !== "handoff", quote: (s) => s.quotes > 0 };

function renderRows() {
  if (!data) return;
  const all = data.ss.sessions, q = view.find.trim().toLowerCase();
  const list = all.filter(ROWS[view.rows]).filter((s) => !q || (s.first || "").toLowerCase().includes(q));
  $("rseg").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.r === view.rows));
  $("rown").textContent = list.length === all.length ? "" : `показано ${list.length} из ${all.length}`;
  paint($("rows"), list.map((s) => `<tr data-id="${esc(s.id)}"><td>${dt(s.updated_at)}</td><td><button type="button" class="open" data-f="d${esc(s.id)}">${esc(cut(s.first || "без сообщений клиента"))}</button></td>
    <td>${s.status === "handoff" ? '<span class="tag h">менеджеру</span>' : '<span class="tag">агент</span>'}${s.quotes ? ' <span class="tag">КП</span>' : ""}</td>
    <td class="num">${s.turns}</td><td class="num">${num(s.prompt_tokens + s.completion_tokens)}</td><td class="num">${rub(s.cost_rub)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty">${all.length ? "Под выбранный фильтр диалогов нет." : operator ? "Диалогов пока нет. Напишите в чат покупателя." : "Демонстрационных диалогов на стенде нет."}</td></tr>`);
}

// Часы из рублей по ставке часа: строки "снято с менеджеров" и "стоимость снятых часов" сходятся
function hours(x, rate) {
  const m = Math.round((rate > 0 ? x.saved_rub / rate : x.saved_hours) * 60);
  return `${num(Math.floor(m / 60))} ч ${m % 60} мин`;
}

function economy(e) {
  const a = e.assumptions, m = e.measured, hy = e.half_year, rate = a.manager_rub_per_hour, n = a.requests_per_half_year;
  const word = plural(n, ["обращении", "обращениях", "обращениях"]);
  $("ecosum").innerHTML = `<b>Экономика</b>${hy ? `<span>за полгода при ${num(n)} ${word}: экономит <b>${rub(hy.saved_rub)}</b> времени менеджеров, модель стоит <b>${rub(hy.model_cost_rub)}</b></span>`
    : "<span>появится после первых диалогов на стенде</span>"}<span class="more">расчет и допущения</span>`;
  const part = (title, x, rows) => x ? `<div><h3>${title}</h3><div class="big">${rub(x.net_rub)}</div><p class="cap">выгода за вычетом расхода на модель</p><dl>
      ${rows}<dt>Снято с менеджеров</dt><dd>${hours(x, rate)}</dd><dt>Стоимость снятых часов</dt><dd>${rub(x.saved_rub)}</dd>
      <dt>Расход на модель${x.model_budget_rub ? "<i>бюджет заказчика 5-7 тыс. ₽</i>" : ""}</dt><dd>${rub(x.model_cost_rub)}</dd><dt>Рубль на модель возвращает</dt><dd>${x.return_x == null ? "нет данных" : num(x.return_x, 1) + " ₽"}</dd></dl></div>`
    : `<div><h3>${title}</h3><div class="big">нет данных</div><p class="cap">Появится после первых диалогов на стенде.</p></div>`;
  $("ecores").innerHTML = part("На этом стенде", e.stand, m ? `<dt>Обращений клиентов</dt><dd>${num(m.dialogs)}</dd><dt>Из них без менеджера</dt><dd>${num(m.closed)} (${Math.round(m.closed_share * 100)}%)</dd>` : "")
    + part(`За полгода при ${num(n)} ${word}`, hy, hy ? `<dt>Закроет сам</dt><dd>${num(hy.closed)}</dd><dt>Передаст карточкой</dt><dd>${num(hy.handed)}</dd>` : "");
  const value = (key, v) => key === "card_share" ? Math.round(v * 100) : v;
  const pd = e.per_workday, day = pd && Math.round(pd.requests);
  $("ecoin").innerHTML = `<h3>На чем стоит расчет: поменяйте допущения под свои данные</h3>
    ${FIELDS.map(([key, ans, label, hint, step, lo, hi]) => `<label><span>${label}<i>${hint}</i></span><input type="number" inputmode="decimal" min="${lo}" max="${hi}" step="${step}" data-eco="${key}" value="${value(key, a[ans])}"></label>`).join("")}
    ${pd ? `<label><span>Расход на модель в рабочий день<i>${day ? `около ${num(day)} ${plural(day, ["обращения", "обращений", "обращений"])}` : "меньше одного обращения"}, ${num(pd.tokens)} ${plural(pd.tokens, ["токен", "токена", "токенов"])}; 21 рабочий день в месяце</i></span><span class="tag">${rub(pd.model_cost_rub)}</span></label>` : ""}
    <label><span>Доля без менеджера среди всех обращений и стоимость обращения<i>замер этого стенда, от этой доли идет расчет; вызов менеджера кнопкой входит в обращения</i></span><span class="tag">${m ? Math.round(m.closed_share * 100) + "% · " + rub(m.cost_per_dialog_rub) : "нет данных"}</span></label>`;
  $("ecoin").querySelectorAll("input").forEach((i) => {
    i.onchange = () => apply(i);
    i.onkeydown = (ev) => { if (ev.key === "Enter") apply(i); };
  });
}

// Пересчет по Enter и по уходу из поля. Значение вне границ на сервер не уходит: расчет и автообновление идут по прежнему
function apply(i) {
  const [key, , label, , , lo, hi] = FIELDS.find((f) => f[0] === i.dataset.eco), v = i.value === "" ? NaN : Number(i.value);
  const warn = $("ecowarn");
  if (!(v >= lo && v <= hi)) {
    i.setAttribute("aria-invalid", "true");
    warn.textContent = `${label}: допустимо от ${num(lo)} до ${num(hi)}. Расчет по прежнему значению.`;
    warn.hidden = false;
    return;
  }
  i.removeAttribute("aria-invalid");
  warn.hidden = true;
  const val = String(key === "card_share" ? v / 100 : v);
  if (eco[key] === val) return;
  eco[key] = val;
  load(true);
}

get("/api/health").then((h) => { model = h; $("model").textContent = "модель " + h.label + (h.online ? "" : ", сейчас не на связи"); }).catch(() => {});

function catalogLine(cat) {
  const lu = cat.last_update || {};
  const feed = /^(\d{4})-(\d{2})-(\d{2})/.exec(cat.feed_date || "");
  const upd = cat.updating ? "идет обновление каталога" : !lu.finished ? `обновление с фида каждый день в ${cat.update_hour_msk}:00 МСК`
    : lu.ok ? `обновлен ${dt(lu.finished)} МСК: добавилось ${num(lu.added ?? 0)}, ушло ${num(lu.removed ?? 0)}, цена изменилась у ${num(lu.price_changed ?? 0)}`
    : `обновление ${dt(lu.finished)} МСК не прошло, работает прежняя база: ${lu.error}`;
  return `Каталог: ${num(cat.offers)} предложений, фид от ${feed ? `${feed[3]}.${feed[2]}.${feed[1]}` : cat.feed_date || "нет данных"}; ${upd}.`;
}

// Счетчики обучения: при нулях объясняют, откуда берутся
function learnLine(st) {
  return (st.learned_answers ? `Ответов менеджеров в знаниях агента: ${num(st.learned_answers)}.`
    : 'Ответов менеджеров в знаниях агента пока нет: ответ с отметкой "общий ответ" попадает в знания агента.')
    + " " + (st.added_cases ? `Диалогов со стенда в проверочном наборе: ${num(st.added_cases)}.`
    : 'Диалогов со стенда в проверочном наборе пока нет: их добавляет кнопка "В проверочный набор" в панели диалога.');
}

async function load(force = false) {
  const q = new URLSearchParams(Object.entries(eco)).toString();
  let st, ss, cat;
  try {
    [st, ss, cat] = await Promise.all([get("/api/admin/stats" + (q ? "?" + q : "")), get("/api/admin/sessions"), get("/api/admin/catalog")]);
  } catch (e) {
    // допущение, которое сервер не принял, снимается, и экран обновляется по прежнему значению
    const bad = e.status === 422 ? (e.data?.detail || []).map((x) => x.loc?.[1]).filter((k) => k in eco) : [];
    if (bad.length) {
      bad.forEach((k) => delete eco[k]);
      $("ecowarn").textContent = "Сервер не принял значение допущения: расчет по прежнему значению.";
      $("ecowarn").hidden = false;
      return load(true);
    }
    $("lerr").querySelector("span").textContent = data
      ? `Данные не обновились: сервер не ответил (${e.status || "нет связи"}). На экране состояние на ${hm(data.st.now)} МСК, следующая попытка через 15 секунд.`
      : `Данные экрана не загрузились: сервер не ответил (${e.status || "нет связи"}). Следующая попытка через 15 секунд.`;
    $("lerr").hidden = false;
    return;
  }
  $("lerr").hidden = true;
  data = { st, ss };
  operator = st.role === "operator";
  const badKey = token && !operator;
  if (badKey) try { sessionStorage.removeItem("op_token"); } catch (e) { /* хранилище недоступно: ключ и так не сохранен */ }
  // без ключа сервер отдает только диалоги прогона и показа: рабочие диалоги посетителей видит только оператор
  $("demo").textContent = badKey
    ? "Ключ оператора не подошел: открыт режим просмотра, в нем только демонстрационные диалоги проверочного прогона. Откройте экран по ссылке с верным ключом."
    : "Режим просмотра: показаны демонстрационные диалоги проверочного прогона, контакты в них скрыты. Рабочие диалоги, ответы клиентам, выгрузка журнала и обновление каталога доступны по ссылке с ключом оператора.";
  $("demo").hidden = operator;
  $("snote").textContent = st.stand_note || "";
  $("snote").hidden = !st.stand_note;
  document.querySelectorAll(".op").forEach((e) => (e.hidden = !operator));
  $("cat").textContent = catalogLine(cat);
  $("learn").textContent = learnLine(st);
  const qu = st.queue, late = qu.overdue > 0;  // счетчики сервер считает по всей открытой очереди, а не по показанным карточкам
  const pct = (v) => v == null ? "нет данных" : Math.round(v * 100) + "%";
  $("kpi").innerHTML = `<a href="#queue" id="towait" class="${late ? "late" : ""}"><b>${num(qu.waiting)}</b><span>ждут менеджера${late ? `, просрочено ${num(qu.overdue)}` : ""}</span></a>` + [
    // две доли рядом, у каждой числитель и знаменатель: число диалогов и обращений видно в них
    [pct(st.without_human_share), `без передачи среди диалогов с агентом: ${num(st.closed_dialogs)}\u00a0из\u00a0${num(st.dialogs)}`],
    [pct(st.requests_without_human_share), `без менеджера среди всех обращений, включая вызов менеджера кнопкой: ${num(st.closed_dialogs)}\u00a0из\u00a0${num(st.requests)}`],
    [num(st.quotes), "черновиков КП"],
    [rub(st.cost_rub_per_turn), "стоимость одного запроса"], [rub(st.cost_rub_per_dialog), "стоимость диалога"],
    [st.avg_model_seconds_per_turn == null ? "нет данных" : String(st.avg_model_seconds_per_turn).replace(".", ",") + " с", "модель думает на запрос"],
  ].map(([b, s]) => `<div><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join("");
  $("towait").onclick = () => { view.queue = "wait"; renderQueue(); };
  const focused = document.activeElement?.dataset?.eco;
  if (force || !focused) {
    economy(st.economy);
    if (focused) $("ecoin").querySelector(`[data-eco="${focused}"]`)?.focus();
  }
  $("handn").textContent = qu.waiting + qu.from_run ? `ждут ответа: ${qu.waiting}, из них просрочено: ${qu.overdue}` : "";
  renderQueue();
  renderRows();
}

// Клик или Enter по карточке или строке открывает диалог; кнопки внутри делают свое
async function pick(ev, list) {
  const el = ev.target.closest("[data-id]");
  if (ev.target.closest("[data-fold]")) { view.runOpen = !view.runOpen; renderQueue(); return; }
  if (!el || ev.target.closest("a")) return;
  const b = ev.target.closest("button[data-do]");
  if (!b) { from = list; show(el.dataset.id, true); return; }
  const h = el.dataset.h;
  if (!await locked([...b.closest(".act").querySelectorAll("button")], () => post(`/api/admin/handoff/${h}/${b.dataset.do}`, b.dataset.do === "take" ? { who: localStorage.getItem("who") || "менеджер" } : {}))) return;
  await load(); if (current) show(current);
}
$("hand").onclick = (ev) => pick(ev, "hand");
$("rows").onclick = (ev) => pick(ev, "rows");
$("qseg").onclick = (ev) => { const b = ev.target.closest("button"); if (b) { view.queue = b.dataset.q; renderQueue(); } };
$("rseg").onclick = (ev) => { const b = ev.target.closest("button"); if (b) { view.rows = b.dataset.r; renderRows(); } };
$("find").oninput = () => { view.find = $("find").value; renderRows(); };

function step(s) {
  if (s.kind === "llm") {
    const tc = s.output?.tool_call;
    return `<div class="st"><b>Модель</b> ${esc(s.name === model.model ? model.label : s.name)} · ${num(s.prompt_tokens)}+${num(s.completion_tokens)} токенов${s.seconds ? " · " + num(s.seconds, 1) + " с" : ""} → ${tc ? "вызывает " + esc(TOOL[tc.name] || tc.name) : "отвечает клиенту"}</div>`;
  }
  const title = s.kind === "tool" ? TOOL[s.name] || s.name : s.kind === "guard" ? GUARD[s.name] || "Проверка ответа" : "Сбой";
  return `<div class="st ${esc(s.kind)}"><b>${esc(title)}</b><details><summary>Запрос и ответ</summary><pre>${esc(JSON.stringify(s.input))}\n${esc(JSON.stringify(s.output).slice(0, 1500))}</pre></details></div>`;
}

function stepsBlock(list) {
  if (!list.length) return "";
  const names = [...new Set(list.filter((x) => x.kind === "tool").map((x) => TOOL[x.name] || x.name))];
  return `<details class="steps"><summary>Шаги агента: ${list.length}${names.length ? " · " + esc(names.join(", ")) : ""}</summary>${list.map(step).join("")}</details>`;
}

function answerBox(h) {
  return `<div class="ans" data-h="${h.id}"><label class="who" for="a${h.id}">Ответ клиенту на обращение № ${h.id}</label>
    <div>${h.card.templates.map((t, i) => `<button type="button" class="btn" data-t="${i}">${esc(t.title)}</button>`).join("")}</div>
    <textarea id="a${h.id}" placeholder="Ответ увидит клиент в чате сайта"></textarea>
    <label class="chk"><input type="checkbox"> Общий ответ, без данных этого клиента: агент будет так отвечать на похожие вопросы</label>
    <div><button type="button" class="btn main" data-send>Отправить клиенту</button><span class="hint" aria-live="polite"></span></div></div>`;
}

async function show(id, opened = false) {
  current = id;
  document.querySelectorAll("#hand [data-id], #rows [data-id]").forEach((el) => el.classList.toggle("on", el.dataset.id === id));
  const seq = ++shown;  // быстрые клики: панель рисует только последний выбранный диалог
  let d;
  try { d = await get("/api/admin/session/" + encodeURIComponent(id)); } catch (e) {
    if (seq !== shown) return;
    $("detail").innerHTML = `<p class="empty">${e.status === 404 ? "Этого диалога больше нет на стенде." : "Диалог не открылся: сервер не ответил."}</p><button type="button" class="btn back">К списку обращений</button>`;
    return;
  }
  if (seq !== shown) return;
  const s = d.session, steps = d.steps || [];
  const facts = Object.entries(s.facts || {}).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("; ");
  const count = (kind) => steps.filter((x) => x.kind === kind).length;
  let html = `<div class="dh"><h2 id="dh" tabindex="-1">Диалог от ${dt(s.created_at)} МСК</h2><button type="button" class="btn back">К списку обращений</button></div>
    <p class="id">№ ${esc(id)}${s.page_url ? " · страница " + esc(s.page_url) : ""}</p>
    <div class="st">Стоимость ${rub(s.cost_rub)} · вызовов модели ${count("llm")} · инструментов ${count("tool")}${facts ? "<br>Память о клиенте: " + facts : ""}</div>`;
  if (operator) html += d.handoffs.filter((x) => x.status === "new" || x.status === "taken").map(answerBox).join("");
  html += "<h3>Переписка</h3>";
  let turn = 0;
  for (const m of d.messages) {
    if (m.role === "user") {
      turn++;
      html += `<div class="who">Клиент</div><div class="msg user">${esc(m.content)}</div>${stepsBlock(steps.filter((x) => x.turn === turn))}`;
    } else {
      const mgr = m.meta?.from === "manager";
      html += `<div class="who">${mgr ? "Менеджер" : "Агент"}</div><div class="msg ${mgr ? "mgr" : "bot"}">${esc(m.content)}</div>`;
      for (const c of m.meta?.cards || []) html += `<div class="st">Карточка: ${esc(c.title)}${c.price ? ", " + rub(c.price) : ""}</div>`;
    }
  }
  for (const q of d.quotes) html += `<div class="st tool"><b>КП ${esc(q.number)}</b> ${esc(q.customer)}, итого ${rub(q.total)} · ${q.url ? `<a href="${esc(q.url)}" target="_blank" rel="noopener">открыть PDF</a>` : "КП доступен оператору"}</div>`;
  if (operator) {
    html += `<div class="case"><h3>Агент ошибся?</h3><p>Диалог станет кейсом проверочного набора: следующий прогон его проиграет.</p>
      <label for="cw">Как должен был поступить агент</label>
      <select id="cw"><option value="no">Ответить сам</option><option value="yes">Передать менеджеру</option><option value="any">Можно и так и так</option></select>
      <label class="chk"><input type="checkbox"> Должен был сформировать черновик КП</label>
      <label for="cn">Что не так, одной фразой</label><input type="text" id="cn" maxlength="500">
      <div><button type="button" class="btn">В проверочный набор</button><span class="tag" hidden></span></div></div>`;
  }
  const panel = $("detail");
  panel.innerHTML = html;
  panel.classList.add("has");
  panel.scrollTop = 0;
  panel.querySelectorAll(".ans").forEach((box) => {
    const h = d.handoffs.find((x) => String(x.id) === box.dataset.h), area = box.querySelector("textarea"), hint = box.querySelector(".hint");
    box.querySelectorAll("button[data-t]").forEach((b) => (b.onclick = () => { area.value = h.card.templates[b.dataset.t].text; hint.textContent = ""; area.focus(); }));
    box.querySelector("button[data-send]").onclick = async () => {
      const answer = area.value.trim();
      if (!answer) { hint.textContent = "Напишите ответ или выберите заготовку выше."; area.focus(); return; }
      hint.textContent = "";
      const teach = box.querySelector("input").checked;
      const sent = await locked([area, ...box.querySelectorAll("button, input")],
        () => post(`/api/admin/handoff/${box.dataset.h}/answer`, { answer, teach }));
      if (!sent) return;  // ответ клиенту не ушел: текст менеджера остается в поле
      area.value = "";  // повторный клик до перерисовки ничего не отправит
      await load(); show(id);
    };
  });
  const box = panel.querySelector(".case");
  if (box) box.querySelector("button").onclick = async () => {
    const pickv = box.querySelector("select").value, quote = box.querySelector("input[type=checkbox]").checked, note = box.querySelector("input[type=text]").value;
    const r = await locked([...box.querySelectorAll("button, select, input")],
      () => post(`/api/admin/session/${id}/case`, { handoff: pickv === "any" ? null : pickv === "yes", quote, note }));
    const tag = box.querySelector(".tag");
    tag.hidden = false; tag.textContent = r && r.ok ? `добавлен кейс ${r.case_id}, со стенда в наборе: ${r.added_cases}` : "не получилось добавить";
    load();
  };
  // на телефоне и планшете панель стоит под очередью: выбор прокручивает к ней, фокус уходит на заголовок
  if (opened) {
    const narrow = matchMedia("(max-width:1100px)").matches;
    if (narrow) panel.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth" });
    $("dh").focus({ preventScroll: true });
  }
}

// "К списку обращений": обратно к карточке или строке, с которой открыт диалог
$("detail").addEventListener("click", (ev) => {
  if (!ev.target.closest(".back")) return;
  const el = document.querySelector(`#${from || "hand"} [data-id="${CSS.escape(current)}"]`) || $("queue");
  el.scrollIntoView({ block: "start" });
  (el.querySelector(".open") || el).focus?.({ preventScroll: true });
});

$("upd").onclick = async () => { if (await locked([$("upd")], () => post("/api/admin/catalog/update"))) load(); };
// Журнал выгружается запросом с ключом в заголовке: ключ не попадает ни в адрес, ни в журнал запросов сервера
$("exp").onclick = () => locked([$("exp")], async () => {
  try {
    const r = await fetch("/api/admin/export.csv", { headers: head });
    if (!r.ok) throw new Error("ответ " + r.status);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = "dialogs.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    fail("");
  } catch (e) {
    fail("Журнал не выгрузился: сервер не ответил или нет прав. Повторите.");
  }
});
$("retry").onclick = () => load();
const busy = () => document.querySelector(".ans textarea:focus, .case input:focus, .case select:focus") || [...document.querySelectorAll(".ans textarea, .case input[type=text]")].some((e) => e.value);
load(); setInterval(() => { if (!busy()) load(); }, 15000);
