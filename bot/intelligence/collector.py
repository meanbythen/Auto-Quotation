"""意见语料采集与导出

P0 只做「挂机随单采集」：读取到的每一条最新意见都落 SQLite，
按「报案号 + 发表时间 + 意见 + 工号」去重，重复采集不会产生脏数据。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from bot.logging_system import app_data_dir, log_error


def _store():
    from bot.pricing import get_price_service
    return get_price_service().store


def _dedup_hash(claim_no: str, opinion: dict) -> str:
    raw = "|".join([
        str(claim_no or "").strip().upper(),
        str(opinion.get("publish_time") or "").strip(),
        str(opinion.get("opinion") or "").strip(),
        str(opinion.get("staff_code") or "").strip(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def record_opinion(claim_no: str, opinion: dict, source: str = "hang",
                   submit_path: str = "", prev_action: str = "") -> int:
    """写入一条意见语料，返回语料 id；失败返回 0（不抛异常）"""
    try:
        store = _store()
        record = {
            "claim_no": claim_no,
            "staff_code": opinion.get("staff_code", ""),
            "org_name": opinion.get("org_name", ""),
            "publish_time": opinion.get("publish_time", ""),
            "opinion": opinion.get("opinion", ""),
            "opinion_note": opinion.get("opinion_note", ""),
            "return_eval": opinion.get("return_eval", ""),
            "submit_path": submit_path,
            "prev_action": prev_action,
            "source": source,
            "raw_json": json.dumps(opinion, ensure_ascii=False),
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "dedup_hash": _dedup_hash(claim_no, opinion),
        }
        return store.add_opinion_corpus(record)
    except Exception as exc:
        log_error("写入意见语料失败", exc)
        return 0


def record_prediction(claim_no: str, decision, corpus_id: int = 0,
                      decided_action: str = "", executed: bool = False,
                      overridden_by: str = "") -> bool:
    try:
        return _store().add_opinion_prediction({
            "claim_no": claim_no,
            "corpus_id": corpus_id,
            "action": getattr(decision, "action", "unknown"),
            "confidence": getattr(decision, "confidence", 0.0),
            "model_version": "rule-v1",
            "decided_action": decided_action,
            "overridden_by": overridden_by,
            "executed": 1 if executed else 0,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as exc:
        log_error("写入意见判断记录失败", exc)
        return False


def corpus_count() -> int:
    try:
        return _store().count_opinion_corpus()
    except Exception:
        return 0


def export_jsonl(out_path: Optional[Path] = None) -> Optional[Path]:
    """导出全部语料为 JSONL（训练用）；返回文件路径"""
    try:
        rows = _store().iter_opinion_corpus()
    except Exception as exc:
        log_error("读取意见语料失败", exc)
        return None
    if out_path is None:
        folder = app_data_dir() / "intelligence" / "corpus"
        folder.mkdir(parents=True, exist_ok=True)
        out_path = folder / f"opinions_{datetime.now():%Y%m}.jsonl"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out_path


if __name__ == "__main__":
    p = export_jsonl()
    print(f"导出 {corpus_count()} 条语料 → {p}")
