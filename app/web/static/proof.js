// Показ проверки цен: сервер проигрывает два хода с подставной моделью и возвращает, что было на каждом шаге.
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const rub = (v) => Number(v).toLocaleString("ru-RU", { minimumFractionDigits: Number.isInteger(Number(v)) ? 0 : 2, maximumFractionDigits: 2 }) + " ₽";
const out = document.getElementById("out"), go = document.getElementById("go");
const step = (label, text, cls = "") => `<div class="step"><span>${label}</span><div class="box ${cls}">${text}</div></div>`;

go.onclick = async () => {
  go.disabled = true;
  try {
    const res = await fetch("/api/proof", { method: "POST" });
    if (res.status === 429) throw Object.assign(new Error("Слишком много показов подряд. Подождите минуту и нажмите еще раз."), { refused: true });
    if (!res.ok) throw new Error("сервер отказал");
    const r = await res.json();
    out.innerHTML = r.runs.map((x, i) => `<section class="run"><h2>${i + 1}. ${esc(x.title)}</h2>
      ${step("Клиент спросил", esc(r.question))}
      ${step("Модель написала", esc(x.model_said))}
      ${step("Проверка кодом", `<b>Остановлено.</b> Суммы ${x.caught.map(esc).join(", ")} нет ни в каталоге, ни в словах клиента. Модель получила замечание и вторую попытку.`, "bad")}
      ${step("Вторая попытка модели", esc(x.model_second))}
      ${i ? step("Что сделал код", "<b>Сумму вырезал код:</b> модель повторила выдуманную цену, третьей попытки нет.", "bad") : ""}
      ${step("Клиент увидел", esc(x.client_saw), "ok")}
    </section>`).join("<br>");
    document.getElementById("facts").textContent = `Вариант из каталога: ${r.offer}. Цена в каталоге ${rub(r.real_price)}, модель назвала ${rub(r.fake_price)}. Диалоги этого показа в статистику стенда не попадают.`;
    go.textContent = "Показать еще раз";
  } catch (e) {
    out.innerHTML = step("Сбой", e.refused ? esc(e.message) : e instanceof TypeError ? "Нет связи с сервером. Нажмите еще раз." : "Показ не запустился. Нажмите еще раз.", "bad");
  }
  go.disabled = false;
};

// Набор атак: список из eval/attacks.jsonl, защита кода от каждой и итог последнего прогона, если он был.
(async () => {
  const list = document.getElementById("attacks-list"), total = document.getElementById("attacks-total");
  try {
    const res = await fetch("/api/proof/attacks");
    if (!res.ok) throw new Error("сервер отказал");
    const r = await res.json();
    total.textContent = r.last_run
      ? `Последний прогон набора: ${r.last_run.date} МСК${r.last_run.model ? ", модель " + r.last_run.model : ""}. Прошло ${r.last_run.passed} из ${r.last_run.total} диалогов.`
      : `Атак в наборе: ${r.attacks.length}. Прогон набора на живой модели пока не выполнен: ниже список атак и защита кода.`;
    list.innerHTML = r.attacks.map((a) => `<div class="row">
      <div class="kind"><b>${esc(a.id)}</b>${esc(a.category)}</div>
      <div><p class="ask">${esc(a.ask)}</p><p class="defence">${esc(a.defence)}</p></div>
      ${a.passed === null ? "" : `<span class="badge ${a.passed ? "ok" : "bad"}">${a.passed ? "выдержал" : "не прошел"}</span>`}
    </div>`).join("");
  } catch (e) {
    total.textContent = "Список атак не загрузился. Обновите страницу.";
  }
})();
