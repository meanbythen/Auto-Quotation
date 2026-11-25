"""
循环挂机运行器 —— 不断「回首页 → 取号 → 跑同一套流程 → 回首页」

与单次运行共用 bot/flows/claim_flow.run_claim_flow。
卡住的案件：记录卡点 → 跳过 → 继续下一个（不中断挂机）。

生命周期拆分（v3.9.0 新增，支持"先拉起浏览器 → 你登录 → 再开始扫描 → 停止扫描不关浏览器 → 再启动 → 最后关浏览器"）：

  env = await open_loop_browser(spec)      # 拉起浏览器，两个门户已开
  await run_loop_scan(env, spec, pause_event=pause_ev)   # 开始扫描（可被暂停/继续）
  await close_loop_browser(env)            # 真正关浏览器

pause_event 用法：
  pause_event.set()     # 循环里每轮开始前检测到就 sleep（不退出循环）→ 等同于"停止扫描"
  pause_event.clear()   # 继续扫描
  外部 stop_requested() → 循环 break → 退出 run_loop_scan（之后 env 还开着）
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Optional

from bot.config import LOOP_POLL_INTERVAL
from bot.core.records import Recorder, RunRecord
from bot.envs import EnvSpec, build_environment
from bot.flows import run_claim_flow, snapshot
from bot.logging_system import log_error, log_run, setup
from bot.models import BotRunResult
from bot.pricing import get_price_service
from bot.runtime import BotStopped, check_stop, clear_stop, stop_requested
from bot.step_gate import StepGate

from .base import finalize_record, make_ctx, record_skip, skip_log_path, write_ledger


# ============================================================
# 三层新 API（GUI 双按钮用）
# ============================================================

async def open_loop_browser(spec: EnvSpec, *, headless: bool = False):
    """拉起浏览器，打开理赔门户 + 一车一件两个页面。返回已启动的 Environment。

    调用方拿到 env 后，用户可以手动登录两个系统的账号，再调用 run_loop_scan。
    """
    setup()
    env = build_environment(spec, headless=headless)
    await env.start()
    await env.portal_page.bring_to_front()
    try:
        await env.portal.go_home()
    except Exception as e:
        log_run(f"门户首页加载（首次）: {e}")
    try:
        await env.market_page.bring_to_front()
        await env.market.go_home()
    except Exception as e:
        log_run(f"一车一件首页加载（首次）: {e}")
    return env


async def run_loop_scan(env, spec: EnvSpec, *,
                        pause_event: Optional[asyncio.Event] = None,
                        tolerance: float = 0.0,
                        max_count: int = 999,
                        logger=None,
                        transfer_target: str = "",
                        mode: str = "loop",
                        poll_interval: float = LOOP_POLL_INTERVAL,
                        skill_learning: bool = False,
                        skip_market: bool = False,
                        market_price_threshold: float = 0.0,
                        market_rate_limit: bool = True,
                        market_breaker: bool = True) -> list:
    """对一个已经打开的 env 跑业务循环。

    pause_event 是可选暂停信号：set → 循环每轮 sleep；clear → 正常继续；
    stop_requested() → 循环 break（不会关掉 env，调用方还可以再调一次 run_loop_scan 继续）。
    """
    clear_stop()
    StepGate.instance().disable()
    poll_interval = max(1.0, float(poll_interval or LOOP_POLL_INTERVAL))

    log_run(f"循环挂机启动 | 模式={mode} | {spec.label} | 上限={max_count} | "
            f"刷新间隔={poll_interval:g}s | 移交目标={transfer_target}"
            + (" | 跳过零件比价" if skip_market else ""))

    await env.portal.go_home()
    try:
        await env.portal.ensure_ready()
    except Exception:
        pass

    print(f"\n{'='*70}")
    print(f"  🔄 循环挂机模式启动")
    print(f"  环境: {spec.label}")
    print(f"  刷新间隔: {poll_interval:g} 秒（没有新案件也持续刷新等待，手动停止才结束）")
    if skip_market:
        print(f"  🚫 跳过一车一件（零件比价），全部以定损价直接提交")
    else:
        print(f"  🍃 反爬开关: 速率限流={'开' if market_rate_limit else '关'} | "
              f"封禁熔断={'开' if market_breaker else '关'} | "
              f"本地价阈值={'¥%g' % market_price_threshold if market_price_threshold > 0 else '不限'}")
    print(f"{'='*70}")

    return await _loop_body(env, spec, pause_event=pause_event,
                            tolerance=tolerance, max_count=max_count,
                            logger=logger, transfer_target=transfer_target,
                            mode=mode, poll_interval=poll_interval,
                            skill_learning=skill_learning,
                            skip_market=skip_market,
                            market_price_threshold=market_price_threshold,
                            market_rate_limit=market_rate_limit,
                            market_breaker=market_breaker)


async def close_loop_browser(env) -> None:
    """关掉浏览器（真正调 Environment.stop）。幂等：已经关过就跳过。"""
    await env.stop()


# ============================================================
# 老入口：run_loop 保持签名不变，内部组合三层新 API（兼容旧调用点）
# ============================================================
async def run_loop(claim_no: str, spec: EnvSpec, *,
                   tolerance: float = 0.0,
                   max_count: int = 999,
                   logger=None,
                   transfer_target: str = "",
                   headless: bool = False,
                   mode: str = "loop",
                   poll_interval: float = LOOP_POLL_INTERVAL,
                   skill_learning: bool = False,
                   skip_market: bool = False,
                   market_price_threshold: float = 0.0,
                   market_rate_limit: bool = True,
                   market_breaker: bool = True) -> list:
    """claim_no 仅作占位/日志用途（循环模式从首页自动取号）"""
    env = None
    try:
        env = await open_loop_browser(spec, headless=headless)
        return await run_loop_scan(env, spec,
                                   tolerance=tolerance, max_count=max_count,
                                   logger=logger, transfer_target=transfer_target,
                                   mode=mode, poll_interval=poll_interval,
                                   skill_learning=skill_learning,
                                   skip_market=skip_market,
                                   market_price_threshold=market_price_threshold,
                                   market_rate_limit=market_rate_limit,
                                   market_breaker=market_breaker)
    finally:
        if env is not None:
            await close_loop_browser(env)


# ============================================================
# 循环体主体（从原 run_loop 拆出来）
# ============================================================
async def _loop_body(env, spec: EnvSpec, *,
                     pause_event: Optional[asyncio.Event],
                     tolerance: float, max_count: int,
                     logger, transfer_target: str,
                     mode: str, poll_interval: float,
                     skill_learning: bool,
                     skip_market: bool = False,
                     market_price_threshold: float = 0.0,
                     market_rate_limit: bool = True,
                     market_breaker: bool = True) -> list:
    all_results: list = []
    processed_set: set = set()          # 已连续处理到上限的报案号（离开列表后自动解除）
    processed_counts: dict = {}         # 报案号 -> 连续处理次数
    MAX_CLAIM_PASSES = 2               # 同一报案号连续出现时最多完整处理 2 次，防止平台异常导致死循环
    failed_attempts: dict = {}
    MAX_OPEN_RETRY = 3
    processed_count = 0
    skipped_count = 0
    skipped_list: list[tuple] = []

    def _mark_processed(claim_no: str) -> None:
        """记一次处理；未达上限的报案号留在列表里仍会被再次完整处理"""
        processed_counts[claim_no] = processed_counts.get(claim_no, 0) + 1
        if processed_counts[claim_no] >= MAX_CLAIM_PASSES:
            processed_set.add(claim_no)

    def _forget_absent_claims(pending_claims) -> None:
        """报案号离开列表后清空计数：以后再次出现时按新任务重新完整处理"""
        present = set(pending_claims)
        for done in [c for c in processed_counts if c not in present]:
            processed_counts.pop(done, None)
            processed_set.discard(done)

    iteration = 0
    while processed_count < max_count:
        # ========== 暂停检测 ==========
        if pause_event is not None and pause_event.is_set():
            if stop_requested():
                break   # 停止优先级最高，即使在暂停也立刻 break
            # 暂停中每秒轮询：pause_event 被 clear() 了 → 继续；stop_requested → 退出
            await asyncio.sleep(1)
            continue

        if stop_requested():
            print(f"\n⏹️  用户停止挂机")
            log_run("用户停止挂机")
            break
        check_stop("挂机循环")

        iteration += 1
        print(f"\n{'─'*70}")
        print(f"  🔁 第 {iteration} 轮扫描")
        print(f"{'─'*70}")
        log_run(f"第 {iteration} 轮扫描")

        try:
            await env.portal.go_home()
        except Exception as e:
            print(f"  ⚠️ 回到首页失败，重试: {e}")
            await asyncio.sleep(3)
            continue

        pending = await env.portal.list_claims()
        _forget_absent_claims(pending)
        new_claims = [c for c in pending if c not in processed_set]

        if not new_claims:
            print(f"\n  🔄 当前列表没有新任务，刷新页面确认...")
            log_run("列表无新任务 → 刷新确认")
            pending = await _refresh_claims(env)
            _forget_absent_claims(pending)
            new_claims = [c for c in pending if c not in processed_set]

            if new_claims:
                claim = new_claims[0]
            elif pending:
                print(f"\n  ℹ️ 刷新后列表里的报案号都处理过了，{poll_interval:g} 秒后再刷新等新任务...")
                log_run(f"列表案件均已处理 → {poll_interval:g}s 后继续轮询")
                if await _wait_poll(poll_interval, pause_event):
                    print(f"\n⏹️  用户停止挂机（轮询等待中）")
                    log_run("用户停止挂机（轮询等待中）")
                    break
                continue
            else:
                print(f"\n  ⏳ 暂无待处理报案号，{poll_interval:g} 秒后重新刷新等待新案件"
                      f"（点『停止』才结束挂机）")
                log_run("列表无新任务 → 持续轮询等待新案件")
                if await _wait_poll(poll_interval, pause_event):
                    print(f"\n⏹️  用户停止挂机（轮询等待中）")
                    log_run("用户停止挂机（轮询等待中）")
                    break
                continue
        else:
            claim = new_claims[0]

        print(f"\n  🎯 处理 #{processed_count + 1}: {claim}")
        log_run(f"处理 #{processed_count + 1} 报案号={claim}")

        result = BotRunResult()
        result.vehicle.claim_no = claim
        rec = Recorder(RunRecord(mode=mode, env_portal=spec.portal,
                                 env_market=spec.market, claim_no=claim)).begin()
        ctx = make_ctx(claim, tolerance, transfer_target,
                       mode=mode, portal_url=env.portal.home_url,
                       skill_learning=skill_learning,
                       skip_market=skip_market,
                       market_price_threshold=market_price_threshold,
                       market_rate_limit=market_rate_limit,
                       market_breaker=market_breaker)
        # 同一报案号再次从列表里扫出来 → 仍走完整流程：
        # 已改过报价的零件由 STEP 7 按『报案号+零件』自动跳过，其余零件照常查询/改价。
        pass_index = processed_counts.get(claim, 0) + 1
        if pass_index > 1:
            print(f"\n  🔁 {claim} 第 {pass_index} 次出现 → 本轮仍执行完整流程"
                  f"（已改价零件跳过，其余零件照常查询）")
            log_run(f"重复出现 → 完整流程 | 报案号={claim} | 第 {pass_index} 次")
        route = ""

        try:
            route = await run_claim_flow(env, ctx, result, rec=rec)
            try:
                get_price_service().mark_claim_processed(claim)
            except Exception:
                pass
            if result.status == "running":
                result.status = "done"

            if route == "skip_part_color":
                processed_count += 1
                _mark_processed(claim)
                all_results.append(result)
                result.status = "skipped"
                skipped_count += 1
                note = result.messages[-1] if result.messages else "零件名称颜色需人工处理"
                skipped_list.append((claim, "STEP 9", "零件名称颜色需人工处理"))
                write_ledger(claim, result.vehicle, result.parts,
                             status="skipped", note=note, skip_step="STEP 9")
                finalize_record(rec, status="skipped", route=route,
                                data=snapshot(ctx, result, route))
                record_skip(claim, "STEP 9", "零件名称颜色需人工处理，STEP 1~8 已完成，未提交报价完成")
                print(f"\n  ⏭ #{processed_count} {claim} 零件名称颜色需人工处理 "
                      f"→ STEP 1~8 已完成，STEP 9 前跳过，继续下一个")
                log_run(f"#{processed_count} {claim} 零件名称颜色需人工处理 → STEP 9 前跳过")
                if logger:
                    try:
                        logger(f"⏭ #{processed_count} {claim} 零件名称颜色需人工处理 → STEP 9 前跳过")
                    except Exception:
                        pass
                await asyncio.sleep(2)
            elif route == "return_part_color":
                processed_count += 1
                _mark_processed(claim)
                all_results.append(result)
                note = (result.messages[-1] if result.messages
                        else "零件名称蓝色/纯橙色 → 已退回")
                write_ledger(claim, result.vehicle, result.parts,
                             status="done", note=note)
                finalize_record(rec, status="done", route=route,
                                data=snapshot(ctx, result, route))
                print(f"\n  ↩️ #{processed_count} {claim} 零件名称蓝色/纯橙色 → 已退回")
                log_run(f"#{processed_count} {claim} 零件名称蓝色/纯橙色 → 退回成功")
                if logger:
                    try:
                        logger(f"↩️ #{processed_count} {claim} 零件名称蓝色/纯橙色 → 已退回")
                    except Exception:
                        pass
                await asyncio.sleep(2)
            elif route == "skip_direct_supply":
                processed_count += 1
                _mark_processed(claim)
                all_results.append(result)
                result.status = "skipped"
                skipped_count += 1
                skipped_list.append((claim, "STEP 4", "直供价有值"))
                write_ledger(claim, result.vehicle, result.parts,
                             status="skipped", note="直供价有值 → 跳过该报案",
                             skip_step="STEP 4")
                finalize_record(rec, status="skipped", route=route,
                                data=snapshot(ctx, result, route))
                record_skip(claim, "STEP 4", "直供价有值，跳过")
                print(f"\n  ⏭ #{processed_count} {claim} 直供价有值 → 跳过，继续下一个")
                log_run(f"#{processed_count} {claim} 直供价有值 → 跳过")
                if logger:
                    try:
                        logger(f"⏭ #{processed_count} {claim} 直供价有值 → 跳过")
                    except Exception:
                        pass
                await asyncio.sleep(2)
            elif route == "skip_by_opinion":
                processed_count += 1
                _mark_processed(claim)
                all_results.append(result)
                result.status = "skipped"
                skipped_count += 1
                note = (ctx.get("opinion_advice_note")
                        or "意见判断：等待资料 / 无需处理")
                skipped_list.append((claim, "STEP 1.5", note))
                write_ledger(claim, result.vehicle, result.parts,
                             status="skipped", note=f"{note} → 按意见跳过该报案",
                             skip_step="STEP 1.5")
                finalize_record(rec, status="skipped", route=route,
                                data=snapshot(ctx, result, route))
                record_skip(claim, "STEP 1.5", note)
                print(f"\n  ⏭ #{processed_count} {claim} 按意见跳过：{note}")
                log_run(f"#{processed_count} {claim} 按意见跳过 | {note}")
                if logger:
                    try:
                        logger(f"⏭ #{processed_count} {claim} 按意见跳过：{note}")
                    except Exception:
                        pass
                await asyncio.sleep(2)
            else:
                processed_count += 1
                _mark_processed(claim)
                all_results.append(result)

                write_ledger(claim, result.vehicle, result.parts,
                             status="done",
                             note=(ctx.get("opinion_advice_note")
                                   or ("无换件案件" if route == "no_parts" else "")))
                finalize_record(rec, status="done", route=route,
                                data=snapshot(ctx, result, route))

                print(f"\n  ✅ #{processed_count} {claim} 处理完成")
                log_run(f"#{processed_count} {claim} 处理完成 | 零件={len(result.parts)} | "
                        f"VIN={result.vehicle.vin} | 走向={route}")
                if logger:
                    try:
                        logger(f"✅ #{processed_count} {claim} 处理完成")
                    except Exception:
                        pass

        except (BotStopped, asyncio.CancelledError):
            result.status = "stopped"
            finalize_record(rec, status="stopped", data=snapshot(ctx, result, route))
            print(f"\n⏹️  用户停止挂机，已中断当前案件 {claim}")
            log_run(f"用户停止挂机 @ {claim} | {StepGate.instance().current_step}")
            await _close_popup(ctx, env)
            break

        except Exception as exc:
            step = (getattr(StepGate.instance(), "current_step", "") or "").strip() or "未知步骤"
            reason = str(exc).splitlines()[0][:200]
            print(f"\n  ⏭  {claim} 在当前流程处理不了，跳过")
            print(f"      卡在: {step}")
            print(f"      原因: {reason}")
            traceback.print_exc()
            log_error(f"处理 {claim} 失败，已跳过（卡在 {step}）", exc)
            record_skip(claim, step, reason)

            result.status = "skipped"
            result.messages.append(f"卡在 {step}: {reason}")
            finalize_record(rec, status="skipped", note=result.messages[-1],
                            data=snapshot(ctx, result, route))

            transient = step.startswith(("STEP 1", "STEP 2"))
            attempt = failed_attempts.get(claim, 0) + 1
            failed_attempts[claim] = attempt
            if transient and attempt < MAX_OPEN_RETRY:
                print(f"\n  ↻ {claim} 第 {attempt} 次没能打开报价页 → 下一轮重试"
                      f"（最多 {MAX_OPEN_RETRY} 次）")
                log_run(f"{claim} 打不开报价页（第 {attempt} 次）→ 下轮重试")
                await _close_popup(ctx, env)
                await asyncio.sleep(2)
                continue

            # 高额案件已提交上级，后续步骤失败时先写工作记录再重试；
            # high_amount_transferred 只代表提交上级成功，不能据此宣称平级移交已成功。
            if ctx.get("high_amount_transferred") and attempt < MAX_OPEN_RETRY:
                write_ledger(claim, result.vehicle, result.parts, status="skipped",
                             note=f"高额分支重试：卡在 {step}：{reason}", skip_step=step)
                print(f"\n  ↻ {claim} 已提交上级，但卡在 {step} → 下一轮按新任务重新处理"
                      f"（第 {attempt} 次，最多 {MAX_OPEN_RETRY} 次）")
                log_run(f"{claim} 高额分支未完成（卡在 {step}：{reason}）→ "
                        f"下轮按新任务重试（第 {attempt} 次）")
                await _close_popup(ctx, env)
                await asyncio.sleep(2)
                continue

            write_ledger(claim, result.vehicle, result.parts, status="skipped",
                         note=f"卡在 {step}：{reason}", skip_step=step)

            all_results.append(result)
            _mark_processed(claim)
            processed_count += 1
            skipped_count += 1
            skipped_list.append((claim, step, reason))
            await _close_popup(ctx, env)

            print(f"\n  ⏭ #{processed_count} {claim} 已跳过（{step}），继续下一个")
            log_run(f"#{processed_count} {claim} 已跳过 | 卡在 {step}")
            if logger:
                try:
                    logger(f"⏭ #{processed_count} {claim} 跳过（{step}）")
                except Exception:
                    pass
            await asyncio.sleep(2)

        await _close_popup(ctx, env)

        # 🍃 一车一件反爬冷却：案件与案件之间给个整体停顿（受控制台限流开关控制）
        # 本单没访问一车一件（全部零件低于阈值 / 勾了跳过比价）就不需要冷却，直接下一单
        try:
            from bot.config import USE_MOCK_MARKET, MARKET_CLAIM_COOLDOWN
            if not ctx.get("market_touched", False):
                print("  ⚡ 本单未访问一车一件 → 免冷却，直接处理下一个报案号")
            elif (not USE_MOCK_MARKET and MARKET_CLAIM_COOLDOWN > 0
                    and bool(ctx.get("market_rate_limit", True))):
                # 加随机抖动 ±30%，精确秒数容易被识别为机器行为
                import random as _r
                cd = MARKET_CLAIM_COOLDOWN * _r.uniform(0.7, 1.3)
                print(f"  🕒 一车一件冷却 {cd:.1f}s 后处理下一个报案号")
                await asyncio.sleep(cd)
        except Exception:
            pass

        await asyncio.sleep(1.5)

    print(f"\n{'='*70}")
    print(f"  🏁 挂机结束")
    print(f"  共处理 {processed_count} 个报案号（成功 {processed_count - skipped_count} / 跳过 {skipped_count}）")
    print(f"  跳过详情（最近 {min(len(skipped_list), 20)} 条）:")
    for sclaim, sstep, sreason in skipped_list[-20:]:
        print(f"    - {sclaim}  卡在 {sstep}: {sreason[:80]}")
    print(f"{'='*70}")
    log_run(f"挂机结束 | 处理 {processed_count} | 成功 {processed_count - skipped_count} | 跳过 {skipped_count}")
    return all_results


# ============================================================
# 辅助函数（原 loop.py 私有，原样保留）
# ============================================================
async def _wait_poll(poll_interval: float, pause_event: Optional[asyncio.Event]) -> bool:
    """轮询等待。返回 True = 期间被 stop_requested 了 → 调用方 break。"""
    end = asyncio.get_event_loop().time() + poll_interval
    while asyncio.get_event_loop().time() < end:
        if stop_requested():
            return True
        # 暂停检测：如果 pause_event set 了，就更慢地等，让 GUI 有机会切回"开始扫描"
        if pause_event is not None and pause_event.is_set():
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(min(1.0, poll_interval))
    return False


async def _refresh_claims(env) -> list:
    """回到首页 → 刷新列表 → 返回新列表（用于确认是否真的没新任务）"""
    try:
        await env.portal_page.bring_to_front()
        await env.portal.go_home()
        await asyncio.sleep(1.5)
        return await env.portal.list_claims()
    except Exception:
        log_run("刷新列表时异常，跳过确认")
        return []


async def _close_popup(ctx: dict, env) -> None:
    """关掉可能堆积的弹窗。失败不影响流程"""
    try:
        from bot.page_objects import dismiss_all_modals
        await dismiss_all_modals(env.portal_page)
    except Exception:
        pass
