"""
Skill hooks —— 挂在 claim_flow 里的失败 / 学习钩子

设计原则：
  不侵入正常业务流程。hook 都是「附加层」：
  · skill_failed_hook(step, error, page, context)   —— step 抛异常时调
  · skill_learn_hook(step, page, context)             —— 学习模式下手动触发

skill_failed_hook 流程：
  1. 查 skill_store.match_skill（step + url + error）
  2. 没匹配到 → 返回 None（保持原流程 skip）
  3. 匹配到 → replay_skill 重放 → record_outcome 更新置信度
  4. 重放成功 → 返回 ("ok", page_after) → 上层可以继续往下走
  5. 重放失败 → 返回 ("fail", None) → 上层走原流程 skip
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from bot.logging_system import log_run
from bot.skills.store import match_skill, record_outcome, confidence
from bot.skills.replay import replay_skill


async def skill_failed_hook(step: str, error_text: str, page, context: dict,
                            *, max_attempts: int = 1) -> tuple[str, Optional[object], Optional[str]]:
    """
    在某个 step 抛异常时调用。尝试匹配一个技能并重放。

    返回: ("ok", page_after, skill_id)     重放成功，上层可以继续
          ("fail", None, skill_id|None)    没匹配 / 重放失败，上层跳过这个案件
          ("skip", None, None)             没匹配到技能，正常跳过
    """
    url = ""
    try:
        url = page.url or ""
    except Exception:
        pass

    # 页面文本只抓前 1000 字（太多没用，太耗时）
    page_text = ""
    try:
        text = await page.locator("body").inner_text(timeout=1500)
        page_text = (text or "")[:1000]
    except Exception:
        pass

    skill = match_skill(step, error_text, url, page_text, min_confidence=0.3)
    if skill is None:
        log_run(f"  🧠 step={step} 没匹配到合适的技能（或所有技能置信度 < 0.3）")
        return ("skip", None, None)

    log_run(f"  🧠 匹配到技能 [{skill.id.split('-')[-1]}] {skill.name} "
            f"（置信度 {round(confidence(skill), 2)}，成功 {skill.success_count} / 失败 {skill.failure_count}）")

    # 防滥用：一个案件里最多重放 1 次；失败了回退到正常流程
    attempt_key = f"__skill_replayed__{step}"
    if context.get(attempt_key):
        log_run(f"  🧠 本案件 step={step} 已经重放过一次了，不再重复")
        return ("skip", None, skill.id)
    context[attempt_key] = True

    res = await replay_skill(page, skill)

    success = res.get("ok", False) and res.get("verification_ok", False)
    if success:
        record_outcome(skill.id, success=True)
        log_run(f"  🧠 技能重放成功！step={step} 问题已自动解决，接下来正常往下走")
        return ("ok", page, skill.id)
    else:
        record_outcome(skill.id, success=False)
        err = res.get("error") or "重放未通过验证"
        log_run(f"  🧠 技能重放失败: {err} —— 回退到原流程跳过此案件")
        return ("fail", None, skill.id)


# ============================================================
# 学习钩子：GUI 开启「学习模式」+ 程序在某个 step 卡住后调用
# 暂停自动流程，把当前 step + 错误 + 页面上下文打包成「待学习」记录，
# 等用户手动解决完后 save_skill_from_context 调用。
# ============================================================

_PENDING_KEY = "__skill_pending__"


def mark_pending_learn(step: str, error_text: str, url: str, context: dict) -> None:
    """把这次失败的上下文挂在 context 上，等 GUI 提示用户手动解决后保存"""
    context[_PENDING_KEY] = {
        "step": step,
        "error": error_text,
        "url": url,
        "at": asyncio.get_event_loop().time() if False else 0,  # 占位，实际存 wall time 也没事
    }


def pop_pending_learn(context: dict) -> Optional[dict]:
    v = context.get(_PENDING_KEY)
    if v:
        context.pop(_PENDING_KEY, None)
    return v


def has_pending_learn(context: dict) -> bool:
    return bool(context.get(_PENDING_KEY))


# ============================================================
# 辅助：把 BotRunResult + actions.jsonl 组合成 Skill
# ============================================================
def build_skill_from_run(actions: list[dict], *,
                         trigger_step: str,
                         trigger_url_patterns: list[str] | None = None,
                         trigger_error_patterns: list[str] | None = None,
                         trigger_sample_texts: list[str] | None = None,
                         verification_url_patterns: list[str] | None = None,
                         verification_text_any: list[str] | None = None,
                         context: dict | None = None,
                         name: str = ""):
    from bot.skills.store import build_skill_from_actions
    if not name:
        name = f"auto-skill-{trigger_step.replace(' ','_').replace('/','')}"
    return build_skill_from_actions(
        actions,
        name=name,
        trigger_step=trigger_step,
        trigger_url_patterns=trigger_url_patterns,
        trigger_error_patterns=trigger_error_patterns,
        trigger_sample_texts=trigger_sample_texts,
        verification_url_patterns=verification_url_patterns,
        verification_text_any=verification_text_any,
        context=context,
    )


# ============================================================
# 学习模式挂起区 —— GUI（tk 主线程） ⇄ 机器人（事件循环）的线程安全桥
# ============================================================
class LearnSession:
    """某一步失败后挂起，等 GUI 点「💾 保存学习」或「⏭ 跳过本案件」。

    GUI:  LearnSession.instance().decide("save" | "skip")
    bot:  action = await LearnSession.instance().wait()
    """

    _inst: Optional["LearnSession"] = None

    @classmethod
    def instance(cls) -> "LearnSession":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Optional[dict] = None
        self._action = ""
        self._event = threading.Event()

    def begin(self, info: dict) -> None:
        with self._lock:
            self._pending = dict(info)
            self._action = ""
            self._event.clear()

    def snapshot(self) -> Optional[dict]:
        with self._lock:
            return dict(self._pending) if self._pending else None

    def decide(self, action: str) -> bool:
        """GUI 调用：save / skip。没有挂起的会话时返回 False。"""
        with self._lock:
            if not self._pending:
                return False
            self._action = action
            self._event.set()
            return True

    def clear(self) -> None:
        with self._lock:
            self._pending = None
            self._action = ""
            self._event.clear()

    async def wait(self, timeout_s: float = 1800.0) -> str:
        """等 GUI 决定。返回 save / skip / stop / timeout。"""
        from bot.runtime import stop_requested
        start = time.time()
        while not self._event.is_set():
            if stop_requested():
                return "stop"
            if time.time() - start > timeout_s:
                return "timeout"
            await asyncio.sleep(0.2)
        with self._lock:
            return self._action or "skip"


_USER_ACTIONS = ("click", "dblclick", "fill", "select", "press")


async def collect_and_save_skill(page, recorder, *, step: str, error_text: str,
                                 claim_no: str = "") -> Optional[str]:
    """把「失败 → 人工手动处理」这段操作存成技能。返回 skill id；没录到动作返回 None。"""
    from bot.skills.store import build_skill_from_actions, save_skill

    steps = []
    try:
        steps = await recorder.collect()
    finally:
        try:
            recorder.stop()
        except Exception:
            pass

    if not any(s.action in _USER_ACTIONS for s in steps):
        return None

    url = ""
    try:
        url = page.url or ""
    except Exception:
        pass
    path = (urlparse(url).path or "").lower()
    first_line = (error_text or "").splitlines()[0][:60]

    skill = build_skill_from_actions(
        [s.to_dict() for s in steps],
        name=f"learn-{step.replace(' ', '_')}-{datetime.now():%m%d-%H%M%S}",
        trigger_step=step,
        trigger_url_patterns=[re.escape(path)] if path else [],
        trigger_error_patterns=[re.escape(first_line.lower())] if first_line else [],
        verification_url_patterns=[re.escape(path)] if path else [],
        context={"claim_no": claim_no, "failed_step": step, "failed_error": first_line},
    )
    # 人工演示成功过一次 → success_count=1，置信度 0.5，否则新技能永远是 0 分匹配不上
    skill.success_count = 1
    save_skill(skill)
    return skill.id
