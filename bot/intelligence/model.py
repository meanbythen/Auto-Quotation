"""离线训练模型的运行时推理（纯 Python，无 sklearn 依赖）

模型文件：data/intelligence/model/model.json
结构：
{
  "version": "nb-20261125-1200",
  "samples": 120,
  "classes": ["quote_done", ...],
  "prior": {"quote_done": -1.23, ...},
  "logprob": {"quote_done": {"不": -3.1, "不同": -4.2, ...}, ...}
}
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from bot.logging_system import app_data_dir
from bot.intelligence.rules import normalize

MODEL_VERSION = "nb-v1"

_cache: dict = {"path": "", "mtime": 0.0, "model": None}


def model_path() -> Path:
    return app_data_dir() / "intelligence" / "model" / "model.json"


def tokenize(text: str) -> list[str]:
    """字符 1~2 gram，中文短句足够；空文本返回空列表"""
    t = normalize(text)
    grams: list[str] = []
    for i, ch in enumerate(t):
        grams.append(ch)
        if i + 1 < len(t):
            grams.append(t[i:i + 2])
    return grams


class NBModel:
    def __init__(self, payload: dict):
        self.version = str(payload.get("version") or MODEL_VERSION)
        self.samples = int(payload.get("samples") or 0)
        self.classes: list[str] = [str(c) for c in (payload.get("classes") or [])]
        self.prior: dict[str, float] = {k: float(v) for k, v in (payload.get("prior") or {}).items()}
        self.logprob: dict[str, dict[str, float]] = {
            str(c): {str(g): float(lp) for g, lp in (grams or {}).items()}
            for c, grams in (payload.get("logprob") or {}).items()
        }

    def predict(self, text: str) -> Optional[tuple[str, float]]:
        tokens = tokenize(text)
        if not tokens or not self.classes:
            return None
        scores: dict[str, float] = {}
        for action in self.classes:
            grams = self.logprob.get(action) or {}
            total = self.prior.get(action, 0.0)
            for tok in tokens:
                total += grams.get(tok, 0.0)
            scores[action] = total / len(tokens)
        if not scores:
            return None
        best = max(scores.values())
        exp = {a: math.exp(s - best) for a, s in scores.items()}
        z = sum(exp.values()) or 1.0
        probs = {a: v / z for a, v in exp.items()}
        action = max(probs, key=probs.get)
        return action, probs[action]


def load(path: Optional[Path] = None) -> Optional[NBModel]:
    p = Path(path) if path else model_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    if _cache["model"] is not None and _cache["path"] == str(p) and _cache["mtime"] == mtime:
        return _cache["model"]
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        model = NBModel(payload)
    except Exception:
        return None
    _cache.update({"path": str(p), "mtime": mtime, "model": model})
    return model


def clear_cache() -> None:
    _cache.update({"path": "", "mtime": 0.0, "model": None})
