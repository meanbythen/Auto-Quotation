"""
唯一流程处理逻辑 —— 四种运行模式（模拟循环 / 真实循环 / 单步测试 / 人工测试）
全部调用这里，保证行为一致、结果可对比。

本模块不关心：
  · 当前是模拟环境还是真实生产环境   → 由 bot/envs 的 PortalPort / MarketPort 决定
  · 是循环跑还是单次跑、要不要逐步暂停 → 由 bot/runners + StepGate 决定
  · 记录写到哪里                     → 由 bot/core/records.Recorder 决定

步骤与「卡在哪一步」的文案保持与原流程一致。
"""
from __future__ import annotations

from typing import Optional

from bot.core.records import Recorder, RunRecord
from bot.logging_system import log_run, log_audit
import bot.config as cfg
from bot.page_objects import (
    step_home_click_claim,
    step_read_opinion_and_decide,
    step_quote_open_loss_edit,
    step_loss_sync,
    step_loss_extract_data,
    step_loss_cancel_direct_supply,
    step_market_vin_search,
    step_market_query_parts,
    step_loss_fix_over_market,
    step_loss_submit,
    step_quote_finalize,
    step_quote_return_for_color_parts,
    step_quote_submit_superior,
    step_task_transfer_back,
    step_quote_reopen_finalize,
)
from bot.routing import ROUTE_NORMAL, ROUTE_HIGH_AMOUNT, is_permission_error
from bot.step_gate import StepGate
from bot.models import BotRunResult
from bot.pricing import get_price_service
from bot.pricing.providers import get_providers

STEP_NAMES = {
    "STEP 1": "首页 → 点击报案号",
    "STEP 1.5": "报价处理 → 读取最新意见并判断（P0 只建议）",
    "STEP 2": "报价处理 → 点编辑打开定损平台",
    "STEP 3": "定损平台 → 同步定损",
    "STEP 4": "定损平台 → 提取 VIN + 换件列表",
    "STEP 5": "一车一件 → VIN 查询",
    "STEP 6": "一车一件 → 逐个零件查市场价",
    "STEP 7": "定损平台 → 自动修复超市场价 + 暂存",
    "STEP 8": "定损平台 → 提交定损单",
    "STEP 9": "报价处理 → 报价完成 → 提交任务",
    "STEP 9a": "报价页 → 业务动作『提交上级』",
    "STEP 9b": "任务中心 → 平级移交回本人",
    "STEP 9c": "重新进入报价页 → 报价完成",
}


def _workflow_step_kwargs(action: str) -> dict:
    """读取流程编辑器 main_flow.json 里该节点配置的 kwargs（读不到就返回空）"""
    try:
        from bot.workflow import Workflow
        wf = Workflow.load("main_flow")
        for node in wf.nodes:
            if node.type == "call_step" and node.action == action:
                return dict(node.kwargs or {})
    except Exception as exc:
        log_run(f"读取流程参数失败（{action}）: {exc}")
    return {}


def _market_ban_left(ctx: dict) -> float:
    """一车一件封禁冷却剩余秒数（关闭熔断开关 → 0，即不拦）"""
    if not ctx.get("market_breaker", True):
        return 0.0
    try:
        from bot.page_objects import market_ban_remaining
        return market_ban_remaining()
    except Exception:
        return 0.0


async def _run_transfer_chain(portal_page, ctx: dict, rec: Recorder) -> None:
    """提交上级之后：任务中心平级移交回本人 → 重开报价页报价完成"""
    gate = StepGate.instance()

    async with rec.step("STEP 9b", STEP_NAMES["STEP 9b"]):
        await gate.checkpoint("STEP 9b 任务中心 → 平级移交回本人")
        await step_task_transfer_back(portal_page, ctx)

    async with rec.step("STEP 9c", STEP_NAMES["STEP 9c"]):
        await gate.checkpoint("STEP 9c 重新进入报价页 → 报价完成")
        await step_quote_reopen_finalize(portal_page, ctx)


