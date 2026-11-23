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
SYSTEM_ACTIONS = {"response", "navigate", "open", "close", "dialog", "submit",
                  "dropdown", "popup"}


def map_step(step) -> tuple[str, str]:
    """返回 (STEP id, 置信度)。未命中返回 ("", "")"""
    blob = " ".join([
        getattr(step, "data_testid", "") or "",
        getattr(step, "selector", "") or "",
        getattr(step, "text", "") or "",
        getattr(step, "value", "") or "",
        getattr(step, "label", "") or "",
        getattr(step, "select_label", "") or "",
        getattr(step, "option_text", "") or "",
        getattr(step, "dialog", "") or "",
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
                         on_step=None, meta: dict | None = None,
                         snapshot_pages: bool = True,
                         snapshot_limit: int = 20,
                         snapshot_min_interval: float = 1.5) -> dict:
    """
    在 context 里注入录制脚本并持续收集人工动作

    stop_event: threading.Event（GUI『停止』按钮）；None 时按 duration_s 结束
    on_step:   每收到新动作回调一次（GUI 推送到预览框）
    snapshot_pages: 遇到打开弹窗 / 路由跳转 / 下拉展开时，顺手存一份页面快照
                    （page.html + snapshot.json），供事后读 DOM 升级程序
    """
    from bot.runtime import BotStopped, check_stop

    rec = Recorder(save_dir=workflows_dir())
    await rec.attach(context)

    folder = _new_actions_dir(name)

    steps = []
    snapshots: list[dict] = []
    snap_state = {"count": 0, "last": 0.0, "dropdown": 0}
    started = time.time()
    deadline = started + max(1, int(duration_s))
    print(f"\n  ⏺ 录制已开始 —— 请在浏览器里正常操作一遍（最长 {duration_s}s，可随时停止）")
    print(f"  📁 本次产出目录: {folder}")
    last_report = 0.0
    warned = False

    async def maybe_snapshot(step):
        """关键节点存页面快照：弹窗/前几个跳转/前几个下拉

        跳转与下拉都单独限流，避免开头的大量 navigate/open 把总额度吃光，
        导致流程后半段的弹窗（如第二次『报价任务提交』）没有快照。
        """
        if not snapshot_pages or step.action not in ("navigate", "popup", "open", "dropdown"):
            return
        caps = {"dropdown": 3, "navigate": 3, "open": 3}
        if step.action in caps and snap_state.get(step.action, 0) >= caps[step.action]:
            return
        if snap_state["count"] >= snapshot_limit:
            return
        if time.time() - snap_state["last"] < snapshot_min_interval:
            return
        page = None
        if step.action == "open":
            # 新标签创建瞬间 url 还是 about:blank，按 open 的目标地址等一会儿再匹配，
            # 否则会把「源页面」截图当成新页面（旧版就这样丢了报价页快照）
            target = (step.value or "").split("?")[0]
            if target:
                for _ in range(10):
                    cands = [p for p in rec.pages() if target in (p.url or "")]
                    if cands:
                        page = cands[-1]
                        break
                    await asyncio.sleep(0.2)
        if page is None:
            page = rec.find_page(getattr(step, "page_url", ""))
        if page is None:
            return
        if step.action in caps:
            snap_state[step.action] = snap_state.get(step.action, 0) + 1
        from bot.capture.snapshot import capture_page_snapshot, _safe

        idx = snap_state["count"] + 1
        tag = f"{idx:02d}-{step.action}"
        try:
            snap = await capture_page_snapshot(
                page, tag, out_dir=folder / "pages" / f"{idx:02d}-{_safe(step.action)}",
                screenshot=False)
        except Exception as exc:
            print(f"  [!] 页面快照失败（{tag}）: {exc}")
            return
        snap_state["count"] = idx
        snap_state["last"] = time.time()
        snapshots.append({
            "index": idx,
            "action": step.action,
            "ts": step.ts,
            "url": snap.get("url", ""),
            "title": snap.get("title", ""),
            "dir": str(folder / "pages" / f"{idx:02d}-{_safe(step.action)}"),
        })
        print(f"  📸 快照 #{idx}（{step.action}）: {snap.get('title') or snap.get('url')}")

    try:
        while True:
            new = await rec.collect()
            for s in new:
                steps.append(s)
                sid, conf = map_step(s)
                label = getattr(s, "label", "") or getattr(s, "select_label", "")
                detail = s.selector[:44]
                if label:
                    detail = f"{label} · {detail}"
                line = f"[{len(steps):02d}] {s.action:8s} {detail[:66]}  {(s.text or s.value)[:24]}"
                if s.options_count:
                    line += f"  （下拉 {s.options_count} 项）"
                if sid:
                    line += f"  → {sid}"
                print("  " + line)
                if on_step:
                    try:
                        on_step(s)
                    except Exception:
                        pass
                try:
                    await maybe_snapshot(s)
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

    raw = [s.to_dict() for s in steps]
    jsonl = folder / "actions.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for item in raw:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    md = folder / "actions.md"
    md.write_text(format_actions_markdown(name, raw, meta=meta, duration=duration,
                                          snapshots=snapshots),
                  encoding="utf-8")

    log_run(f"人工操作录制完成：{len(steps)} 个动作 / {duration}s → {md}")
    print(f"\n  ✅ 录制完成，共 {len(steps)} 个动作，用时 {duration}s")
    print(f"  📄 改进草案: {md}")
    print(f"  🗂 页面快照: {len(snapshots)} 份")
    print(f"  📁 兼容流程比对: {workflow}")
    return {
        "steps": raw,
        "workflow": str(workflow),
        "jsonl": str(jsonl),
        "markdown": str(md),
        "dir": str(folder),
        "duration_s": duration,
        "snapshots": snapshots,
    }


def _new_actions_dir(name: str) -> Path:
    from bot.capture.snapshot import capture_dir, _safe, _ts
    d = capture_dir() / f"{_ts()}-{_safe(name)}-actions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# Markdown 草案
# ============================================================
def _cell(value, n: int = 60) -> str:
    """表格单元格清洗：竖线/换行会破坏 markdown 表格"""
    t = str(value if value is not None else "")
    return t.replace("|", "／").replace("\n", " ").replace("\r", " ")[:n]


def format_actions_markdown(name: str, raw_steps: list[dict], *,
                            meta: dict | None = None, duration: float = 0,
                            snapshots: list[dict] | None = None) -> str:
    snapshots = snapshots or []
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
             f"- 动作数: {len(raw_steps)} · 时长: {duration}s · 页面快照: {len(snapshots)} 份"]
    if meta:
        for k, v in meta.items():
            lines.append(f"- {k}: {v}")

    lines += ["", "## 时间线", "",
              "| # | 时间 | 动作 | 字段 | 元素 | 文本/值 | 下拉 | 标准步骤 |",
              "|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(raw_steps, 1):
        ts = (s.get("ts") or "")[11:19]
        elem = s.get("selector") or s.get("tag") or ""
        field = s.get("label") or s.get("select_label") or (
            ("弹窗:" + s.get("text", "")[:24]) if s.get("action") == "popup" else "")
        val = s.get("text") or s.get("value") or ""
        cnt = s.get("options_count") or ""
        lines.append(
            f"| {i} | {ts} | {s.get('action','')} | {_cell(field, 24)} | "
            f"`{_cell(elem, 44)}` | {_cell(val, 28)} | {cnt} | "
            f"{s.get('_step','') or '—'} |")
    lines.append("")

    # ---- 下拉记录：这是排查「组别代码读到 8 项提交路径下拉」那类问题的关键证据 ----
    drop_rows = [s for s in raw_steps if s.get("action") == "dropdown"]
    opt_rows = [s for s in raw_steps if s.get("action") == "click" and s.get("option_text")]
    if drop_rows or opt_rows:
        lines += ["## 下拉记录（展开的选项 / 点中的选项）", "",
                  "| 时间 | 来源 | 字段 | 项数 | 选项内容 |", "|---|---|---|---|---|"]
        for s in drop_rows:
            lines.append(
                f"| {(s.get('ts') or '')[11:19]} | 展开 | {_cell(s.get('label') or s.get('select_label'), 24)} | "
                f"{s.get('options_count', 0)} | {_cell(s.get('value'), 120)} |")
        for s in opt_rows:
            lines.append(
                f"| {(s.get('ts') or '')[11:19]} | 选中 | {_cell(s.get('select_label'), 24)} | "
                f"{s.get('options_count', 0)} | 第 {s.get('option_index', 0)} 项：{_cell(s.get('option_text'), 80)} |")
        lines.append("")

    # ---- 弹窗记录：谁挡住了点击、需要点什么按钮 ----
    popups = [s for s in raw_steps if s.get("action") == "popup"]
    if popups:
        lines += ["## 弹窗记录（标题 / 正文 / 按钮）", "",
                  "| 时间 | 标题或正文 | 按钮 |", "|---|---|---|"]
        for s in popups[:40]:
            lines.append(
                f"| {(s.get('ts') or '')[11:19]} | {_cell(s.get('dialog') or s.get('text'), 120)} | "
                f"{_cell(s.get('value'), 60)} |")
        lines.append("")

    # ---- 页面快照：可直接打开读 DOM ----
    if snapshots:
        lines += ["## 关键页面快照（可用于核对选择器 / 页面结构）", "",
                  "| # | 触发 | 时间 | 页面 | 文件 |", "|---|---|---|---|---|"]
        for sn in snapshots:
            rel = Path(sn.get("dir", "")).name
            lines.append(
                f"| {sn.get('index')} | {sn.get('action')} | {(sn.get('ts') or '')[11:19]} | "
                f"{_cell(sn.get('title') or sn.get('url'), 60)} | "
                f"`pages/{rel}/page.html` |")
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
            lines.append(f"| {ts} | {s.get('action')} | {detail} | `{_cell(target, 88)}` |")
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
            field = s.get("label") or s.get("select_label") or ""
            suggestions.append(
                f"- `{s.get('action','')}` `{(s.get('selector') or '')[:50]}` "
                f"字段={field[:20]} 文本={(s.get('text') or '')[:30]} 值={(s.get('value') or '')[:20]}")

    # 下拉项异常多：生产踩过「读到提交路径的 8 个选项」的坑
    big_drops = [s for s in drop_rows if int(s.get("options_count") or 0) >= 6]
    if big_drops:
        suggestions.append(
            f"**下拉项较多（≥6 项）{len(big_drops)} 处**：确认展开的是目标字段的下拉。"
            f"生产曾出现『组别代码』读到『提交路径』下拉 8 项导致卡死，"
            f"建议自动化里按选项文本精确匹配，不要取第一个")
        for s in big_drops[:6]:
            suggestions.append(
                f"- {(s.get('ts') or '')[11:19]} 字段={s.get('label') or '未知'} "
                f"共 {s.get('options_count')} 项：{(s.get('value') or '')[:80]}")

    # 弹窗：记录下来但仍卡住的场景
    if popups:
        suggestions.append("**弹窗提醒**：以下弹窗出现时，自动流程要能按文案分流处理"
                           "（确认 / 取消 / 分支），不能依赖固定按钮位置")
        for s in popups[:6]:
            suggestions.append(
                f"- {(s.get('ts') or '')[11:19]} {(s.get('dialog') or s.get('text') or '')[:60]}"
                f" → 按钮：{s.get('value') or '（未识别）'}")

    # 同一字段多次改值
    by_field = {}
    for s in raw_steps:
        if s.get("action") == "fill" and s.get("value"):
            key = s.get("label") or s.get("selector") or ""
            by_field.setdefault(key, []).append(s.get("value"))
    multi = {k: v for k, v in by_field.items() if len(set(v)) > 1}
    if multi:
        suggestions.append("**同一字段多次改值**：人工在反复试填，自动流程可据此确定最终有效值")
        for k, v in list(multi.items())[:6]:
            suggestions.append(f"- {k[:40]} ← {' / '.join(str(x)[:20] for x in v[:5])}")

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
            field = s.get("label") or ""
            suggestions.append(
                f"- {field[:20]} `{(s.get('selector') or '')[:44]}` ← {(s.get('value') or '')[:40]}")

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
        self.label = d.get("label", "")
        self.select_label = d.get("select_label", "")
        self.option_text = d.get("option_text", "")
        self.dialog = d.get("dialog", "")
