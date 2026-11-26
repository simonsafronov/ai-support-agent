// Экран оператора: показатели, экономика, карточки обращений, диалоги с шагами агента, ответ клиенту.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 0) => Number(v).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
const rub = (v) => { if (v == null) return "нет данных"; const n = Number(v); return num(n, Number.isInteger(n) ? 0 : 2) + " ₽"; };
const dt = (t) => t ? new Date(t * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
const hm = (t) => new Date(t * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
const token = new URLSearchParams(location.search).get("token") || "";
const head = token ? { "X-Admin-Token": token } : {};
const get = async (u) => (await fetch(u, { headers: head })).json();
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
if (token) $("exp").href += "?token=" + encodeURIComponent(token);
const STATUS = { new: "ждет менеджера", taken: "в работе", answered: "отвечено", returned: "возвращено агенту" };
const eco = {};  // допущения, которые оператор поменял на экране
let current = "", operator = false;

function span(sec) {
  const m = Math.round(Math.abs(sec) / 60);
  return m < 60 ? m + " мин" : Math.floor(m / 60) + " ч " + (m % 60) + " мин";
}

function card(h, now) {
  const c = h.card, done = h.status === "answered" || h.status === "returned";
  const left = c.sla_due - now;
  const due = done ? STATUS[h.status] : h.from_run ? "диалог проверочного прогона" : left < 0 ? `просрочено на ${span(left)}` : `ответить до ${left > 12 * 3600 ? dt(c.sla_due) : hm(c.sla_due)}, осталось ${span(left)}`;
  const kv = (o) => Object.entries(o).map(([k, v]) => `${esc(k)}: <b>${esc(v)}</b>`).join(" · ");
  const offers = c.offers.slice(0, 3).map((o) => `${esc(o.name)}${o.qty > 1 ? ", " + num(o.qty) + " шт." : ""}: ${o.price ? rub(o.price) : "цена по запросу"}`).join("<br>");
  const money = [c.calc_total ? `расчет ${rub(c.calc_total)}` : "", c.quote ? `${c.quote.url ? `<a href="${esc(c.quote.url)}" target="_blank" rel="noopener">черновик ${esc(c.quote.number)}</a>` : `черновик ${esc(c.quote.number)}`} на ${rub(c.quote.total)}${c.quote.url ? "" : ", КП доступен оператору"}` : ""].filter(Boolean).join(" · ");
  const rows = [
    ["Нужно", esc(h.summary)],
    ["Клиент", kv(c.client) || "о себе не рассказал, контакт не оставил"],
    Object.keys(c.needs).length ? ["Запрос", kv(c.needs)] : null,
    offers ? ["Позиции", offers] : null,
    money ? ["Сумма", money] : null,
  ].filter(Boolean).map(([a, b]) => `<div class="row"><span>${a}</span><span>${b}</span></div>`).join("");
  const act = done ? "" : `<div class="act">${operator ? (h.status === "new" ? '<button type="button" class="btn main" data-do="take">Взять в работу</button>' : "") + '<button type="button" class="btn" data-do="return">Вернуть агенту</button>' : ""}${c.assignee ? `<em>в работе: ${esc(c.assignee)} с ${hm(c.taken_at)}</em>` : ""}</div>`;
  return `<article class="card ${c.priority}${done ? " done" : ""}" data-id="${esc(h.session_id)}" data-h="${h.id}">
    <div class="hd"><span class="tag ${c.priority === "high" ? "u" : ""}">срочность ${esc(c.priority_name)}</span><b>№ ${h.id} · ${esc(h.reason)}</b>${c.flags.map((f) => `<span class="tag h">${esc(f)}</span>`).join("")}<span class="due${!done && !h.from_run && left < 0 ? " late" : ""}">${dt(h.ts)} · ${due}</span></div>
    ${rows}${act}</article>`;
}

function economy(e) {
  const a = e.assumptions, m = e.measured, field = (key, label, hint, step) => `<label><span>${label}<i>${hint}</i></span><input type="number" min="0" step="${step}" data-eco="${key}" value="${a[{ minutes: "minutes_per_request", rub_hour: "manager_rub_per_hour", card_share: "card_saves_share", volume: "requests_per_half_year" }[key]]}"></label>`;
  const part = (title, x, extra) => x ? `<div><h3>${title}</h3><div class="big">${rub(x.net_rub)}</div><dl>
      ${extra}<dt>Снято с менеджеров</dt><dd>${num(x.saved_hours, 1)} ч</dd><dt>Это стоит компании</dt><dd>${rub(x.saved_rub)}</dd>
      <dt>Расход на модель${x.model_budget_rub ? `, заказчик согласен на ${rub(x.model_budget_rub)}` : ""}</dt><dd>${rub(x.model_cost_rub)}</dd><dt>Рубль на модель возвращает</dt><dd>${x.return_x == null ? "нет данных" : num(x.return_x, 1) + " ₽"}</dd></dl></div>`
    : `<div><h3>${title}</h3><div class="big">нет данных</div><p class="sub">Появится после первых диалогов на стенде.</p></div>`;
  $("eco").innerHTML = `<h2>Экономика<small>выгода за вычетом расхода на модель; замеры стенда и допущения разделены</small></h2>
    <div class="res">
      ${part("На этом стенде", e.stand, m ? `<dt>Диалогов, из них без менеджера</dt><dd>${m.dialogs}, ${m.closed} (${Math.round(m.closed_share * 100)}%)</dd>` : "")}
      ${part(`За полгода при ${num(a.requests_per_half_year)} обращениях`, e.half_year, e.half_year ? `<dt>Закроет сам, передаст карточкой</dt><dd>${e.half_year.closed}, ${e.half_year.handed}</dd>` : "")}
    </div>
    <div class="in"><h3>На чем стоит расчет: поменяйте под свои данные</h3>
      ${field("volume", "Обращений за полгода", "цифра заказчика", 1)}
      ${field("minutes", "Минут менеджера на обращение", "оценка заказчика на брифе", 1)}
      ${field("rub_hour", "Стоимость часа менеджера, ₽", "допущение", 50)}
      ${field("card_share", "Доля времени, которую снимает готовая карточка", "допущение, от 0 до 1", 0.1)}
      ${e.per_workday ? `<label><span>Расход на модель в рабочий день<i>${num(e.per_workday.requests, 1)} обращения, ${num(e.per_workday.tokens)} токенов; 21 рабочий день в месяце</i></span><span class="tag">${rub(e.per_workday.model_cost_rub)}</span></label>` : ""}
      <label><span>Доля диалогов без менеджера и стоимость диалога<i>замер этого стенда</i></span><span class="tag">${m ? Math.round(m.closed_share * 100) + "% · " + rub(m.cost_per_dialog_rub) : "нет данных"}</span></label>
    </div>`;
  $("eco").querySelectorAll("input").forEach((i) => (i.onchange = () => { eco[i.dataset.eco] = i.value; load(); }));
}

get("/api/health").then((h) => { $("model").textContent = "модель " + h.label + (h.online ? "" : ", сейчас не на связи"); });

async function load() {
  const q = new URLSearchParams(Object.entries(eco).filter(([, v]) => v !== "")).toString();
  const [st, ss, cat] = await Promise.all([get("/api/admin/stats" + (q ? "?" + q : "")), get("/api/admin/sessions"), get("/api/admin/catalog")]);
  if (st.detail) return;  // допущение вне допустимых границ: экран остается прежним
  operator = st.role === "operator";
  $("demo").hidden = operator;
  $("snote").textContent = st.stand_note || "";
  $("snote").hidden = !st.stand_note;
  document.querySelectorAll(".op").forEach((e) => (e.hidden = !operator));
  const lu = cat.last_update || {};
  const upd = cat.updating ? "идет обновление каталога" : !lu.finished ? `обновление с фида каждый день в ${cat.update_hour_msk}:00 МСК`
    : lu.ok ? `обновлен ${dt(lu.finished)}: добавилось ${lu.added ?? 0}, ушло ${lu.removed ?? 0}, цена изменилась у ${lu.price_changed ?? 0}`
    : `обновление ${dt(lu.finished)} не прошло, работает прежняя база: ${lu.error}`;
  $("cat").textContent = `Каталог: ${num(cat.offers)} предложений, фид от ${cat.feed_date || "нет данных"}; ${upd}. Ответов менеджеров в знаниях агента: ${st.learned_answers}. Диалогов со стенда в проверочном наборе: ${st.added_cases}.`;
  const pct = st.without_human_share == null ? "нет данных" : Math.round(st.without_human_share * 100) + "%";
  $("kpi").innerHTML = [
    [st.dialogs, "диалогов"], [pct, "закрыто без менеджера"], [st.quotes, "черновиков КП"],
    [rub(st.cost_rub_per_turn), "стоимость одного запроса"], [rub(st.cost_rub_per_dialog), "стоимость диалога"],
    [st.avg_model_seconds_per_turn == null ? "нет данных" : String(st.avg_model_seconds_per_turn).replace(".", ",") + " с", "модель думает на запрос"],
  ].map(([b, s]) => `<div><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join("");
  if (!document.activeElement?.dataset?.eco) economy(st.economy);
  const qu = st.queue;  // счетчики сервер считает по всей открытой очереди, а не по показанным карточкам
  $("handn").textContent = qu.waiting + qu.from_run ? `ждут ответа: ${qu.waiting}, из них просрочено: ${qu.overdue}${qu.from_run ? `; из проверочного прогона: ${qu.from_run}` : ""}` : "";
  $("hand").innerHTML = st.handoffs.map((h) => card(h, st.now)).join("") || '<div class="empty">Передач менеджеру пока нет.</div>';
  $("rows").innerHTML = ss.sessions.map((s) => `<tr data-id="${esc(s.id)}"><td>${dt(s.created_at)}</td><td>${esc((s.first || "").slice(0, 140))}</td>
    <td>${s.status === "handoff" ? '<span class="tag h">менеджеру</span>' : '<span class="tag">агент</span>'}${s.quotes ? ' <span class="tag">КП</span>' : ""}</td>
    <td class="num">${s.turns}</td><td class="num">${num(s.prompt_tokens + s.completion_tokens)}</td><td class="num">${rub(s.cost_rub)}</td></tr>`).join("")
    || '<tr><td colspan="6" class="empty">Диалогов пока нет. Напишите в чат покупателя.</td></tr>';
  document.querySelectorAll("[data-id]").forEach((el) => {
    el.classList.toggle("on", el.dataset.id === current);
    el.onclick = (ev) => { if (!ev.target.closest("a,button")) show(el.dataset.id); };
  });
  document.querySelectorAll(".card button[data-do]").forEach((b) => (b.onclick = async () => {
    const h = b.closest(".card").dataset.h;
    if (!await locked([...b.closest(".act").querySelectorAll("button")], () => post(`/api/admin/handoff/${h}/${b.dataset.do}`, b.dataset.do === "take" ? { who: localStorage.getItem("who") || "менеджер" } : {}))) return;
    await load(); if (current) show(current);
  }));
}

async function show(id) {
  current = id;
  document.querySelectorAll("[data-id]").forEach((el) => el.classList.toggle("on", el.dataset.id === id));
  const d = await get("/api/admin/session/" + id);
  const facts = Object.entries(d.session.facts || {}).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("; ");
  let html = `<h2>Диалог ${esc(id)}</h2><div class="st">Стоимость ${rub(d.session.cost_rub)} · вызовов модели ${d.session.llm_calls} · инструментов ${d.session.tool_calls}${d.session.page_url ? " · страница " + esc(d.session.page_url) : ""}${facts ? "<br>Память о клиенте: " + facts : ""}</div>`;
  let turn = 0;
  for (const m of d.messages) {
    if (m.role === "user") {
      turn++;
      html += `<div class="who">Клиент</div><div class="msg user">${esc(m.content)}</div>`;
      for (const s of d.steps.filter((x) => x.turn === turn)) {
        if (s.kind === "llm") {
          const tc = s.output.tool_call;
          html += `<div class="st"><b>Модель</b> ${esc(s.name)} · ${s.prompt_tokens}+${s.completion_tokens} токенов${s.seconds ? " · " + s.seconds.toFixed(1).replace(".", ",") + " с" : ""}${tc ? " → вызывает " + esc(tc.name) : " → отвечает клиенту"}</div>`;
        } else {
          html += `<div class="st ${esc(s.kind)}"><b>${esc(s.name)}</b> ${esc(JSON.stringify(s.input))}<pre>${esc(JSON.stringify(s.output).slice(0, 1500))}</pre></div>`;
        }
      }
    } else {
      html += `<div class="who">${m.meta.from === "manager" ? "Менеджер" : "Агент"}</div><div class="msg bot">${esc(m.content)}</div>`;
      for (const c of m.meta.cards || []) html += `<div class="st">Карточка: ${esc(c.title)}${c.price ? ", " + rub(c.price) : ""}</div>`;
    }
  }
  for (const q of d.quotes) html += `<div class="st tool"><b>КП ${esc(q.number)}</b> ${esc(q.customer)}, итого ${rub(q.total)} · ${q.url ? `<a href="${esc(q.url)}" target="_blank" rel="noopener">открыть PDF</a>` : "КП доступен оператору"}</div>`;
  if (operator) {
    for (const h of d.handoffs.filter((x) => x.status === "new" || x.status === "taken")) html += `<div class="ans" data-h="${h.id}"><div class="who">Ответ менеджера на обращение № ${h.id}</div>
      <div>${h.card.templates.map((t, i) => `<button type="button" class="btn" data-t="${i}">${esc(t.title)}</button>`).join("")}</div>
      <textarea placeholder="Ответ увидит клиент в чате"></textarea>
      <label><input type="checkbox"> Ответ общий, без данных этого клиента: агент будет отвечать так на похожие вопросы</label>
      <div><button type="button" class="btn main" data-send>Отправить клиенту</button></div></div>`;
    html += `<div class="case"><div class="who">Агент ошибся? Диалог станет кейсом проверочного набора: следующий прогон его проиграет</div>
      <select><option value="no">Должен был ответить сам, без менеджера</option><option value="yes">Должен был передать менеджеру</option><option value="any">Допустимо и так и так</option></select>
      <label><input type="checkbox"> Должен был сформировать черновик КП</label>
      <input type="text" maxlength="500" placeholder="Что не так, одной фразой">
      <div><button type="button" class="btn">В проверочный набор</button><span class="tag" hidden></span></div></div>`;
  }
  $("detail").innerHTML = html;
  $("detail").querySelectorAll(".ans").forEach((box) => {
    const h = d.handoffs.find((x) => String(x.id) === box.dataset.h), area = box.querySelector("textarea");
    box.querySelectorAll("button[data-t]").forEach((b) => (b.onclick = () => { area.value = h.card.templates[b.dataset.t].text; area.focus(); }));
    box.querySelector("button[data-send]").onclick = async () => {
      const answer = area.value.trim();
      if (!answer) return;
      const teach = box.querySelector("input").checked;
      const sent = await locked([area, ...box.querySelectorAll("button, input")],
        () => post(`/api/admin/handoff/${box.dataset.h}/answer`, { answer, teach }));
      if (!sent) return;  // ответ клиенту не ушел: текст менеджера остается в поле
      area.value = "";  // повторный клик до перерисовки ничего не отправит
      await load(); show(id);
    };
  });
  const box = $("detail").querySelector(".case");
  if (box) box.querySelector("button").onclick = async () => {
    const pick = box.querySelector("select").value, quote = box.querySelector("input[type=checkbox]").checked, note = box.querySelector("input[type=text]").value;
    const r = await locked([...box.querySelectorAll("button, select, input")],
      () => post(`/api/admin/session/${id}/case`, { handoff: pick === "any" ? null : pick === "yes", quote, note }));
    const tag = box.querySelector(".tag");
    tag.hidden = false; tag.textContent = r && r.ok ? `добавлен кейс ${r.case_id}, со стенда в наборе: ${r.added_cases}` : "не получилось добавить";
    load();
  };
}
$("upd").onclick = async () => { if (await locked([$("upd")], () => post("/api/admin/catalog/update"))) load(); };
const busy = () => document.querySelector(".ans textarea:focus, .case input:focus, .case select:focus, .eco input:focus") || [...document.querySelectorAll(".ans textarea, .case input[type=text]")].some((e) => e.value);
load(); setInterval(() => { if (!busy()) load(); }, 15000);
