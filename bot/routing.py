"""
案件路由 —— 集中路由 + 运行时兜底

不做按案件类型预分类，也不预判金额：
  先走正常流程（报价完成）；提交失败且页面提示属于权限类错误时，
  才切换到「提交上级 → 平级移交回自己 → 报价完成」的补救分支。
"""
from __future__ import annotations

ROUTE_NORMAL = "normal"
ROUTE_HIGH_AMOUNT = "high_amount"

# 「权限不够 / 需报上级」类页面提示关键词
PERMISSION_KEYWORDS = (
    "权限不够",
    "权限不足",
    "报上级",
    "上报上级",
    "无权限",
    "超出权限",
    "转核价",
    "需上级审核",
    "工作流转交成功",
)


def is_permission_error(text: str) -> bool:
    """判断页面文本 / 异常信息是否属于「权限不够、需报上级」类"""
    if not text:
        return False
    return any(k in text for k in PERMISSION_KEYWORDS)
