from __future__ import annotations

GROUPS_0 = [
    "0-市交通运输行业安全应急群",
    "0-天津铁路沿线安全治理工作领导小组",
    "0-决策服务群(内部)",
]

GROUPS_1 = [
    "1-天津气象监测预警联防",
    "1-防汛联络群",
    "1-决策气象服务-市政府总值班室",
]

TIANDA_GROUP = "天大服务产品"

PRODUCT_ROUTES = {
    "预警信号": GROUPS_1 + [TIANDA_GROUP],
    "温馨提示": GROUPS_1 + [TIANDA_GROUP],
    "雨情增刊": GROUPS_1 + GROUPS_0,
}