async def _finalize_with_fallback(portal_page, ctx: dict, result: BotRunResult,
                                  rec: Recorder) -> str:
    """
    STEP 9 集中路由 + 运行时兜底

    ① STEP 9 内先判『报价汇总 → 换件金额』：超过 4 万 → 直接业务动作『提交上级』，
       再平级移交回本人（该报案号重新回到首页列表，按基础流程继续处理）
    ② 未超限 → 正常「报价完成」提交
    ③ 提交失败且提示属于权限类错误 → 仍走补救链（提交上级 → 平级移交 → 报价完成）
    非权限类异常原样抛出，交给外层失败/跳过逻辑。
    """
    gate = StepGate.instance()
    claim_no = ctx.get("claim_no", "")

    try:
        # 透传「提交上级」节点配置（路径 / 组别），金额超限分支直接复用同一套参数
        await step_quote_finalize(
            portal_page, ctx, **_workflow_step_kwargs("quote_submit_superior"))
    except Exception as exc:
        error_text = str(exc)
        if not is_permission_error(error_text):
            try:
                tip_text = "\n".join(
                    await portal_page.locator(cfg.SELECTORS_PORTAL["quote_result_tip"]).all_inner_texts()
                )
            except Exception:
                tip_text = ""
            if not is_permission_error(tip_text):
                raise
            error_text = tip_text

        # ★ 已经提交过一次上级 → 不再连着提；只有『请提交远程操作平台处理』才走分中心报价
        if ctx.get("high_amount_transferred"):
            from bot.page_objects import _handle_localized_part_50k_dialog, \
                step_quote_submit_superior as _submit_superior
            handled = await _handle_localized_part_50k_dialog(portal_page, timeout=2.0)
            if handled:
                log_run(f"报价完成仍无法完成 + 『请提交远程操作平台处理』→ 分中心报价 | {claim_no}")
                await _submit_superior(
                    portal_page, ctx,
                    submit_path=cfg.TRANSFER_SUBMIT_PATH_BRANCH,
                    group_option=cfg.TRANSFER_GROUP_OPTION_BRANCH,
                    group_code=cfg.TRANSFER_GROUP_CODE_BRANCH,
                    group_key=cfg.TRANSFER_GROUP_OPTION_KEY_BRANCH,
                    group_exclude="",
                )
                ctx["branch_center_done"] = True
                result.status = "done"
                result.messages.append("分中心报价提交成功")
                return ROUTE_HIGH_AMOUNT
            log_run(f"已提交过上级，禁止重复提交上级 → 保持失败 | {claim_no}")
            raise

        log_run("route=high_amount 触发（权限不足）")
        print(f"\n🔀 检测到权限不足提示 → 切换高额补救分支：{error_text.splitlines()[0][:120]}")

        async with rec.step("STEP 9a", STEP_NAMES["STEP 9a"]) as st:
            await gate.checkpoint("STEP 9a 报价页 → 业务动作『提交上级』")
            # 先标记：本处理轮次内即使提交失败，也不在兜底/重试里再次提交上级
            ctx["high_amount_transferred"] = True
            await step_quote_submit_superior(
                portal_page, ctx, **_workflow_step_kwargs("quote_submit_superior"))
            st.detail.update(submit_path=ctx.get("transfer_submit_path", ""),
                             group=ctx.get("transfer_group_option", ""))

        # 标记已提过上级：9c 重新报价时不再触发金额分支
        ctx["high_amount_transferred"] = True
        await _run_transfer_chain(portal_page, ctx, rec)

        result.status = "done"
        result.messages.append("high_amount 补救成功")
        log_run(f"route=high_amount 补救成功 | 报案号={claim_no}")
        print(f"\n  ✅ 高额补救完成：提交上级 → 平级移交 → 报价完成（{claim_no}）")
        return ROUTE_HIGH_AMOUNT

    # ★ 分中心报价已提交 → 本环节结束，不做平级移交
    if ctx.pop("branch_center_done", False):
        result.status = "done"
        result.messages.append("分中心报价已提交（本环节完成）")
        log_run(f"route=branch_center 完成 | 报案号={claim_no}")
        print(f"\n  ✅ 分中心报价完成：任务已提交远程操作平台（{claim_no}）")
        return ROUTE_HIGH_AMOUNT

    # ★ 二次处理：提交上级后点快速链接『处理』，已在当前页完成『报价完成』→ 不做平级移交
    if ctx.pop("quick_process_done", False):
        result.status = "done"
        result.messages.append("二次处理：提交上级 → 快速链接处理 → 报价完成")
        log_run(f"route=high_amount（二次处理快速链接）完成 | 报案号={claim_no}")
        print(f"\n  ✅ 二次处理完成：提交上级 → 快速链接处理 → 报价完成（{claim_no}）")
        return ROUTE_HIGH_AMOUNT

    # STEP 9 内部已按『报价汇总换件金额 > 4 万』走了提交上级 → 继续平级移交 + 报价完成
    if ctx.pop("transfer_back_needed", False):
        print(f"\n🔀 换件金额超 4 万 → 已提交上级（九级报价），继续平级移交回本人")
        await _run_transfer_chain(portal_page, ctx, rec)
        result.status = "done"
        result.messages.append("换件金额超 4 万 → 提交上级 → 平级移交成功")
        log_run(f"route=high_amount（金额超限）完成 | 报案号={claim_no}")
        print(f"\n  ✅ 高额分支完成：提交上级 → 平级移交 → 报价完成（{claim_no}）")
        return ROUTE_HIGH_AMOUNT

    result.status = "done"
    return ROUTE_NORMAL


