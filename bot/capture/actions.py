"""
人工操作录制 —— 在（生产）浏览器里记录人工的点击 / 输入 / 跳转，输出流程改进草案

复用：
  · bot/recorder.py 的 Recorder（JS 注入 + 拉取 buffer），不重复写注入脚本
  · bot/flows/claim_flow.py 的 STEP_NAMES（把动作映射回标准步骤，指出流程可改进处）

产出（data/capture/<ts>-<name>-actions/）：
  actions.jsonl   每个动作一行（含时间 / URL / 选择器 / 文本 / 值）
  actions.md      可读报告：时间线 + 标准步骤覆盖 + 改进建议
同时兼容老格式：data/workflows/<name>.json（「流程比对」页可直接选）
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from bot.flows.claim_flow import STEP_NAMES
from bot.logging_system import log_run
from bot.recorder import Recorder


def workflows_dir() -> Path:
    from bot.logging_system import app_data_dir
    d = app_data_dir() / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# 动作 → 标准步骤 映射（越具体越靠前）
# ============================================================
STEP_PATTERNS: list[tuple[str, str]] = [
    # STEP 9：报价完成 / 提交报价 / 提交任务 / 报价意见
    (r"quote-done|报价完成|quote_finalize", "STEP 9"),
    (r"quote-opinion|报价意见|opinion", "STEP 9"),
    (r"submit-quote|提交报价|quote-submit", "STEP 9"),
    (r"confirm-quote|提交任务|quote_submit_confirm", "STEP 9"),
    # 超权限补救
    (r"submit-superior|提交上级|superior", "STEP 9a"),
    (r"transfer|移交|taskTransfer", "STEP 9b"),
    # 提交定损
    (r"submit-loss|提交定损|diagnosis-modal", "STEP 8"),
    # 修复超价 / 暂存
    (r"save-draft|暂存|修复|over-market", "STEP 7"),
    # 零件比价
    (r"part-input|part-search|配件|OE", "STEP 6"),
    # VIN
    (r"vin", "STEP 5"),
    # 零件表 / 全选
    (r"parts|零件|全选|select-all", "STEP 4"),
    # 同步定损
    (r"sync|同步", "STEP 3"),
    # 编辑打开定损平台
    (r"open-loss-edit|编辑|loss-edit", "STEP 2"),
    # 首页报案号
    (r"claim|报案号", "STEP 1"),
]


# 系统事件（非人工操作，不参与“未映射动作”统计，单独在报告里列「系统响应」）
SYSTEM_ACTIONS = {"response", "navigate", "open", "close", "dialog", "submit"}


def map_step(step) -> tuple[str, str]:
    """返回 (STEP id, 置信度)。未命中返回 ("", "")"""
    blob = " ".join([
        getattr(step, "data_testid", "") or "",
        getattr(step, "selector", "") or "",
        getattr(step, "text", "") or "",
        getattr(step, "value", "") or "",
    ]).lower()
    for pat, sid in STEP_PATTERNS:
        if re.search(pat, blob, re.IGNORECASE):
            strong = bool(getattr(step, "data_testid", "")) or bool(getattr(step, "selector", ""))
            return sid, ("high" if strong else "medium")
    return "", ""


# ============================================================
# 录制主流程
# ============================================================
async def record_actions(context, name: str, *, duration_s: int = 600,
                         poll_s: float = 1.0, stop_event=None,
                         on_step=None, meta: dict | None = None) -> dict:
    """
    在 context 里注入录制脚本并持续收集人工动作

    stop_event: threading.Event（GUI『停止』按钮）；None 时按 duration_s 结束
    on_step:   每收到新动作回调一次（GUI 推送到预览框）
    """
    from bot.runtime import BotStopped, check_stop

    rec = Recorder(save_dir=workflows_dir())
    await rec.attach(context)

    steps = []
    started = time.time()
    deadline = started + max(1, int(duration_s))
    print(f"\n  ⏺ 录制已开始 —— 请在浏览器里正常操作一遍（最长 {duration_s}s，可随时停止）")
    last_report = 0.0
    warned = False

    try:
        while True:
            new = await rec.collect()
            for s in new:
                steps.append(s)
                sid, conf = map_step(s)
                line = f"[{len(steps):02d}] {s.action:8s} {s.selector[:52]}  {(s.text or s.value)[:24]}"
                if sid:
                    line += f"  → {sid}"
                print("  " + line)
                if on_step:
                    try:
                        on_step(s)
                    except Exception:
                        pass

            # 每 6 秒打印一次诊断：注入生效了吗、动作有多少
            now_s = time.time()
            if now_s - last_report >= 6:
                last_report = now_s
                try:
                    st = await rec.status()
                    print(f"  ⏺ 录制中 · 窗口 {st['pages']} / frame {st['frames']} · "
                          f"已注入 {st['installed']} · 累计动作 {len(steps)}")
                    if not warned and st["installed"] == 0 and now_s - started > 15:
                        warned = True
                        print("  ⚠️ 还没有任何页面被注入：请确认你操作的是『程序打开或接管』的那个浏览器窗口")
                        print("     如果你在自己的浏览器里操作，程序收不到动作 —— 可先用调试端口启动浏览器让程序接管")
                except Exception:
                    pass
                if not warned and len(steps) == 0 and now_s - started > 30:
                    warned = True
                    print("  ⚠️ 30 秒内没有捕捉到任何动作：请确认在程序打开的浏览器窗口里点击/输入")

            if time.time() >= deadline:
                break
            if stop_event is not None and stop_event.is_set():
                break
            try:
                check_stop()
            except BotStopped:
                break
            await asyncio.sleep(poll_s)
    finally:
        # 收尾再拉一次，避免最后几个动作丢失
        try:
            steps.extend(await rec.collect())
        except Exception:
            pass
        rec.stop()

    duration = round(time.time() - started, 1)
    workflow = await rec.save(steps, name, meta=meta)

    folder = _new_actions_dir(name)
    raw = [s.to_dict() for s in steps]
    jsonl = folder / "actions.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for item in raw:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    md = folder / "actions.md"
    md.write_text(format_actions_markdown(name, raw, meta=meta, duration=duration),
                  encoding="utf-8")

    log_run(f"人工操作录制完成：{len(steps)} 个动作 / {duration}s → {md}")
    print(f"\n  ✅ 录制完成，共 {len(steps)} 个动作，用时 {duration}s")
    print(f"  📄 改进草案: {md}")
    print(f"  📁 兼容流程比对: {workflow}")
    return {
        "steps": raw,
        "workflow": str(workflow),
        "jsonl": str(jsonl),
        "markdown": str(md),
        "dir": str(folder),
        "duration_s": duration,
    }


def _new_actions_dir(name: str) -> Path:
    from bot.capture.snapshot import capture_dir, _safe, _ts
    d = capture_dir() / f"{_ts()}-{_safe(name)}-actions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# Markdown 草案
# ============================================================
def format_actions_markdown(name: str, raw_steps: list[dict], *,
                            meta: dict | None = None, duration: float = 0) -> str:
    mapped = []
    unmapped = []
    for i, s in enumerate(raw_steps, 1):
        if s.get("action") in SYSTEM_ACTIONS:
            s["_step"] = ""
            s["_conf"] = ""
            continue
        sid, conf = map_step(_DictStep(s))
        s["_step"] = sid
        s["_conf"] = conf
        (mapped if sid else unmapped).append(s)

    lines = [f"# 人工操作录制 · {name}", "",
             f"- 录制时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 动作数: {len(raw_steps)} · 时长: {duration}s"]
    if meta:
        for k, v in meta.items():
            lines.append(f"- {k}: {v}")
    lines += ["", "## 时间线", "",
              "| # | 时间 | 动作 | 元素 | 文本 | 值 | 标准步骤 |",
              "|---|---|---|---|---|---|---|"]
    for i, s in enumerate(raw_steps, 1):
        ts = (s.get("ts") or "")[11:19]
        elem = s.get("selector") or s.get("tag") or ""
        lines.append(
            f"| {i} | {ts} | {s.get('action','')} | `{elem[:44]}` | "
            f"{(s.get('text') or '')[:24]} | {(s.get('value') or '')[:20]} | "
            f"{s.get('_step','') or '—'} |")
    lines.append("")

    # 系统响应（XHR / 页面加载 / 跳转 / 弹窗）—— 便于看清每步动作后系统回了什么
    sys_events = [s for s in raw_steps
                  if s.get("action") in ("response", "navigate", "open", "close", "dialog", "submit")]
    if sys_events:
        lines += ["## 系统响应（接口调用 / 页面跳转 / 弹窗）", "",
                  "| 时间 | 类型 | 方法/内容 | 地址或值 |", "|---|---|---|---|"]
        for s in sys_events[:60]:
            ts = (s.get("ts") or "")[11:19]
            detail = (s.get("text") or "")[:18]
            target = s.get("url") or s.get("value") or ""
            lines.append(f"| {ts} | {s.get('action')} | {detail} | `{target[:88]}` |")
        if len(sys_events) > 60:
            lines.append(f"| … | 共 {len(sys_events)} 条，仅显示前 60 | | |")
        lines.append("")

    # 标准步骤覆盖
    lines += ["## 流程覆盖（对照 bot/flows/claim_flow.STEP_NAMES）", "",
              "| STEP | 名称 | 本次是否出现 |", "|---|---|---|"]
    hit = {}
    for s in mapped:
        hit.setdefault(s["_step"], []).append(s)
    for sid, sname in STEP_NAMES.items():
        mark = f"✅ {len(hit[sid])} 次" if sid in hit else "○ 未出现"
        lines.append(f"| {sid} | {sname} | {mark} |")
    lines.append("")

    # 改进建议
    lines += ["## 改进建议", ""]
    suggestions = []

    if unmapped:
        suggestions.append(
            f"**未映射动作 {len(unmapped)} 个**：可能是新增业务动作或页面版式变化，"
            f"建议核对流程逻辑（claim_flow / page_objects）是否需要补步骤")
        for s in unmapped[:12]:
            suggestions.append(
                f"- `{s.get('action','')}` `{(s.get('selector') or '')[:60]}` "
                f"文本={ (s.get('text') or '')[:30] } 值={ (s.get('value') or '')[:20] }")

    dups = _find_duplicates(raw_steps)
    if dups:
        suggestions.append(f"**重复动作 {len(dups)} 处**：可能人工多点了一次，"
                           f"自动流程可加去重/幂等判断")
        for d in dups[:10]:
            suggestions.append(f"- `{d['selector'][:60]}` 在 {d['times']}s 内重复 {d['count']} 次")

    missing = [sid for sid in STEP_NAMES
               if sid in ("STEP 1", "STEP 2", "STEP 3", "STEP 4", "STEP 5",
                          "STEP 6", "STEP 7", "STEP 8", "STEP 9")
               and sid not in hit]
    if missing:
        suggestions.append("**未出现的标准步骤**："
                           + "、".join(f"{m} {STEP_NAMES[m]}" for m in missing)
                           + "（本次未走到 / 人工绕过 / 流程可裁剪）")

    fills = [s for s in raw_steps if s.get("action") in ("fill", "select") and s.get("value")]
    if fills:
        suggestions.append("**人工填写/选择的值**（可作为流程默认参数参考）")
        for s in fills[:12]:
            suggestions.append(f"- `{(s.get('selector') or '')[:50]}` ← "
                               f"{(s.get('value') or '')[:40]}")

    if not suggestions:
        suggestions.append("- 本次动作与标准流程一致，暂无明显改进点")
    lines += suggestions

    return "\n".join(lines)


def _find_duplicates(steps: list[dict]) -> list[dict]:
    """连续 2.5 秒内同元素同动作重复"""
    out = []
    for i in range(1, len(steps)):
        prev, cur = steps[i - 1], steps[i]
        if prev.get("selector") and prev.get("selector") == cur.get("selector") \
                and prev.get("action") == cur.get("action"):
            t1, t2 = prev.get("ts", ""), cur.get("ts", "")
            if t1 == t2 or _same_window(t1, t2, 2.5):
                if out and out[-1]["selector"] == cur.get("selector"):
                    out[-1]["count"] += 1
                    continue
                out.append({"selector": cur.get("selector", ""),
                            "count": 2, "times": _seconds_between(t1, t2)})
    return out


def _same_window(t1: str, t2: str, seconds: float) -> bool:
    return _seconds_between(t1, t2) <= seconds


def _seconds_between(t1: str, t2: str) -> float:
    try:
        a = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        b = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return abs((b - a).total_seconds())
    except Exception:
        return 0.0


class _DictStep:
    """让 dict 形式也能复用 map_step（它只读属性）"""

    def __init__(self, d: dict):
        self.data_testid = d.get("data_testid", "")
        self.selector = d.get("selector", "")
        self.text = d.get("text", "")
        self.value = d.get("value", "")
