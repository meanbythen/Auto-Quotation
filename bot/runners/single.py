"""
单次运行器 —— 处理一个报案号（可用于连续执行 / 单步调试）

与循环挂机共用 bot/flows/claim_flow.run_claim_flow，
环境可以是模拟 / 真实 / 混合（门户 mock + 一车一件真实 等）。
"""
from __future__ import annotations

import traceback

from bot.core.records import Recorder, RunRecord
from bot.envs import EnvSpec, build_environment
from bot.flows import run_claim_flow, snapshot
from bot.logging_system import log_error, log_run, setup
from bot.models import BotRunResult
from bot.runtime import BotStopped, clear_stop
from bot.step_gate import StepGate, StepAborted

from .base import finalize_record, make_ctx, write_ledger


async def run_single(claim_no: str, spec: EnvSpec, *,
                     tolerance: float = 0.0,
                     dry_run: bool = False,
                     transfer_target: str = "",
                     headless: bool = False,
                     mode: str = "single",
                     skill_learning: bool = False,
                     skip_market: bool = False,
                     market_price_threshold: float = 0.0,
                     market_rate_limit: bool = True,
                     market_breaker: bool = True) -> BotRunResult:
    """dry_run=True 演练模式：跑到比价/修复为止，跳过提交定损与报价完成，且不写台账"""
    setup()
    clear_stop()
    log_run(
        f"开始处理 报案号={claim_no} | 模式={mode} | {spec.label} | "
        f"容忍度=¥{tolerance:g} | 移交目标={transfer_target}"
        + (" | 演练模式" if dry_run else "")
    )

    result = BotRunResult()
    result.vehicle.claim_no = claim_no
    result.status = "running"

    rec = Recorder(RunRecord(mode=mode, env_portal=spec.portal,
                             env_market=spec.market, claim_no=claim_no)).begin()
    route = ""

    print(f"\n{'='*70}")
    print(f"  🤖 理赔报价机器人 启动")
    print(f"  报案号:   {claim_no}")
    print(f"  环境:     {spec.label}")
    print(f"  容忍度:   ¥{tolerance:g}（超市场价在此金额内不算超）")
    print(f"  演练模式: {'开（跳过提交，不写台账）' if dry_run else '关'}")
    print(f"  本地价阈值: ¥{market_price_threshold:g}（低于此值跳过一车一件查询，设 0 = 全部查询）"
          if market_price_threshold > 0 else f"  本地价阈值: 0（全部查询一车一件）")
    print(f"{'='*70}")

    async with build_environment(spec, headless=headless) as env:
        ctx = make_ctx(claim_no, tolerance, transfer_target,
                       mode=mode, portal_url=env.portal.home_url,
                       skill_learning=skill_learning,
                       skip_market=skip_market,
                       market_price_threshold=market_price_threshold,
                       market_rate_limit=market_rate_limit,
                       market_breaker=market_breaker)
        try:
            print(f"\n📂 打开理赔门户首页: {env.portal.home_url}")
            await env.portal.go_home()
            await env.portal.ensure_ready()

            route = await run_claim_flow(env, ctx, result, dry_run=dry_run, rec=rec)
            if result.status == "running":
                result.status = "done"

        except StepAborted:
            result.status = "stopped"
            print(f"\n⏹️ 单步调试已被中止（{StepGate.instance().current_step}）")
            log_run(f"单步调试中止 @ {StepGate.instance().current_step}")
        except BotStopped:
            result.status = "stopped"
            print(f"\n⏹️ 已停止（{StepGate.instance().current_step}）")
            log_run(f"用户停止 @ {StepGate.instance().current_step}")
        except Exception as exc:
            result.status = "failed"
            step = (getattr(StepGate.instance(), "current_step", "") or "").strip() or "未知步骤"
            result.messages.append(f"卡在 {step}: {str(exc).splitlines()[0][:200]}")
            traceback.print_exc()
            print(f"\n❌ 流程失败（卡在 {step}）: {exc}")
            log_error(f"流程失败 报案号={claim_no} 卡在 {step}", exc)
        finally:
            log_run(
                f"结束 报案号={claim_no} 状态={result.status} "
                f"零件数={len(result.parts)} VIN={result.vehicle.vin}"
            )
            if dry_run:
                print(f"\n🛡 演练模式：跳过工作记录写入")
            else:
                write_ledger(claim_no, result.vehicle, result.parts, result.status,
                             note=result.messages[-1] if result.messages else "")
            finalize_record(rec, status=result.status, route=route,
                            note=result.messages[-1] if result.messages else "",
                            data=snapshot(ctx, result, route))

    return result
