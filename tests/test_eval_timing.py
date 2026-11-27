"""Синтетические выдачи: часы, агент и источник исторического model_seconds подменены."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eval import run


def response(reply="", cards=()):
    return dict(session_id="s", reply=reply, cards=list(cards), choices=[],
                events=[{"type": "status", "text": "Расчёт готов: 1200 ₽"}],
                trace=[{"kind": "tool", "name": "search", "output": {"price": 1200}}],
                usage=dict(prompt_tokens=0, completion_tokens=0, llm_calls=0, tool_calls=0))


def simulate(monkeypatch, responses):
    monkeypatch.setattr(run.loop, "run_turn", Mock(side_effect=responses))
    monkeypatch.setattr(run, "time", SimpleNamespace(monotonic=Mock(side_effect=range(10, 12 + len(responses))),
                                                   time=Mock(side_effect=[100, 140])))
    db = Mock()
    db.return_value.execute.return_value.fetchone.return_value = (7,)
    monkeypatch.setattr(run.store, "db", db)
    monkeypatch.setattr(run.config, "LLM_PROVIDER", "bridge")
    return run.play({"id": "synthetic", "turns": ["Запрос"] * len(responses)})


def test_case_clock_stops_at_each_first_return(monkeypatch):
    result = simulate(monkeypatch, [response(), response("Сколько рабочих мест?"),
                                    response("Цена по запросу", [{"type": "offer", "id": 1, "price": None}]),
                                    response(cards=[{"type": "offer", "id": 2, "price": 1200}]), response("Дальше")])
    timing = result["eval_run_to_output_seconds"]
    assert (timing["first_reply"], timing["first_priced_offer"]) == (2, 4)
    assert timing["provider"] == "bridge"
    assert (result["model_seconds"], result["wall_seconds"]) == (7, 40)


@pytest.mark.parametrize("reply, cards", [
    ("Ваш бюджет — 1200 ₽?", []), ("1200 ₽", []), ("Есть 1200 лицензий", []),
    ("Вариант «Альфа» — цена по запросу", [{"type": "offer", "price": "по запросу"}]),
    ("Посмотрите продукт", [{"type": "product", "price_min": 1200}]),
])
def test_reply_is_not_necessarily_a_priced_result(monkeypatch, reply, cards):
    timing = simulate(monkeypatch, [response(reply, cards)])["eval_run_to_output_seconds"]
    assert timing["first_reply"] == 1 and timing["first_priced_offer"] is None


def test_status_and_tool_output_alone_do_not_stop_either_clock(monkeypatch):
    timing = simulate(monkeypatch, [response()])["eval_run_to_output_seconds"]
    assert timing["first_reply"] is None and timing["first_priced_offer"] is None


def test_explicit_text_offer_can_be_the_first_reply(monkeypatch):
    timing = simulate(monkeypatch, [response("Подойдёт вариант «Альфа Про» — 1 200 ₽.")])["eval_run_to_output_seconds"]
    assert timing["first_reply"] == timing["first_priced_offer"] == 1


@pytest.mark.parametrize("event", [
    {"type": "quote", "total": 1900, "url": "/quotes/kp.pdf"},
    {"type": "spec", "rows": [{"status": "точно", "name": "Альфа Про", "price": 1900}]},
])
def test_visible_priced_document_counts_without_reply_text(monkeypatch, event):
    timing = simulate(monkeypatch, [dict(response(), events=[event])])["eval_run_to_output_seconds"]
    assert timing["first_reply"] == timing["first_priced_offer"] == 1


def test_mean_is_not_median_or_missing_sample():
    rows = [{"run": {"eval_run_to_output_seconds": {"first_reply": t, "provider": "bridge"}}} for t in (1, 2, 9, None)]
    assert "4.00 / 2.00 с (3 из 4)" in "\n".join(run._timing_rows(rows))


def test_merge_preserves_missing_and_partial_coverage(monkeypatch, tmp_path):
    old = dict(id="old", session_id="s", turns=[dict(user="Запрос", tools=[], guards=[], **response("Ответ"))],
               model_seconds=7, wall_seconds=40)
    new = dict(old, id="new", eval_run_to_output_seconds=dict(first_reply=2, first_priced_offer=None, provider="bridge"))
    source = tmp_path / "inputs"
    source.mkdir()
    (source / "transcripts.jsonl").write_text("\n".join(map(json.dumps, [old, new])), encoding="utf-8")
    cases = [dict(id=r["id"], scenario="тест", note="", expect={"handoff": False}) for r in [old, new]]
    monkeypatch.setattr(run, "check", lambda *_: {"ответил_сам": True, "_плохие_цены": []})
    monkeypatch.setattr(run, "msk", lambda _: "stamp")
    out = tmp_path / "out"
    run.merge(cases, [source], out)
    merged = [json.loads(s) for s in (out / "stamp/transcripts.jsonl").read_text(encoding="utf-8").splitlines()]
    by_id = {r["id"]: r for r in merged}
    assert "eval_run_to_output_seconds" not in by_id["old"]
    assert by_id["new"]["eval_run_to_output_seconds"] == new["eval_run_to_output_seconds"]
    report = (out / "stamp/report.md").read_text(encoding="utf-8")
    assert "2.00 с (1 из 2)" in report
    assert "нет замера (0 из 2)" in report
