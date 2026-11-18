"""
理赔报价机器人 —— 入口层（薄封装）

真正的分层结构：
  bot/core   接口契约（PortalPort / MarketPort）+ 运行记录与对比
  bot/envs   环境适配器：模拟环境 / 真实生产环境（门户 + 一车一件）
  bot/flows  唯一流程处理逻辑（STEP 1 → STEP 9，含无换件分支与超权限补救）
  bot/runners 运行方式：单次 / 循环挂机 / 人工测试（都调用同一个 flow）

本文件只保留「命令行入口」和向后兼容的 run() / run_loop()，
GUI 直接把环境规格 + 运行模式传给 runners。

运行:
  python run_bot.py                          # 全部 mock（开发测试）
  python run_bot.py --market-real            # 一车一件走真实外网
  python run_bot.py --claim RDZA...502491    # 指定报案号
  python run_bot.py --loop                   # 循环挂机（mock 环境）
  python run_bot.py --loop --market-real     # 循环挂机（真实环境）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import (
    USE_MOCK_PORTAL, USE_MOCK_MARKET,
    PRICE_TOLERANCE_AMOUNT, TRANSFER_TARGET_DEFAULT, LOOP_POLL_INTERVAL,
)
from bot.envs import EnvSpec
from bot.models import BotRunResult
from bot.runners import run_loop as _run_loop, run_manual as _run_manual, run_single as _run_single
from bot.runners import record_skip, skip_log_path  # 兼容旧引用
from bot.logging_system import setup, log_system, environment_summary
import bot.version as _version
import bot.config as _cfg


# 默认测试报案号（GUI 单步测试 / 循环挂机占位用）
DEFAULT_CLAIM_NO = "RDZA2026110000501913"


def _spec(use_mock_portal: bool, use_mock_market: bool) -> EnvSpec:
    return EnvSpec.from_flags(use_mock_portal, use_mock_market)


async def run(
    claim_no: str = DEFAULT_CLAIM_NO,
    use_mock_market: bool = USE_MOCK_MARKET,
    use_mock_portal: bool = USE_MOCK_PORTAL,
    headless: bool = False,
    tolerance: float = PRICE_TOLERANCE_AMOUNT,
    dry_run: bool = False,
    transfer_target: str = TRANSFER_TARGET_DEFAULT,
    mode: str = "single",
) -> BotRunResult:
    """单次处理一个报案号（dry_run=True 演练模式：不提交、不写台账）"""
    return await _run_single(
        claim_no, _spec(use_mock_portal, use_mock_market),
        tolerance=tolerance, dry_run=dry_run, transfer_target=transfer_target,
        headless=headless, mode=mode,
    )


async def run_loop(
    claim_no: str = DEFAULT_CLAIM_NO,
    use_mock_market: bool = USE_MOCK_MARKET,
    use_mock_portal: bool = USE_MOCK_PORTAL,
    headless: bool = False,
    tolerance: float = PRICE_TOLERANCE_AMOUNT,
    max_count: int = 999,
    logger=None,
    transfer_target: str = TRANSFER_TARGET_DEFAULT,
    mode: str = "loop",
    poll_interval: float = LOOP_POLL_INTERVAL,
) -> list:
    """循环挂机：不断回首页取号 → 处理 → 回首页"""
    return await _run_loop(
        claim_no, _spec(use_mock_portal, use_mock_market),
        tolerance=tolerance, max_count=max_count, logger=logger,
        transfer_target=transfer_target, headless=headless, mode=mode,
        poll_interval=poll_interval,
    )


async def run_manual(
    claim_no: str = DEFAULT_CLAIM_NO,
    use_mock_market: bool = USE_MOCK_MARKET,
    use_mock_portal: bool = USE_MOCK_PORTAL,
    timeout_s: int = 900,
    transfer_target: str = TRANSFER_TARGET_DEFAULT,
):
    """人工测试：人工在浏览器操作，程序逐步校验并生成对比记录"""
    return await _run_manual(
        claim_no, _spec(use_mock_portal, use_mock_market),
        timeout_s=timeout_s, transfer_target=transfer_target,
    )


def main():
    setup()
    log_system(f"{_version.APP_NAME} {_version.version_text()} 启动（命令行）")
    log_system(environment_summary())
    for line in _version.recent_changes(3):
        log_system("最近变更 " + line)

    parser = argparse.ArgumentParser(description="理赔报价机器人")
    parser.add_argument("--claim",        default=DEFAULT_CLAIM_NO, help="报案号（单处理模式）")
    parser.add_argument("--headless",     action="store_true",      help="无头模式")
    parser.add_argument("--market-real",  action="store_true",      help="一车一件走真实外网")
    parser.add_argument("--portal-real",  action="store_true",      help="理赔门户走真实内网")
    parser.add_argument("--cdp",          default="",               help="复用已打开浏览器，如 http://127.0.0.1:9222")
    parser.add_argument("--tolerance",    type=float, default=PRICE_TOLERANCE_AMOUNT, help="价格容忍度（绝对金额，如 2 = 超出市场价 2 元以内不算超）")
    parser.add_argument("--dry-run",      action="store_true",      help="演练模式：不提交、不写台账")
    parser.add_argument("--loop",         action="store_true",      help="循环挂机：自动处理所有未处理报案号")
    parser.add_argument("--manual",       action="store_true",      help="人工测试：人工操作，程序逐步校验")
    parser.add_argument("--max",          type=int, default=999,    help="循环模式最多处理几个")
    parser.add_argument("--poll-interval", type=float, default=LOOP_POLL_INTERVAL, help="循环挂机没有新案件时的刷新间隔（秒），会一直等到有新案件或手动停止")
    args = parser.parse_args()

    if args.cdp:
        _cfg.PORTAL_CDP_URL = args.cdp
    use_mock_portal = not args.portal_real
    use_mock_market = not args.market_real

    # 挂机/单次运行期间不让系统休眠、熄屏、进屏保
    from bot.power import keep_awake, allow_sleep
    keep_awake()
    try:
        if args.manual:
            asyncio.run(run_manual(args.claim,
                                   use_mock_market=use_mock_market,
                                   use_mock_portal=use_mock_portal))
        elif args.loop:
            asyncio.run(run_loop(
                claim_no=args.claim,
                use_mock_market=use_mock_market,
                use_mock_portal=use_mock_portal,
                headless=args.headless,
                tolerance=args.tolerance,
                max_count=args.max,
                poll_interval=args.poll_interval,
            ))
        else:
            asyncio.run(run(
                claim_no=args.claim,
                use_mock_market=use_mock_market,
                use_mock_portal=use_mock_portal,
                headless=args.headless,
                tolerance=args.tolerance,
                dry_run=args.dry_run,
            ))
    finally:
        allow_sleep()


if __name__ == "__main__":
    main()
