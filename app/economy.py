"""Экономика агента: сколько времени менеджеров он снимает и во что обходится модель.

Замеры стенда (сколько диалогов закрыто без менеджера, сколько стоит диалог) отделены от допущений
(минуты менеджера на обращение, стоимость его часа, польза карточки). Допущения видны на экране и меняются там же.
"""
from __future__ import annotations

from app import config


def calc(dialogs: int, handed: int, cost_total: float, minutes: float | None = None, rub_hour: float | None = None,
         card_share: float | None = None, volume: int | None = None, tokens_total: int = 0) -> dict:
    minutes = config.MANAGER_MIN_PER_REQUEST if minutes is None else minutes
    rub_hour = config.MANAGER_RUB_PER_HOUR if rub_hour is None else rub_hour
    card_share = config.CARD_SAVES_SHARE if card_share is None else card_share
    volume = config.REQUESTS_PER_HALF_YEAR if volume is None else volume
    out = {"assumptions": {"minutes_per_request": minutes, "manager_rub_per_hour": rub_hour,
                           "card_saves_share": card_share, "requests_per_half_year": volume},
           "measured": None, "stand": None, "half_year": None, "per_workday": None}
    if not dialogs:
        return out
    handed = min(max(handed, 0), dialogs)  # переданных не больше диалогов: доля без менеджера от 0 до 1
    closed = dialogs - handed
    share, per_dialog = closed / dialogs, cost_total / dialogs

    def saving(n_closed: float, n_handed: float, model_cost: float) -> dict:
        saved_min = n_closed * minutes + n_handed * minutes * card_share
        saved_rub = saved_min / 60 * rub_hour
        return {"saved_hours": round(saved_min / 60, 1), "saved_rub": round(saved_rub),
                "model_cost_rub": round(model_cost), "net_rub": round(saved_rub - model_cost),
                "return_x": round(saved_rub / model_cost, 1) if model_cost else None}

    out["measured"] = {"dialogs": dialogs, "closed": closed, "handed": handed, "closed_share": round(share, 3),
                       "cost_per_dialog_rub": round(per_dialog, 2)}
    out["stand"] = saving(closed, handed, cost_total)
    out["half_year"] = {"requests": volume, "closed": round(volume * share), "handed": volume - round(volume * share),
                        **saving(volume * share, volume * (1 - share), volume * per_dialog),
                        "model_budget_rub": config.MODEL_BUDGET_HALF_YEAR_RUB or None}
    days = config.WORKDAYS_PER_MONTH * 6  # заказчик просил расход в день: полгода при 21 рабочем дне в месяце
    out["per_workday"] = {"requests": round(volume / days, 1),
                          "tokens": round(volume * tokens_total / dialogs / days),
                          "model_cost_rub": round(volume * per_dialog / days, 2)}
    return out
