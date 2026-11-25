"""离线训练意见分类模型（纯 Python，无 sklearn 依赖）

用法：
    python -m bot.intelligence.train
    python -m bot.intelligence.train --min-samples 30 --out D:/tmp/model.json

数据来源：opinion_label 中的人工标签（P1 确认框积累）。
样本不足或类别少于 2 时不生成模型，运行时自动回退到规则。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from bot.logging_system import app_data_dir
from bot.intelligence.model import model_path, tokenize


def _fit(rows: list[dict]) -> dict:
    """多项式朴素贝叶斯；返回可直接落盘的 payload"""
    class_counts: dict[str, Counter] = defaultdict(Counter)
    class_total: Counter = Counter()
    class_docs: Counter = Counter()
    vocab: set[str] = set()
    for row in rows:
        action = str(row.get("action") or "").strip()
        text = " ".join(str(row.get(k) or "") for k in
                        ("opinion", "opinion_note", "return_eval")).strip()
        if not action or action == "unknown" or not text:
            continue
        tokens = tokenize(text)
        if not tokens:
            continue
        class_counts[action].update(tokens)
        class_total[action] += len(tokens)
        class_docs[action] += 1
        vocab.update(tokens)

    total_samples = sum(class_docs.values())
    if not class_counts:
        raise ValueError("没有可用样本")

    v = max(1, len(vocab))
    import math
    prior = {c: math.log(class_total[c] / total_samples) for c in class_counts}
    logprob: dict[str, dict[str, float]] = {}
    for c, counter in class_counts.items():
        denom = class_total[c] + v
        logprob[c] = {
            gram: math.log((counter.get(gram, 0) + 1) / denom) for gram in vocab
        }

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return {
        "version": f"nb-{stamp}",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "samples": total_samples,
        "classes": sorted(class_counts),
        "prior": prior,
        "logprob": logprob,
    }


def _accuracy(payload: dict, rows: list[dict]) -> Optional[float]:
    from bot.intelligence.model import NBModel
    model = NBModel(payload)
    ok = total = 0
    for row in rows:
        action = str(row.get("action") or "").strip()
        text = " ".join(str(row.get(k) or "") for k in
                        ("opinion", "opinion_note", "return_eval")).strip()
        if action not in model.classes or not text:
            continue
        hit = model.predict(text)
        total += 1
        if hit and hit[0] == action:
            ok += 1
    return round(ok / total, 4) if total else None


def train(min_samples: int = 20, out_path: Optional[Path] = None) -> Path:
    from bot.pricing import get_price_service
    rows = get_price_service().store.iter_labeled_corpus()
    usable = [r for r in rows if str(r.get("action") or "") != "unknown"]
    if len(usable) < max(2, min_samples):
        raise ValueError(f"已标注样本 {len(usable)} 条，少于阈值 {min_samples}，暂不训练")
    classes = {str(r.get("action")) for r in usable}
    if len(classes) < 2:
        raise ValueError(f"只有 {len(classes)} 个动作类别，无法训练分类模型")

    payload = _fit(usable)
    # 样本足够时做一次简单留出评估（每 5 条留 1 条）
    if len(usable) >= 60:
        test = usable[::5]
        train_rows = [r for i, r in enumerate(usable) if i % 5 != 0]
        try:
            payload["holdout_accuracy"] = _accuracy(_fit(train_rows), test)
        except Exception:
            pass
        payload["holdout_size"] = len(test)

    out = Path(out_path) if out_path else model_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="训练报价意见分类模型")
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()
    try:
        out = train(args.min_samples, Path(args.out) if args.out else None)
    except ValueError as exc:
        print(f"未训练：{exc}")
        return 1
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    acc = payload.get("holdout_accuracy")
    print(f"模型已生成 → {out}")
    print(f"版本 {payload['version']} | 样本 {payload['samples']} | 类别 {len(payload['classes'])}")
    if acc is not None:
        print(f"留出准确率 {acc * 100:.1f}%（{payload.get('holdout_size')} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
