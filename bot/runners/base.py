"""
运行器公共部分：上下文、台账、跳过记录、运行记录落盘与自动对比
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bot.core.records import Recorder, auto_compare
from bot.logging_system import log_error, log_run


# ============================================================
# 跳过记录：当前流程处理不了的案件 → 「报案号 + 卡在哪一步 + 原因」
# ============================================================
def skip_log_path() -> Path:
    from bot.logging_system import app_data_dir
    d = app_data_dir() / "skipped"
    d.mkdir(parents=True, exist_ok=True)
    return d / "跳过案件.jsonl"


def record_skip(claim_no: str, step: str, reason: str) -> None:
    """追加一行 JSONL，供后续补流程分支时逐个复盘"""
    item = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "claim_no": claim_no,
        "step": step or "未知步骤",
        "reason": reason,
    }
    try:
        with open(skip_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        log_error("写入跳过案件记录失败", e)
    log_run(f"⏭ 跳过 {claim_no} | 卡在 {item['step']} | {reason}")


# ============================================================
# 上下文
# ============================================================
def make_ctx(claim_no: str, tolerance: float, transfer_target: str,
             mode: str = "", portal_url: str = "", skill_learning: bool = False,
             skip_market: bool = False, market_price_threshold: float = 0.0,
             market_rate_limit: bool = True, market_breaker: bool = True) -> dict:
    return {
        "claim_no": claim_no,
        "tolerance": tolerance,
        "transfer_target": transfer_target,
        "mode": mode,
        "portal_url": portal_url,
        "skill_learning": bool(skill_learning),
        "skip_market": bool(skip_market),
        "market_price_threshold": float(market_price_threshold or 0.0),
        "market_rate_limit": bool(market_rate_limit),
        "market_breaker": bool(market_breaker),
        # 本单是否真的访问过一车一件（决定案件结束后要不要冷却）
        "market_touched": False,
    }


# ============================================================
# 台账（Excel）
# ============================================================
def write_ledger(claim_no: str, vehicle, parts, status: str, note: str = "",
                 skip_step: str = "") -> None:
    try:
        from bot.ledger import record as _ledger_record
        path = _ledger_record(claim_no=claim_no, vehicle=vehicle,
                              parts=parts, status=status, note=note,
                              skip_step=skip_step)
        if path:
            print(f"\n📊 工作记录 → {path}")
    except Exception:
        pass


# ============================================================
# 运行记录落盘 + 自动对比（同一案件的另一套环境）
# ============================================================
def finalize_record(rec: Recorder, *, status: str, route: str = "",
                    note: str = "", data: dict | None = None,
                    ignore_env: bool = False) -> Path:
    rec.finish(status=status, route=route, note=note, data=data)
    try:
        path = rec.record.save()
        print(f"\n🧾 运行记录 → {path}")
    except Exception as e:
        log_error("保存运行记录失败", e)
        path = None

    try:
        cmp_path = auto_compare(rec.record, ignore_env=ignore_env)
        if cmp_path:
            print(f"🔍 已生成对比报告（另一套环境的同一案件）→ {cmp_path}")
            log_run(f"对比报告: {cmp_path}")
    except Exception as e:
        log_error("生成对比报告失败", e)
    return path
