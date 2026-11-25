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
    """当前智能模式：GUI 设置（app_setting）优先，其次环境变量/配置"""
    m = (cfg.INTELLIGENCE_MODE or "off").strip().lower()
    try:
        from bot.pricing import get_price_service
        saved = (get_price_service().store.get_setting("intelligence_mode", "") or "").strip().lower()
        if saved in ("off", "advise", "confirm", "auto"):
            m = saved
    except Exception:
        pass
    return m if m in ("off", "advise", "confirm", "auto") else "off"


def enabled() -> bool:
    return mode() != "off"


def build_text(opinion: Optional[dict]) -> str:
    if not opinion:
        return ""
    return " ".join(str(opinion.get(k) or "") for k in
                    ("opinion", "opinion_note", "return_eval")).strip()


def decide(opinion: Optional[dict]) -> Decision:
    """规则优先；规则未命中时用离线模型补充；都没有则 unknown"""
    text = build_text(opinion)
    hit = match_rule(text)
    if hit:
        action, conf, kw = hit
        return Decision(action=action, label=label_of(action),
                        confidence=conf, source="rule",
                        reason=f"命中关键词「{kw}」", opinion=opinion)
    try:
        from bot.intelligence.model import load as load_model
        model = load_model()
    except Exception:
        model = None
    if model is not None:
        pred = model.predict(text)
        if pred:
            action, conf = pred
            return Decision(action=action, label=label_of(action),
                            confidence=conf, source="model",
                            reason=f"模型 {model.version} 预测",
                            opinion=opinion,
                            extra={"model_version": model.version})
    return Decision(action=UNKNOWN, label=label_of(UNKNOWN),
                    confidence=0.0, source="none",
                    reason="规则与模型均未命中", opinion=opinion)
