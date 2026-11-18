"""
录制流程 vs 机器人 step 的 canonical diff 引擎

diff 维度:
  1. action 匹配
  2. selector / data-testid 匹配（canonical 后归一化）
  3. 顺序匹配
  4. 缺失步骤 / 多余步骤 / 顺序错位 / 预期值不符

输出:
  {
    "status": "match" | "partial" | "mismatch",
    "score": 0~100,
    "diff_items": [
      {"type": "missing" | "extra" | "mismatch" | "match",
       "expected_step": {...},
       "recorded_step": {...},
       "note": "..."}
    ],
    "summary": {match: N, missing: N, extra: N, mismatch: N}
  }
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple


# ================================================================
# canonical 归一化
# ================================================================
def canonicalize_selector(selector: str) -> str:
    """归一化 selector，去掉动态部分"""
    if not selector:
        return ""
    # data-testid 保留
    if "[data-testid=" in selector:
        return selector
    # id 保留
    if selector.startswith("#"):
        return selector
    # 报案号 URL 参数去掉
    s = re.sub(r"RD[A-Za-z]{2}[0-9A-Za-z]{16,20}", "<CLAIM>", selector)
    s = re.sub(r"\?.*$", "", s)
    s = re.sub(r":nth-of-type\(\d+\)", ":nth-of-type(N)", s)
    return s.strip()


def canonicalize_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """把录制 step 或预期 step 都 canonicalize 成统一格式"""
    return {
        "action": step.get("action", "").lower(),
        "tag": step.get("tag", "").lower(),
        "selector": canonicalize_selector(step.get("selector", "")),
        "data_testid": step.get("data_testid", "") or "",
        "text": (step.get("text", "") or "").strip()[:40],
        # 匹配 key: 优先 data-testid，然后 selector，然后 text
        "key": (
            step.get("data_testid")
            or step.get("selector")
            or (step.get("text", "") or "")[:20]
        ),
    }


def steps_match(expected: Dict[str, Any], recorded: Dict[str, Any]) -> bool:
    """判断两个 canonical step 是否匹配"""
    e = canonicalize_step(expected)
    r = canonicalize_step(recorded)

    # action 必须一致
    if e["action"] != r["action"]:
        return False

    # key 三级匹配：data-testid → selector → text
    if e["data_testid"] and r["data_testid"]:
        if e["data_testid"] == r["data_testid"]:
            return True

    if e["selector"] and r["selector"]:
        if e["selector"] == r["selector"]:
            return True
        # 互相包含也 OK（宽松匹配）
        if e["selector"] in r["selector"] or r["selector"] in e["selector"]:
            return True

    if e["text"] and r["text"]:
        if e["text"] == r["text"] or e["text"] in r["text"] or r["text"] in e["text"]:
            return True

    return False


# ================================================================
# 主 diff 算法（LCS 最长公共子序列 + backtrack）
# ================================================================
def diff_workflows(
    expected: List[Dict[str, Any]],
    recorded: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    计算 expected vs recorded 的 diff

    返回:
      {
        "status": "match" | "partial" | "mismatch",
        "score": 0~100,
        "diff_items": [...],
        "summary": {...}
      }
    """
    E = [canonicalize_step(s) for s in expected]
    R = [canonicalize_step(s) for s in recorded]
    n, m = len(E), len(R)

    # LCS DP 表
    dp: List[List[int]] = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if steps_match(E[i - 1], R[j - 1]):
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # backtrack 生成 diff
    diff_items: List[Dict[str, Any]] = []
    i, j = n, m
    match_count = 0
    missing_count = 0
    extra_count = 0

    while i > 0 or j > 0:
        if i > 0 and j > 0 and steps_match(E[i - 1], R[j - 1]):
            diff_items.insert(0, {
                "type": "match",
                "expected": expected[i - 1],
                "recorded": recorded[j - 1],
            })
            match_count += 1
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            # recorded 多了一步
            diff_items.insert(0, {
                "type": "extra",
                "expected": None,
                "recorded": recorded[j - 1],
                "note": "录制多余（机器人不会做这步）",
            })
            extra_count += 1
            j -= 1
        else:
            # expected 少了一步
            diff_items.insert(0, {
                "type": "missing",
                "expected": expected[i - 1],
                "recorded": None,
                "note": "录制缺失（机器人会做但你没做）",
            })
            missing_count += 1
            i -= 1

    # 计算 score
    total_expected = len(expected)
    if total_expected == 0:
        score = 0
    else:
        score = round(match_count / max(total_expected, len(recorded)) * 100)

    if score >= 90 and missing_count == 0:
        status = "match"
    elif score >= 60:
        status = "partial"
    else:
        status = "mismatch"

    return {
        "status": status,
        "score": score,
        "diff_items": diff_items,
        "summary": {
            "match": match_count,
            "missing": missing_count,
            "extra": extra_count,
            "total_expected": len(expected),
            "total_recorded": len(recorded),
        },
    }


# ================================================================
# 漂亮打印
# ================================================================
def format_diff_report(diff: Dict[str, Any]) -> str:
    """生成人类可读的 diff 报告"""
    lines = []
    s = diff["summary"]
    emoji = {"match": "✅", "partial": "⚠️", "mismatch": "❌"}[diff["status"]]
    lines.append(f"{emoji} 比对结果: {diff['status'].upper()}  Score={diff['score']}/100")
    lines.append(f"  预期 {s['total_expected']} 步 / 录制 {s['total_recorded']} 步")
    lines.append(f"  ✅ 匹配 {s['match']}  ⚠️ 缺失 {s['missing']}  ➕ 多余 {s['extra']}")
    lines.append("")
    lines.append("-" * 72)

    for item in diff["diff_items"]:
        t = item["type"]
        if t == "match":
            e = item["expected"]
            r = item["recorded"]
            lines.append(f"  ✅  [{e.get('action',''):8s}] {e.get('selector') or e.get('data_testid') or e.get('text','')}")
        elif t == "missing":
            e = item["expected"]
            lines.append(f"  ❌ 缺失 [{e.get('action',''):8s}] {e.get('selector') or e.get('data_testid') or e.get('text','')}")
        elif t == "extra":
            r = item["recorded"]
            lines.append(f"  ➕ 多余 [{r.get('action',''):8s}] {r.get('selector') or r.get('data_testid') or r.get('text','')}")

    lines.append("-" * 72)
    return "\n".join(lines)