def _parts_snapshot(parts) -> list[dict]:
    out = []
    for p in parts or []:
        out.append({
            "index": p.index, "code": p.code, "name": p.name, "qty": p.qty,
            "price_type": p.price_type, "local_price": p.local_price,
            "list_price": p.list_price, "market_price": p.market_price,
            "within_market": p.within_market, "fixed_price": p.fixed_price,
            # ===== 新增 =====
            "price_scheme": p.price_scheme,
            "price_type_code": p.price_type_code,
            "s_price_value": p.s_price_value,
        })
    return out


async def run_claim_flow(env, ctx: dict, result: Optional[BotRunResult] = None,
                         *, dry_run: bool = False,
                         rec: Optional[Recorder] = None) -> str:
    """
    跑一个报案号的完整流程（STEP 1 → STEP 9）

    前置：env.portal_page 已在门户首页（由 runner 负责 go_home / ensure_ready）
    返回：normal / high_amount / no_parts / dry_run
    """
    result = result or BotRunResult()
    result.vehicle.claim_no = ctx.get("claim_no", "")
    if rec is None:
        rec = Recorder(RunRecord(mode=ctx.get("mode", "flow"),
                                 env_portal=getattr(env.portal, "label", "?"),
                                 env_market=getattr(env.market, "label", "?"),
                                 claim_no=ctx.get("claim_no", "")))

    gate = StepGate.instance()
    portal = env.portal_page
    claim_no = ctx.get("claim_no", "")

    # ========== skill 学习 / 自动修复开关 ==========
    # 没显式开启就跳过 hook（不碰原有流程），避免学习数据不足时误套用
    _SKILL_ENABLED = bool(ctx.get("skill_learning", False))
    _SKILL_STEP_FAILS: dict[str, int] = {}  # step -> 本案件里这个 step 已经被 skill 救过几次

    def _is_market_ban(exc: BaseException) -> bool:
        """一车一件反爬熔断抛出的异常 → 应降级为定损价，而不是把整个案件判失败"""
        return "反爬封禁" in str(exc)

    async def _skill_try(step_name: str, page, error_text: str, ctx_ref: dict) -> bool:
        """step 抛异常后调这个。救成了返回 True，调用方重跑一次 step。"""
        if not _SKILL_ENABLED:
            return False
        if _SKILL_STEP_FAILS.get(step_name, 0) >= 1:  # 一个案件一个 step 最多救 1 次
            return False
        try:
            from bot.skills.hooks import skill_failed_hook
            res = await skill_failed_hook(step_name, error_text, page, ctx_ref)
            if res[0] == "ok":
                _SKILL_STEP_FAILS[step_name] = _SKILL_STEP_FAILS.get(step_name, 0) + 1
                return True
        except Exception:
            pass
        # 技能库救不了 → 学习模式：挂起，等用户手动处理完再存成新技能
        return await _learn_pause(step_name, page, error_text, ctx_ref)

    async def _learn_pause(step_name: str, page, error_text: str, ctx_ref: dict) -> bool:
        """学习模式：录制用户手动修正的动作 → 存成技能 → 重跑本步。返回是否需要重跑。"""
        from bot.skills.hooks import LearnSession, collect_and_save_skill
        from bot.runtime import BotStopped

        bus = LearnSession.instance()
        action_rec = None
        try:
            from bot.recorder import Recorder
            action_rec = Recorder()
            await action_rec.attach(page.context)
        except Exception as exc:
            log_run(f"  🧠 录制器注入失败（{type(exc).__name__}）→ 仅记录失败上下文")
            action_rec = None

        bus.begin({
            "step": step_name,
            "error": (error_text or "").splitlines()[0][:200],
            "url": getattr(page, "url", "") or "",
            "claim_no": ctx_ref.get("claim_no", ""),
        })
        print(f"\n  🧠 学习模式已暂停（卡在 {step_name}）")
        print(f"     请在浏览器里手动把这一步处理完，然后回到控制台点『💾 保存学习』")
        print(f"     不想要这次经验就点『⏭ 跳过本案件』；点『停止扫描』也会立即退出")
        log_run(f"学习模式挂起 @ {step_name} | {bus.snapshot()}")

        action = await bus.wait()
        if action == "stop":
            bus.clear()
            raise BotStopped(f"学习模式等待中用户停止 @ {step_name}")
        if action != "save":
            bus.clear()
            log_run(f"学习模式未保存（{action}）→ 按原流程处理 @ {step_name}")
            return False

        skill_id = None
        if action_rec is not None:
            try:
                skill_id = await collect_and_save_skill(
                    page, action_rec, step=step_name,
                    error_text=error_text, claim_no=ctx_ref.get("claim_no", ""))
            except Exception as exc:
                log_run(f"  🧠 保存技能失败: {type(exc).__name__}: {exc}")
        bus.clear()

        if skill_id:
            print(f"  🧠 已存为新技能 [{skill_id.split('-')[-1]}]，下次同类错误自动重放")
            log_run(f"学习模式保存技能: {skill_id}")
        else:
            print(f"  🧠 没录到手动动作 → 不保存技能，直接重跑 {step_name}")
            log_run(f"学习模式：未录到动作，不保存 @ {step_name}")

        _SKILL_STEP_FAILS[step_name] = 1   # 本案件该步不再重复挂起
        return True

    # ==================== PART 1: 理赔门户 ====================
    async with rec.step("STEP 1", STEP_NAMES["STEP 1"]):
        await gate.checkpoint("STEP 1 首页 → 点击报案号")
        try:
            quote_page = await step_home_click_claim(portal, ctx, claim_no=claim_no)
        except Exception as exc:
            if await _skill_try("STEP 1", portal, str(exc), ctx):
                quote_page = await step_home_click_claim(portal, ctx, claim_no=claim_no)
            else:
                raise
        ctx["quote_page"] = quote_page

    # ==================== STEP 1.5: 读取报价处理意见（P0 只建议不执行） ====================
    async with rec.step("STEP 1.5", STEP_NAMES["STEP 1.5"]):
        await gate.checkpoint("STEP 1.5 读取报价处理意见并判断（只建议）")
        decision = await step_read_opinion_and_decide(quote_page, ctx)
        if decision.get("action") and decision["action"] != "unknown":
            advice = (f"意见判断={decision.get('label')}"
                      f"（置信度{float(decision.get('confidence') or 0):.2f}）")
            ctx["opinion_advice_note"] = advice
            result.messages.append(advice)
            print(f"  🧠 建议动作: {advice}（P0 仅记录，不改变执行）")

    async with rec.step("STEP 2", STEP_NAMES["STEP 2"]) as st:
        await gate.checkpoint("STEP 2 报价处理 → 点编辑打开定损平台")
        try:
            loss_page = await step_quote_open_loss_edit(quote_page, ctx)
        except Exception as exc:
            if await _skill_try("STEP 2", quote_page, str(exc), ctx):
                loss_page = await step_quote_open_loss_edit(quote_page, ctx)
            else:
                raise
        ctx["loss_page"] = loss_page
        st.detail["loss_url"] = getattr(loss_page, "url", "")

    async with rec.step("STEP 3", STEP_NAMES["STEP 3"]):
        await gate.checkpoint("STEP 3 定损平台 → 同步定损")
        try:
            await step_loss_sync(loss_page, ctx)
        except Exception as exc:
            if await _skill_try("STEP 3", loss_page, str(exc), ctx):
                await step_loss_sync(loss_page, ctx)
            else:
                raise

    async with rec.step("STEP 4", STEP_NAMES["STEP 4"]) as st:
        await gate.checkpoint("STEP 4 定损平台 → 提取 VIN + 换件列表")
        try:
            vehicle, parts = await step_loss_extract_data(loss_page, ctx)
        except Exception as exc:
            if await _skill_try("STEP 4", loss_page, str(exc), ctx):
                vehicle, parts = await step_loss_extract_data(loss_page, ctx)
            else:
                raise
        ctx["vehicle"] = vehicle
        ctx["parts"] = parts
        result.vehicle = vehicle
        result.parts = parts
        st.detail.update(vin=vehicle.vin, parts=len(parts))

    # ==================== 分支：零件名称颜色需人工处理 → 先正常走完 STEP 5~8 ====================
    # 生产规则：不再在 STEP 4 直接跳过；同步定损、比价、改价、提交定损单照常执行，
    # 只到 STEP 9 选业务动作『报价完成』之前才跳过提交。
    if ctx.get("skip_part_color") or ctx.get("return_part_color"):
        bad = ctx.get("skip_part_color_parts") or []
        names = "、".join(str(p.get("name")) for p in bad[:5])
        colors = "、".join(str(p.get("color")) for p in bad[:5])
        action = ("STEP 9 选业务动作『退回』"
                  if ctx.get("return_part_color") else "STEP 9 前跳过")
        print(f"\n{'='*70}")
        print(f"  ⚠️ 零件名称颜色需人工处理 → 先正常执行 STEP 5~8，{action}")
        print(f"  报案号: {claim_no}")
        print(f"  零件:   {names}")
        print(f"  颜色:   {colors}")
        print(f"{'='*70}")
        log_run(f"零件名称颜色需人工处理 → 继续 STEP 5~8，{action} | "
                f"报案号={claim_no} | {names} | {colors}")

    # ==================== 分支：直供价有值 → 取消直供价后按正常案件继续 ====================
    # 处理方式：切换「价格类型」把直供价清掉（R → 原类型 A/S）→ 保存 →
    #          提交行填「接受系统点选」→ 重新提取零件（价格已刷新）→ 继续比价/改价/提交
    if ctx.get("skip_direct_supply"):
        print(f"\n{'='*70}")
        print(f"  🔧 该报案『直供价』列已有数值 → 先切换价格类型取消直供价")
        print(f"  报案号: {claim_no}")
        print(f"{'='*70}")
        log_run(f"直供价非0 → 执行取消直供价流程 | 报案号={claim_no}")
        try:
            vehicle, parts = await step_loss_cancel_direct_supply(loss_page, ctx, parts=parts)
            ctx["vehicle"], ctx["parts"] = vehicle, parts
            result.vehicle, result.parts = vehicle, parts
            log_audit(f"取消直供价完成 | 零件 {len(parts)} 个 | VIN={getattr(vehicle, 'vin', '')}")
        except Exception as exc:
            print(f"\n  ⚠️ 取消直供价失败: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            log_audit(f"取消直供价失败，跳过该报案：{exc}")
            result.status = "skipped"
            result.messages.append(f"直供价取消失败 → 跳过：{exc}")
            for code in ("STEP 5", "STEP 6", "STEP 7", "STEP 8", "STEP 9"):
                rec.mark(code, "skipped", reason="直供价取消失败")
            return "skip_direct_supply"

        # 取消后仍有直供价 → 视为失败，跳过（避免提交带直供价的单据）
        if ctx.get("skip_direct_supply"):
            print(f"\n  ⏭ 取消直供价后直供价仍非空 → 跳过该报案")
            result.status = "skipped"
            result.messages.append("直供价未能清空 → 跳过该报案")
            for code in ("STEP 5", "STEP 6", "STEP 7", "STEP 8", "STEP 9"):
                rec.mark(code, "skipped", reason="直供价未能清空")
            return "skip_direct_supply"

    # ==================== 分支：无换件案件 ====================
    if not parts:
        print(f"\n  [i] 该案件无需换件（零件列表为空）→ 跳过一车一件价格比对，直接提交")
        if dry_run:
            result.status = "dry_run"
            rec.mark("STEP 8", "skipped", reason="演练模式")
            rec.mark("STEP 9", "skipped", reason="演练模式")
            print(f"\n  🛡 演练模式：无换件案件 → 跳过『提交定损』与『报价完成』")
            return "dry_run"

        await loss_page.bring_to_front()
        async with rec.step("STEP 8", STEP_NAMES["STEP 8"]):
            await gate.checkpoint("STEP 8 定损平台 → 提交定损单")
            await step_loss_submit(loss_page, ctx)

        await quote_page.bring_to_front()
        async with rec.step("STEP 9", STEP_NAMES["STEP 9"]) as st:
            await gate.checkpoint("STEP 9 报价处理 → 报价完成 → 提交任务")
            st.detail["route"] = await _finalize_with_fallback(quote_page, ctx, result, rec)

        result.status = "done"
        print(f"\n{'='*70}")
        print(f"  🎉 流程成功完成！（无换件案件）")
        print(f"  报案号:   {claim_no}")
        print(f"  VIN:      {vehicle.vin}")
        print(f"{'='*70}")
        return "no_parts"

    # ==================== PART 2: 一车一件 ====================
    # ---------- 阈值前置筛选：先读零件本地价，再决定要不要进一车一件 ----------
    # 全部零件都低于阈值时连 VIN 都不查，直接提交定损单（省一次一车一件流量）
    thresh = float(ctx.get("market_price_threshold") or 0)
    target_parts = parts
    if not ctx.get("skip_market") and thresh > 0:
        target_parts = []
        cheap_parts = []
        for p in parts:
            # ★ 阈值前置筛选：用 S价格值（msRetailPrice）判断，而不是 local_price
            # A 类方案本地价已打折可能低于阈值，但 4S 原价可能高于阈值
            compare_val = p.s_price_value or p.local_price
            if compare_val < thresh:
                cheap_parts.append(p)
                p.market_price = None
                p.within_market = True   # 便宜零件视为合规，直接以定损价提交
            else:
                target_parts.append(p)
        if cheap_parts:
            print(f"\n  💡 阈值 ¥{thresh:g} 前置筛选：{len(cheap_parts)} 件 <¥{thresh:g} 不查一车一件，"
                  f"{len(target_parts)} 件 ≥¥{thresh:g} 需要查询")
            log_audit(f"阈值前置筛选: 便宜 {len(cheap_parts)} / 查询 {len(target_parts)} (threshold=¥{thresh:g})")

    if ctx.get("skip_market"):
        print(f"\n🚫 已勾选『跳过查询零件价格』→ 不进一车一件，全部 {len(parts)} 件以定损价直接提交")
        log_audit(f"用户选择跳过一车一件比价（skip_market=True）")
        for p in parts:
            p.market_price = None
            p.within_market = True
        rec.mark("STEP 5", "skipped", reason="用户勾选跳过零件比价")
        rec.mark("STEP 6", "skipped", reason="用户勾选跳过零件比价")
    elif not target_parts:
        print(f"\n  ✅ 没有零件超过阈值 ¥{thresh:g} → 不查一车一件，全部 {len(parts)} 件以定损价直接提交")
        log_audit(f"阈值前置筛选: 全部 {len(parts)} 件低于 ¥{thresh:g}，跳过一车一件")
        rec.mark("STEP 5", "skipped", reason=f"全部零件低于阈值 ¥{thresh:g}")
        rec.mark("STEP 6", "skipped", reason=f"全部零件低于阈值 ¥{thresh:g}")
    elif _market_ban_left(ctx) > 0:
        # 一车一件处于封禁冷却 → 不再访问（否则会延长封禁），本单全部以定损价提交
        _left = _market_ban_left(ctx)
        print(f"\n  ⏸ 一车一件封禁冷却中（剩 {_left/60:.1f} 分钟）→ 本单不查一车一件，"
              f"全部 {len(parts)} 件以定损价提交")
        log_audit(f"一车一件封禁冷却剩 {int(_left)}s → 本单跳过比价")
        for p in target_parts:
            p.market_price = None
            p.within_market = None
        ctx["skip_market_compare"] = True
        rec.mark("STEP 5", "skipped", reason="一车一件封禁冷却中")
        rec.mark("STEP 6", "skipped", reason="一车一件封禁冷却中")
    else:
        # ---------- 本地价格库：命中新鲜缓存就不查一车一件 ----------
        price_svc = get_price_service()
        remote_parts = target_parts
        cached_parts: list = []
        if price_svc.enabled:
            price_svc.touch_vin(vehicle.vin)
            remote_parts, cached_parts = price_svc.resolve(
                vehicle.vin, target_parts,
                tolerance=float(ctx.get("tolerance") or 0))
            if cached_parts:
                print(f"\n  💾 本地价格库命中 {len(cached_parts)}/{len(target_parts)} 件"
                      f"（有效期 {price_svc.valid_days} 天）→ 这些零件不再查一车一件")
                log_audit(f"本地价格库命中 {len(cached_parts)}/{len(target_parts)} 件，"
                          f"剩余 {len(remote_parts)} 件需远程查询")

        if not remote_parts:
            print(f"\n  ✅ 全部零件命中本地价格库 → 跳过 STEP 5/6，完全不访问一车一件")
            log_audit("全部零件命中本地价格库 → 跳过一车一件（含 VIN 查询）")
            rec.mark("STEP 5", "skipped", reason="全部零件命中本地价格库")
            rec.mark("STEP 6", "skipped", reason="全部零件命中本地价格库")
        else:
            providers = get_providers()
            primary = providers[0]

            # 标记本单真的访问了一车一件 → 案件间才需要冷却
            ctx["market_touched"] = True
            await env.market.go_home()

            async with rec.step("STEP 5", STEP_NAMES["STEP 5"]):
                await gate.checkpoint("STEP 5 一车一件 → VIN 查询")
                try:
                    await primary.query_vin(env.market_page, ctx)
                except Exception as exc:
                    if _is_market_ban(exc):
                        print("  🛑 STEP 5 触发一车一件反爬熔断 → 跳过比价，全部以定损价提交")
                        log_audit("STEP 5 反爬熔断 → 降级为定损价")
                        ctx["skip_market_compare"] = True
                    elif await _skill_try("STEP 5", env.market_page, str(exc), ctx):
                        await primary.query_vin(env.market_page, ctx)
                    else:
                        # 一车一件只是“比价”环节，出任何问题都不该让整个案件失败
                        print(f"  ⚠️ STEP 5 一车一件异常（{type(exc).__name__}）→ 跳过比价，全部以定损价提交")
                        log_audit(f"STEP 5 异常降级: {type(exc).__name__}: {exc}")
                        ctx["skip_market_compare"] = True

            # VIN 在一车一件查不到 / 触发反爬熔断 → 跳过整个比价环节，全部以定损价为准
            if ctx.get("skip_market_compare"):
                print(f"\n  ⚠️ 跳过一车一件比价 → 全部 {len(parts)} 件以定损价为准")
                for p in parts:
                    p.market_price = None
                    p.within_market = None
            else:
                async with rec.step("STEP 6", STEP_NAMES["STEP 6"]) as st:
                    await gate.checkpoint(
                        f"STEP 6 一车一件 → 逐个查询 {len(remote_parts)} 个贵零件市场价")
                    try:
                        await primary.query_parts(env.market_page, ctx, parts=remote_parts)
                    except Exception as exc:
                        if _is_market_ban(exc):
                            print("  🛑 STEP 6 触发一车一件反爬熔断 → 剩余零件以定损价提交")
                            log_audit("STEP 6 反爬熔断 → 剩余零件降级为定损价")
                        elif await _skill_try("STEP 6", env.market_page, str(exc), ctx):
                            await primary.query_parts(env.market_page, ctx, parts=remote_parts)
                        else:
                            print(f"  ⚠️ STEP 6 一车一件异常（{type(exc).__name__}）→ 剩余零件以定损价提交")
                            log_audit(f"STEP 6 异常降级: {type(exc).__name__}: {exc}")

                    # 主端口结果落库（只存有效价）
                    if price_svc.enabled:
                        n = price_svc.save(vehicle.vin, remote_parts, source=primary.name)
                        if n:
                            print(f"  💾 已写入本地价格库 {n} 条（来源：{primary.label}）")
                            log_audit(f"写入本地价格库 {n} 条（source={primary.name}）")

                    # 备用端口：主端口查不到的零件再试下一家（当前仅预留）
                    for extra in providers[1:]:
                        left = [p for p in remote_parts if p.market_price is None]
                        if not left:
                            break
                        before = {id(p): p.market_price for p in left}
                        try:
                            await extra.query_parts(env.market_page, ctx, parts=left)
                        except Exception as exc2:
                            print(f"  ⚠️ 备用端口 {extra.label} 查询异常"
                                  f"（{type(exc2).__name__}）→ 保持定损价")
                            log_audit(f"备用端口 {extra.label} 异常: "
                                      f"{type(exc2).__name__}: {exc2}")
                            continue
                        newly = [p for p in left
                                 if p.market_price is not None
                                 and before.get(id(p)) is None]
                        if newly:
                            price_svc.save(vehicle.vin, newly, source=extra.name)
                            print(f"  💾 备用端口 {extra.label} 补查 {len(newly)} 件并写入本地库")

                    result.market_queries_count = sum(
                        1 for p in remote_parts if p.market_price is not None)
                    st.detail["queries"] = result.market_queries_count
                    if cached_parts:
                        st.detail["cache_hits"] = len(cached_parts)

    over_parts = [p for p in parts if p.within_market is False]
    ok_parts = [p for p in parts if p.within_market is True]
    result.over_market_parts = over_parts

    # ==================== PART 3: 修复 + 提交 ====================
    if over_parts:
        print(f"\n📊 比对: 合规 {len(ok_parts)} 件 / 超市场价 {len(over_parts)} 件")
        print(f"   进入自动修复流程...")
        await loss_page.bring_to_front()
        async with rec.step("STEP 7", STEP_NAMES["STEP 7"]) as st:
            await gate.checkpoint("STEP 7 定损平台 → 自动修复超市场价 + 暂存")
            try:
                fixed = await step_loss_fix_over_market(loss_page, ctx)
                st.detail["fixed"] = fixed
            except Exception as exc:
                # 改价失败不能让案件被跳过：先试 skill hook，救不了再降级
                if await _skill_try("STEP 7", loss_page, str(exc), ctx):
                    try:
                        fixed = await step_loss_fix_over_market(loss_page, ctx)
                        st.detail["fixed"] = fixed
                    except Exception as exc2:
                        print(f"\n  ⚠️ STEP 7 自动修复异常（skill 救后仍失败）: "
                              f"{type(exc2).__name__}: {str(exc2).splitlines()[0][:160]}")
                        log_audit(f"STEP 7 自动修复异常，继续提交：{exc2}")
                        st.detail["fixed"] = 0
                        st.detail["fix_error"] = str(exc2).splitlines()[0][:200]
                        ctx["resync_loss"] = True
                else:
                    print(f"\n  ⚠️ STEP 7 自动修复异常（不中断案件）: "
                          f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
                    log_audit(f"STEP 7 自动修复异常，继续提交：{exc}")
                    st.detail["fixed"] = 0
                    st.detail["fix_error"] = str(exc).splitlines()[0][:200]
                    ctx["resync_loss"] = True
        for p in parts:
            if p.fixed_price is not None:
                p.local_price = p.fixed_price
                p.within_market = True
        ok_count = sum(1 for p in parts if p.within_market)
        print(f"\n✅ 修复后全部 {ok_count} 件合规，开始自动提交...")
    else:
        print(f"\n✅ 全部 {len(ok_parts)} 个零件本地价 ≤ 市场价，开始自动提交...")

    await loss_page.bring_to_front()
    if dry_run:
        result.status = "dry_run"
        rec.mark("STEP 8", "skipped", reason="演练模式")
        rec.mark("STEP 9", "skipped", reason="演练模式")
        print(f"\n  🛡 演练模式：跳过『提交定损』与『报价完成』（比价/修复步骤已实际执行）")
        return "dry_run"

    async with rec.step("STEP 8", STEP_NAMES["STEP 8"]):
        await gate.checkpoint("STEP 8 定损平台 → 提交定损单")
        try:
            await step_loss_submit(loss_page, ctx)
        except Exception as exc:
            # 提交定损单失败也要继续走报价提交，避免整个案件被跳过
            # 先试 skill hook 救一次
            if await _skill_try("STEP 8", loss_page, str(exc), ctx):
                try:
                    await step_loss_submit(loss_page, ctx)
                    exc = None  # 标记救成功
                except Exception as exc2:
                    exc = exc2
            if exc is not None:
                print(f"\n  ⚠️ STEP 8 提交定损单异常（不中断案件，继续报价提交）: "
                      f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
                log_audit(f"STEP 8 提交定损单异常，继续报价提交：{exc}")

    # ==================== STEP 9R：蓝色 / 纯橙色零件 → 业务动作『退回』 ====================
    # 前面 STEP 1~8 已正常完成；零件名称蓝色/纯橙色的案件不选『报价完成』，
    # 改为『退回』+ 填写意见 → 评价页勾选最后一项 → 二次提交任务。
    if ctx.get("return_part_color"):
        blue = ctx.get("return_blue_parts") or []
        orange = ctx.get("return_orange_parts") or []
        names = "、".join(str(p.get("name")) for p in (blue + orange)[:5])
        print(f"\n{'='*70}")
        print(f"  ↩️ STEP 1~8 已完成；零件名称蓝色/纯橙色 → 业务动作改『退回』")
        print(f"  报案号: {claim_no}")
        print(f"  零件:   {names}")
        print(f"{'='*70}")
        await quote_page.bring_to_front()
        async with rec.step("STEP 9", STEP_NAMES["STEP 9"]) as st:
            await gate.checkpoint("STEP 9 报价处理 → 业务动作『退回』→ 评分 → 提交任务")
            try:
                await step_quote_return_for_color_parts(quote_page, ctx)
            except Exception as exc:
                if await _skill_try("STEP 9", quote_page, str(exc), ctx):
                    await step_quote_return_for_color_parts(quote_page, ctx)
                else:
                    raise
            st.detail["route"] = "return_part_color"
        result.status = "done"
        result.messages.append(f"零件名称蓝色/纯橙色 → 已退回（{names}）")
        log_run(f"零件名称蓝色/纯橙色 → 退回并提交成功 | 报案号={claim_no} | {names}")
        return "return_part_color"

    # ==================== STEP 9 前跳过：其他颜色零件 → 不提交 ====================
    # 前面 STEP 1~8（同步定损 / 查价 / 改价 / 提交定损单）已正常完成，
    # 这里不再选业务动作『报价完成』，也不提交任务，直接跳过等人工处理。
    if ctx.get("skip_part_color"):
        bad = ctx.get("skip_part_color_parts") or []
        names = "、".join(str(p.get("name")) for p in bad[:5])
        print(f"\n{'='*70}")
        print(f"  ⏭ STEP 1~8 已完成；零件名称颜色需人工处理 → 不提交『报价完成』，跳过")
        print(f"  报案号: {claim_no}")
        print(f"  零件:   {names}")
        print(f"{'='*70}")
        result.status = "skipped"
        result.messages.append(
            f"零件名称颜色需人工处理（{names}）→ STEP 1~8 已完成，未提交报价完成")
        rec.mark("STEP 9", "skipped", reason="零件名称颜色需人工处理，未提交报价完成")
        log_run(f"零件名称颜色需人工处理 → STEP 9 前跳过 | 报案号={claim_no} | {names}")
        return "skip_part_color"

    await quote_page.bring_to_front()
    async with rec.step("STEP 9", STEP_NAMES["STEP 9"]) as st:
        await gate.checkpoint("STEP 9 报价处理 → 报价完成 → 提交任务")
        st.detail["route"] = await _finalize_with_fallback(quote_page, ctx, result, rec)

    result.status = "done"
    print(f"\n{'='*70}")
    print(f"  🎉 流程成功完成！")
    print(f"  报案号:   {claim_no}")
    print(f"  VIN:      {vehicle.vin}")
    print(f"  零件数:   {len(parts)}  全部合规 ✅")
    if over_parts:
        print(f"  修复:     自动修复 {len(over_parts)} 个超市场价零件 + 暂存")
    print(f"{'='*70}")
    return "normal"


def snapshot(ctx: dict, result: BotRunResult, route: str) -> dict:
    """记录里统一的「数据」部分（模拟 / 真实 / 人工 三种来源都用它，便于对比）"""
    parts = result.parts or []
    return {
        "route": route,
        "vin": result.vehicle.vin,
        "plate": result.vehicle.plate,
        "parts": _parts_snapshot(parts),
        "over_market": sum(1 for p in parts if p.within_market is False),
        "market_queries": result.market_queries_count,
        "status": result.status,
    }
