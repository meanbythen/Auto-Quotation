"""智能意见判断模块

P0 范围：读取报价处理意见 → 规则判断 → 采集语料 + 写判断记录 → 只建议不执行。
后续阶段（confirm/auto、模型推理）在同一入口扩展，调用方不需要改。
"""
from __future__ import annotations

from bot.intelligence.collector import (
    corpus_count,
    export_jsonl,
    record_opinion,
    record_prediction,
)
from bot.intelligence.labels import HOLD_ACTIONS, LABELS, UNKNOWN, label_of
from bot.intelligence.policy import Decision, decide, enabled, mode
from bot.intelligence.reader import format_opinion, read_latest_opinion

__all__ = [
    "Decision", "LABELS", "UNKNOWN", "HOLD_ACTIONS", "label_of",
    "decide", "enabled", "mode", "read_latest_opinion", "format_opinion",
    "record_opinion", "record_prediction", "corpus_count", "export_jsonl",
]
