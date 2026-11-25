"""判断策略（P0：规则基线；advise 模式只建议不执行）"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import bot.config as cfg
from bot.intelligence.labels import UNKNOWN, label_of
from bot.intelligence.rules import match as match_rule


@dataclass
class Decision:
    action: str
    label: str
    confidence: float
    source: str                 # rule / none
    reason: str
    opinion: Optional[dict] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "label": self.label,
            "confidence": round(float(self.confidence), 3),
            "source": self.source,
            "reason": self.reason,
        }


def mode() -> str:
    """当前智能模式；未知值一律按 off 处理"""
    m = (cfg.INTELLIGENCE_MODE or "off").strip().lower()
    return m if m in ("off", "advise", "confirm", "auto") else "off"


def enabled() -> bool:
    return mode() != "off"


def build_text(opinion: Optional[dict]) -> str:
    if not opinion:
        return ""
    return " ".join(str(opinion.get(k) or "") for k in
                    ("opinion", "opinion_note", "return_eval")).strip()


def decide(opinion: Optional[dict]) -> Decision:
    """规则优先；未命中返回 unknown（P0 不接模型）"""
    text = build_text(opinion)
    hit = match_rule(text)
    if hit:
        action, conf, kw = hit
        return Decision(action=action, label=label_of(action),
                        confidence=conf, source="rule",
                        reason=f"命中关键词「{kw}」", opinion=opinion)
    return Decision(action=UNKNOWN, label=label_of(UNKNOWN),
                    confidence=0.0, source="none",
                    reason="规则未命中", opinion=opinion)
