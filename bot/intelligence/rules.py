"""意见规则基线（P0 冷启动阶段的主要判断依据）

设计原则：
1. 先判否定（不同意 / 不予 / 驳回），避免「不同意」被「同意」命中；
2. 先判具体动作，最后判宽泛的「同意 / 通过」；
3. 命中即返回，置信度 0.72~0.85，人工确认阶段可覆盖。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# (动作, 关键词组)；顺序即优先级
_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("return_requote", (
        "不同意", "不予同意", "不通过", "不认可", "驳回", "拒绝",
        "退回", "重报", "重新报价", "重新核价", "价格偏高", "报价偏高",
        "价格高", "价格过高", "下调", "核减", "减少报价",
    )),
    ("branch_center", (
        "分中心", "远程操作", "远程处理", "提交远程", "转远程",
    )),
    ("submit_superior", (
        "提交上级", "报上级", "上报上级", "上级审核", "上级核",
        "权限不足", "权限不够", "超权限", "九级报价", "九级",
    )),
    ("transfer_back", (
        "平级移交", "移交回", "转回原", "原报价员", "退回原报价",
    )),
    ("wait_material", (
        "补充资料", "补充材料", "资料不全", "待查勘", "查勘中",
        "等待查勘", "待定损", "等待定损", "待补充",
    )),
    ("no_action", (
        "无需处理", "不用处理", "无须处理", "已处理", "已报价",
        "已核价", "已完成",
    )),
    ("quote_done", (
        "同意", "无异议", "按报价", "可以执行", "执行报价", "认可", "通过",
    )),
)

_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """去空白、全角转半角常见符号，便于关键词命中"""
    t = _SPACE_RE.sub("", text or "")
    return (t.replace("（", "(").replace("）", ")")
             .replace("：", ":").replace("，", ",")
             .replace("。", ".").replace("；", ";"))


def match(text: str) -> Optional[Tuple[str, float, str]]:
    """返回 (动作, 置信度, 命中关键词)；未命中返回 None"""
    t = normalize(text)
    if not t:
        return None
    for action, keywords in _RULES:
        for kw in keywords:
            if kw in t:
                # 关键词越长越具体，置信度略高
                conf = 0.72 if len(kw) <= 2 else 0.78 if len(kw) <= 4 else 0.85
                return action, conf, kw
    return None
