"""
技能重放引擎 —— 把 store.Skill.actions 序列翻译成 Playwright 调用

action 类型（store.py build_skill_from_actions 产出）：
  {"action": "click",   "selector": "button:has-text('编辑')", "text": "编辑"}
  {"action": "dblclick", "selector": "tr:has-text('RDAA...')"}
  {"action": "fill",    "selector": "#vin-input", "value": "LRWY..."}
  {"action": "select",  "selector": "#biz-action", "value": "报价完成"}
  {"action": "press",   "key": "Enter"}
  {"action": "wait",    "ms": 1200}
  {"action": "dispatch", "selector": "[data-bot-claim-pick='1']", "event": "click"}

如果 selector 是纯文本（没 CSS 语法），退化成 get_by_text(selector)。
如果带 CSS 语法（:, [, #, ., >, + 等），直接 locator(selector)。
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from bot.logging_system import log_run


def _selector_kind(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "empty"
    if re.match(r"^[.#\[]", s) or ":" in s or ">" in s or "+" in s:
        return "css"
    if s.startswith("text=") or s.startswith("get_by_text"):
        return "exact-text"
    # 纯字符串但带特殊字符（/ 空格 空格）—— 当文本处理
    return "text"


async def _resolve_locator(page, selector: str, *, frame=None):
    loc = frame or page
    kind = _selector_kind(selector)
    if kind == "css":
        base = loc.locator(selector).first
        if frame is not None:
            return base
        try:
            if await base.count() > 0:
                return base
        except Exception:
            pass
        # 录制时 CSS path 是按各 frame 的 document 生成的 → 主页面找不到就去 iframe 里找
        for fr in page.frames[1:]:
            try:
                cand = fr.locator(selector).first
                if await cand.count() > 0:
                    return cand
            except Exception:
                continue
        return base
    if kind == "exact-text":
        text = selector.split("=", 1)[1].strip()
        return loc.get_by_text(text, exact=True).first
    if kind == "text":
        return loc.get_by_text(selector, exact=True).first
    return None


async def replay_skill(page, skill, *, frame=None, timeout_s: float = 40.0) -> dict:
    """
    重放一个技能的动作序列。

    返回: {"ok": bool, "steps_done": int, "error": str|None, "verification_ok": bool}
    不会抛异常 —— 失败时返回 ok=False，由上层决定怎么处理。
    """
    actions = skill.actions or []
    result = {"ok": False, "steps_done": 0, "error": None, "verification_ok": False,
              "skill_id": skill.id, "skill_name": skill.name}

    try:
        start = asyncio.get_event_loop().time()
        for i, a in enumerate(actions):
            # 超时保护
            if asyncio.get_event_loop().time() - start > timeout_s:
                result["error"] = f"重放超时 {timeout_s}s"
                break

            act = (a.get("action") or "").lower()
            log_run(f"  🧠 技能#{skill.id.split('-')[-1]} 重放 [{i+1}/{len(actions)}] {act} "
                    f"{(a.get('selector') or a.get('key') or a.get('ms') or a.get('event') or '')[:40]}")

            try:
                if act == "wait":
                    ms = int(a.get("ms") or 500)
                    # 夹到 100ms ~ 5s，防止学习时录了 60 秒空白
                    await asyncio.sleep(max(0.1, min(ms, 5000)) / 1000.0)

                elif act == "click":
                    loc = await _resolve_locator(page, a.get("selector", ""), frame=frame)
                    if loc is None:
                        raise RuntimeError(f"selector 为空或无法解析: {a.get('selector','')}")
                    if await loc.count() == 0:
                        raise RuntimeError(f"找不到元素: {a.get('selector','')}")
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    await loc.click(timeout=6000)

                elif act == "dblclick":
                    loc = await _resolve_locator(page, a.get("selector", ""), frame=frame)
                    if loc is None or await loc.count() == 0:
                        raise RuntimeError(f"找不到元素: {a.get('selector','')}")
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    await loc.dblclick(timeout=6000)

                elif act == "fill":
                    loc = await _resolve_locator(page, a.get("selector", ""), frame=frame)
                    if loc is None or await loc.count() == 0:
                        raise RuntimeError(f"找不到元素: {a.get('selector','')}")
                    await loc.click(timeout=4000)
                    try:
                        await loc.press("Control+A")
                    except Exception:
                        pass
                    val = str(a.get("value") or a.get("text") or "")
                    await loc.fill(val)
                    await loc.dispatch_event("change")

                elif act == "select":
                    loc = await _resolve_locator(page, a.get("selector", ""), frame=frame)
                    if loc is None or await loc.count() == 0:
                        raise RuntimeError(f"找不到元素: {a.get('selector','')}")
                    await loc.select_option(label=str(a.get("value") or a.get("text") or ""))

                elif act == "press":
                    await page.keyboard.press(str(a.get("key") or "Enter"))

                elif act == "dispatch":
                    loc = await _resolve_locator(page, a.get("selector", ""), frame=frame)
                    if loc is None or await loc.count() == 0:
                        raise RuntimeError(f"找不到元素: {a.get('selector','')}")
                    await loc.dispatch_event(str(a.get("event") or "click"))

                else:
                    log_run(f"    [i] 未知 action={act}，跳过")
                    continue

                result["steps_done"] += 1
                await asyncio.sleep(0.4)

            except Exception as exc:
                result["error"] = f"动作#{i+1}失败: {act} {a.get('selector','')[:40]} → {str(exc)[:100]}"
                break

        # ---- 验证 ----
        ver = skill.verification or {}
        url_ok = True
        text_ok = True
        url_patterns = [p.lower() for p in ver.get("url_pattern", []) if p]
        text_any = [t for t in ver.get("text_any", []) if t]
        if url_patterns:
            cur_url = (page.url or "").lower()
            url_ok = any(re.search(p, cur_url) for p in url_patterns)
        if text_any:
            body_text = ""
            try:
                body_text = (await page.locator("body").inner_text(timeout=3000)).lower()
            except Exception:
                pass
            text_ok = any(t.lower() in body_text for t in text_any)
        result["verification_ok"] = url_ok and text_ok
        result["ok"] = result["error"] is None and result["verification_ok"]

    except Exception as exc:
        result["error"] = f"重放顶层异常: {type(exc).__name__}: {str(exc)[:120]}"
        result["ok"] = False

    log_run(f"  🧠 技能#{skill.id.split('-')[-1]} 重放结束: ok={result['ok']} "
            f"steps={result['steps_done']}/{len(actions)} verification={result['verification_ok']} "
            f"confidence={round(skill.success_count / (skill.success_count + skill.failure_count + 1), 2)}")
    return result
