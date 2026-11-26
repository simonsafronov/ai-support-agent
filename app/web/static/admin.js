// Экран оператора: показатели, очередь обращений, диалоги с шагами агента, ответ клиенту.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const rub = (v) => { if (v == null) return "нет данных"; const n = Number(v); return n.toLocaleString("ru-RU", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 }) + " ₽"; };
const dt = (t) => new Date(t * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const token = new URLSearchParams(location.search).get("token") || "";
const head = token ? { "X-Admin-Token": token } : {};
const get = async (u) => (await fetch(u, { headers: head })).json();
const post = async (u, body) => (await fetch(u, { method: "POST", headers: { ...head, "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })).json();
const day = (t) => t ? new Date(t * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
if (token) document.getElementById("exp").href += "?token=" + encodeURIComponent(token);
let current = "";

async function load() {
  const [st, ss, cat] = await Promise.all([get("/api/admin/stats"), get("/api/admin/sessions"), get("/api/admin/catalog")]);
  const lu = cat.last_update || {};
  const upd = cat.updating ? "идет обновление каталога" : !lu.finished ? `обновление с фида каждый день в ${cat.update_hour_msk}:00 МСК`
    : lu.ok ? `обновлен ${day(lu.finished)}: добавилось ${lu.added ?? 0}, ушло ${lu.removed ?? 0}, цена изменилась у ${lu.price_changed ?? 0}`
    : `обновление ${day(lu.finished)} не прошло, работает прежняя база: ${lu.error}`;
  $("cat").textContent = `Каталог: ${cat.offers.toLocaleString("ru-RU")} предложений, фид от ${cat.feed_date || "нет данных"}; ${upd}. Ответов менеджеров в знаниях агента: ${st.learned_answers}.`;
  const pct = st.without_human_share == null ? "нет данных" : Math.round(st.without_human_share * 100) + "%";
  $("kpi").innerHTML = [
    [st.dialogs, "диалогов"], [pct, "закрыто без менеджера"], [st.quotes, "черновиков КП"],
    [rub(st.cost_rub_per_turn), "стоимость одного запроса"], [rub(st.cost_rub_per_dialog), "стоимость диалога"],
    [st.avg_model_seconds_per_turn == null ? "нет данных" : String(st.avg_model_seconds_per_turn).replace(".", ",") + " с", "модель думает на запрос"],
  ].map(([b, s]) => `<div><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join("");
  $("hand").innerHTML = st.handoffs.map((h) => `<tr data-id="${esc(h.session_id)}"><td>№ ${h.id}<br><span class="tag">${dt(h.ts)}</span></td><td>${esc(h.reason)}</td><td>${esc(h.summary)}</td><td>${esc(h.contact || "не оставил")}</td><td>${h.status === "answered" ? '<span class="tag">отвечено</span>' : '<span class="tag h">ждет ответа</span>'}</td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">Передач менеджеру пока нет.</td></tr>';
  $("rows").innerHTML = ss.sessions.map((s) => `<tr data-id="${esc(s.id)}"><td>${dt(s.created_at)}</td><td>${esc((s.first || "").slice(0, 140))}</td>
    <td>${s.status === "handoff" ? '<span class="tag h">менеджеру</span>' : '<span class="tag">агент</span>'}${s.quotes ? ' <span class="tag">КП</span>' : ""}</td>
    <td class="num">${s.turns}</td><td class="num">${(s.prompt_tokens + s.completion_tokens).toLocaleString("ru-RU")}</td><td class="num">${rub(s.cost_rub)}</td></tr>`).join("")
    || '<tr><td colspan="6" class="empty">Диалогов пока нет. Напишите в чат покупателя.</td></tr>';
  document.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => open(tr.dataset.id)));
}

async function open(id) {
  current = id;
  document.querySelectorAll("tr[data-id]").forEach((tr) => tr.classList.toggle("on", tr.dataset.id === id));
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
          html += `<div class="st"><b>Модель</b> ${esc(s.name)} · ${s.prompt_tokens}+${s.completion_tokens} токенов · ${s.seconds.toFixed(1).replace(".", ",")} с${tc ? " → вызывает " + esc(tc.name) : " → отвечает клиенту"}</div>`;
        } else {
          html += `<div class="st ${esc(s.kind)}"><b>${esc(s.name)}</b> ${esc(JSON.stringify(s.input))}<pre>${esc(JSON.stringify(s.output).slice(0, 1500))}</pre></div>`;
        }
      }
    } else {
      html += `<div class="who">${m.meta.from === "manager" ? "Менеджер" : "Агент"}</div><div class="msg bot">${esc(m.content)}</div>`;
      for (const c of m.meta.cards || []) html += `<div class="st">Карточка: ${esc(c.title)}${c.price ? ", " + rub(c.price) : ""}</div>`;
    }
  }
  for (const q of d.quotes) html += `<div class="st tool"><b>КП ${esc(q.number)}</b> ${esc(q.customer)}, итого ${rub(q.total)} · <a href="/quotes/${esc(q.number)}.pdf" target="_blank" rel="noopener">открыть PDF</a></div>`;
  for (const h of d.handoffs.filter((x) => x.status !== "answered")) html += `<div class="ans" data-h="${h.id}"><div class="who">Ответ менеджера на обращение № ${h.id}</div>
    <textarea placeholder="Ответ увидит клиент в чате"></textarea>
    <label><input type="checkbox"> Ответ общий, без данных этого клиента: агент будет отвечать так на похожие вопросы</label>
    <div><button type="button">Отправить клиенту</button></div></div>`;
  $("detail").innerHTML = html;
  $("detail").querySelectorAll(".ans").forEach((box) => (box.querySelector("button").onclick = async () => {
    const answer = box.querySelector("textarea").value.trim();
    if (!answer) return;
    await post(`/api/admin/handoff/${box.dataset.h}/answer`, { answer, teach: box.querySelector("input").checked });
    await load(); open(id);
  }));
}
$("upd").onclick = async () => { await post("/api/admin/catalog/update"); load(); };
load(); setInterval(() => { if (!document.querySelector(".ans textarea:focus") && !document.querySelector(".ans textarea")?.value) load(); }, 15000);
