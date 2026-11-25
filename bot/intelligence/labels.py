"""意见动作标签 —— 唯一事实来源

所有训练标签、规则输出、GUI 文案、审计记录都从这里取。
新增标签必须同时确认「有可执行的现有流程」，否则不要加。
"""
from __future__ import annotations

UNKNOWN = "unknown"

LABELS: dict[str, str] = {
    "quote_done":      "报价完成",
    "submit_superior": "提交上级",
    "transfer_back":   "平级移交",
    "return_requote":  "退回重报",
    "branch_center":   "分中心报价",
    "wait_material":   "等待资料",
    "no_action":       "无需处理",
    UNKNOWN:           "无法判断",
}

# 需要人工介入 / 不进入后续报价流程的动作
HOLD_ACTIONS = {"wait_material", "no_action", UNKNOWN}


def label_of(action: str) -> str:
    return LABELS.get(action or UNKNOWN, LABELS[UNKNOWN])
