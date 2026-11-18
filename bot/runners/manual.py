"""
人工测试运行器 —— 人工操作 + 程序逐步校验

人工在浏览器里按 STEP 顺序操作，程序只做「观察者」：
  · 每秒采样页面状态，判断当前 STEP 是否已经达成
  · 达成即记录 ✅（含证据：URL / 关键字 / 表格行数）
  · 全程没观察到 → 记 ○ not_observed
运行结束后落一份与自动执行同结构的记录，并自动与自动执行记录对比。

注意：本运行器不会替你点击任何按钮。
"""
from __future__ import annotations

import asyncio
import time

from bot.core.records import Recorder, RunRecord
from bot.envs import EnvSpec, build_environment
from bot.flows.claim_flow import STEP_NAMES
from bot.logging_system import log_run, setup
from bot.runtime import BotStopped, check_stop, clear_stop

from .base import finalize_record, make_ctx

POLL_INTERVAL = 1.5


# ============================================================
# 观察原语（只读，不点击）
# ============================================================
async def _text(page) -> str:
    try:
        return (await page.inner_text("body", timeout=2000)) or ""
    except Exception:
        return ""


async def _count(page, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


async def _js(page, expr: str):
    try:
        return await page.evaluate(expr)
    except Exception:
        return None


def _loss_page(env):
    """从门户上下文里找定损平台 popup"""
    for pg in env.portal_pages:
        try:
            url = pg.url or ""
        except Exception:
            continue
        if "loss_assessment" in url or "lossAssessment" in url or "lossAssessment" in url:
            return pg
    # 兜底：除门户主页面外的第一个页面
    for pg in env.portal_pages:
        try:
            if pg != env.portal_page:
                return pg
        except Exception:
            pass
    return None


def _url(page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


# ============================================================
# 逐步校验规则
# ============================================================
KEYWORDS = {
    "sync": ("已同步", "同步完成", "同步成功"),
    "fix": ("修复", "暂存", "已修改"),
    "loss_submit": ("提交成功", "已提交", "提交完成"),
    "quote_done": ("提交成功", "报价任务已成功提交", "工作流转交成功", "已提交"),
}


async def _check_step1(env, ctx, state):
    url = _url(env.portal_page)
    claim = ctx.get("claim_no", "")
    if claim and claim in url:
        return True, {"url": url}
    # 真实门户可能是弹层/同页跳转：页面文本出现报案号 + 报价处理字样
    txt = await _text(env.portal_page)
    if claim and claim in txt and ("报价处理" in txt or "定损" in txt):
        return True, {"evidence": "页面出现报案号 + 报价处理"}
    return False, {}


async def _check_step2(env, ctx, state):
    loss = _loss_page(env)
    if loss is not None:
        state["loss_page"] = loss
        return True, {"loss_url": _url(loss), "pages": len(env.portal_pages)}
    return False, {}


async def _check_step3(env, ctx, state):
    loss = state.get("loss_page") or _loss_page(env)
    if loss is None:
        return False, {}
    synced = await _js(loss, "() => window.__synced === true")
    if synced:
        return True, {"evidence": "window.__synced=true"}
    txt = await _text(loss)
    for kw in KEYWORDS["sync"]:
        if kw in txt:
            return True, {"evidence": kw}
    return False, {}


async def _check_step4(env, ctx, state):
    loss = state.get("loss_page") or _loss_page(env)
    if loss is None:
        return False, {}
    n = await _count(loss, "#parts-tbody tr")
    if n == 0:
        n = await _count(loss, "table tbody tr")
    if n > 0:
        state["parts_rows"] = n
        return True, {"parts_rows": n}
    return False, {}


async def _check_step5(env, ctx, state):
    url = _url(env.market_page)
    if "market_detail" in url or "vin=" in url:
        return True, {"url": url}
    txt = await _text(env.market_page)
    if "零件" in txt and "市场价" in txt:
        return True, {"evidence": "已进入零件详情页"}
    return False, {}


async def _check_step6(env, ctx, state):
    page = env.market_page
    n = await _count(page, "table tbody tr")
    if n == 0:
        n = await _count(page, ".part-row")
    txt = await _text(page)
    if n > 0 and ("市场价" in txt or "marketPrice" in txt or "比价" in txt):
        return True, {"rows": n}
    return False, {}


async def _check_step7(env, ctx, state):
    loss = state.get("loss_page") or _loss_page(env)
    if loss is None:
        return False, {}
    txt = await _text(loss)
    for kw in KEYWORDS["fix"]:
        if kw in txt:
            return True, {"evidence": kw}
    return False, {}


async def _check_step8(env, ctx, state):
    loss = state.get("loss_page") or _loss_page(env)
    if loss is None:
        return False, {}
    txt = await _text(loss)
    for kw in KEYWORDS["loss_submit"]:
        if kw in txt:
            return True, {"evidence": kw}
    # 弹窗曾出现且已关闭，也视为人工已提交
    shown_now = await _js(loss, "() => document.querySelector('#diagnosis-modal')?.classList.contains('show')")
    if shown_now:
        state["modal_seen"] = True
    elif state.get("modal_seen"):
        return True, {"evidence": "诊断弹窗已确认（人工已提交）"}
    return False, {}


async def _check_step9(env, ctx, state):
    portal = env.portal_page
    txt = await _text(portal)
    for kw in KEYWORDS["quote_done"]:
        if kw in txt:
            return True, {"evidence": kw}
    return False, {}


CHECKS = [
    ("STEP 1", _check_step1),
    ("STEP 2", _check_step2),
    ("STEP 3", _check_step3),
    ("STEP 4", _check_step4),
    ("STEP 5", _check_step5),
    ("STEP 6", _check_step6),
    ("STEP 7", _check_step7),
    ("STEP 8", _check_step8),
    ("STEP 9", _check_step9),
]


# ============================================================
# 主流程
# ============================================================
async def run_manual(claim_no: str, spec: EnvSpec, *,
                     timeout_s: int = 900,
                     transfer_target: str = "",
                     headless: bool = False,
                     logger=None) -> RunRecord:
    setup()
    clear_stop()
    log_run(f"人工测试启动 | 报案号={claim_no} | {spec.label} | 超时={timeout_s}s")

    rec = Recorder(RunRecord(mode="manual", env_portal=spec.portal,
                             env_market=spec.market, claim_no=claim_no)).begin()
    ctx = make_ctx(claim_no, 0.0, transfer_target, mode="manual")
    status = "stopped"

    async with build_environment(spec, headless=headless) as env:
        await env.portal.go_home()
        await env.portal.ensure_ready()

        print(f"\n{'='*70}")
        print(f"  🖐 人工测试模式 —— 请你在浏览器里按顺序操作，程序只做校验")
        print(f"  报案号: {claim_no}")
        print(f"  环境:   {spec.label}")
        print(f"{'='*70}")
        print("  建议顺序：点开报案号 → 编辑打开定损平台 → 同步定损 → 看零件表")
        print("            → VIN 查询 → 零件比价 → 修复超价 → 提交定损 → 报价完成")
        print(f"  程序每 {POLL_INTERVAL}s 校验一次，全部达成或点『■ 停止』结束")
        print(f"{'='*70}")

        state: dict = {}
        idx = 0
        t0 = time.time()
        last_note = 0
        try:
            while idx < len(CHECKS):
                if time.time() - t0 > timeout_s:
                    print(f"\n  ⌛ 人工测试超时（{timeout_s}s），自动结束")
                    break

                sid, check = CHECKS[idx]
                try:
                    ok, detail = await check(env, ctx, state)
                except Exception:
                    ok, detail = False, {}

                if ok:
                    async with rec.step(sid, STEP_NAMES.get(sid, sid)) as st:
                        st.detail.update(detail)
                    print(f"  ✅ {sid} 已达成 {detail if detail else ''}")
                    log_run(f"人工测试 {sid} 已达成")
                    if logger:
                        try:
                            logger(f"✅ {sid} 已达成")
                        except Exception:
                            pass
                    idx += 1
                    last_note = 0
                    continue

                # 每 10 秒提示一次当前等待的步骤
                waited = int(time.time() - t0)
                if waited - last_note >= 10:
                    print(f"  ⏳ 等待人工完成 {sid} {STEP_NAMES.get(sid, '')}（已等待 {waited}s）")
                    last_note = waited

                await asyncio.sleep(POLL_INTERVAL)

            if idx >= len(CHECKS):
                status = "done"
                print(f"\n  🎉 人工测试：9 个步骤全部观察达成（{claim_no}）")
            else:
                status = "partial"

        except BotStopped:
            status = "stopped"
            print(f"\n⏹️ 人工测试已被停止（已观察 {idx}/{len(CHECKS)} 步）")
            log_run(f"人工测试停止 @ 已观察 {idx} 步")

        # 未观察到的步骤补记
        for sid, _ in CHECKS[idx:]:
            if rec.record.step(sid) is None:
                rec.mark(sid, "not_observed", reason="人工测试未观察到该步骤")

        data = {
            "observed_steps": sum(1 for s in rec.record.steps if s.status == "ok"),
            "total_steps": len(CHECKS),
            "parts_rows": state.get("parts_rows", 0),
            "portal_url": _url(env.portal_page),
            "market_url": _url(env.market_page),
        }
        finalize_record(rec, status=status,
                        note=f"人工测试观察 {idx}/{len(CHECKS)} 步",
                        data=data, ignore_env=True)

    return rec.record
