// Показ проверки цен: сервер проигрывает два хода с подставной моделью и возвращает, что было на каждом шаге.
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const rub = (v) => Number(v).toLocaleString("ru-RU", { minimumFractionDigits: Number.isInteger(Number(v)) ? 0 : 2, maximumFractionDigits: 2 }) + " ₽";
const out = document.getElementById("out"), go = document.getElementById("go");
const step = (label, text, cls = "") => `<div class="step"><span>${label}</span><div class="box ${cls}">${text}</div></div>`;

go.onclick = async () => {
  go.disabled = true;
  try {
    const r = await (await fetch("/api/proof", { method: "POST" })).json();
    out.innerHTML = r.runs.map((x, i) => `<section class="run"><h2>${i + 1}. ${esc(x.title)}</h2>
      ${step("Клиент спросил", esc(r.question))}
      ${step("Модель написала", esc(x.model_said))}
      ${step("Проверка кодом", `<b>Остановлено.</b> Суммы ${x.caught.map(esc).join(", ")} нет ни в каталоге, ни в словах клиента. Модель получила замечание и вторую попытку.`, "bad")}
      ${step("Вторая попытка модели", esc(x.model_second))}
      ${step("Клиент увидел", esc(x.client_saw) + (i ? "<br><b>Сумму вырезал код:</b> модель повторила выдуманную цену, третьей попытки нет." : ""), "ok")}
    </section>`).join("<br>");
    document.getElementById("facts").textContent = `Вариант из каталога: ${r.offer}. Цена в каталоге ${rub(r.real_price)}, модель назвала ${rub(r.fake_price)}. Диалоги этого показа в статистику стенда не попадают.`;
  } catch (e) {
    out.innerHTML = step("Сбой", "Показ не запустился, обновите страницу.", "bad");
  }
  go.disabled = false; go.textContent = "Показать еще раз";
};
