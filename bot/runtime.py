"""
运行时控制 —— 全局「停止」开关

GUI 的「■ 停止」按钮 → request_stop()（主线程）
机器人协程在每个 STEP 前 → check_stop()（发现已请求停止则抛 BotStopped）

这样的好处：
  - 停止对「单步调试」「连续执行」「循环挂机」三种模式都生效
  - 不用去改 asyncio 任务（跨线程 cancel 很容易留下半截页面状态）
"""
from __future__ import annotations
import threading


class BotStopped(Exception):
    """用户请求停止（区别于异常失败）"""


_STOP = threading.Event()


def request_stop() -> None:
    _STOP.set()


def clear_stop() -> None:
    _STOP.clear()


def stop_requested() -> bool:
    return _STOP.is_set()


def check_stop(label: str = "") -> None:
    """同步检查：已请求停止则立即抛出"""
    if _STOP.is_set():
        raise BotStopped(label or "用户停止")
