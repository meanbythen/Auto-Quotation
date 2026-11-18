"""
单步调试闸门 —— 让 run() 能像调试器一样一步一步执行

原理:
  run() 在每个 step 之间调用 await gate.checkpoint("STEP x ...")
  - gate 未启用 → 直接返回（连续执行，行为与原流程一致）
  - gate 已启用 → 阻塞等待 GUI 点「下一步」，或「停止」时抛出 StepAborted

用法:
  from bot.step_gate import StepGate, StepAborted
  gate = StepGate.instance()
  gate.enable()                      # GUI 线程
  await gate.checkpoint("STEP 3")    # bot 协程
"""
from __future__ import annotations
import asyncio
import threading
from typing import Callable, Optional


class StepAborted(Exception):
    """用户中止单步调试"""


class StepGate:
    _inst: Optional["StepGate"] = None

    @classmethod
    def instance(cls) -> "StepGate":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self.enabled = False
        self.current_step = ""
        self._next = threading.Event()
        self._abort = threading.Event()
        # GUI 回调（在 bot 协程线程里调用，GUI 自己负责切回主线程）
        self.on_wait: Optional[Callable[[str], None]] = None

    # ---------- 控制（GUI 线程调用） ----------
    def enable(self) -> None:
        self.enabled = True
        self._abort.clear()
        self._next.clear()

    def disable(self) -> None:
        self.enabled = False
        self._next.set()

    def next_step(self) -> None:
        self._next.set()

    def abort(self) -> None:
        self._abort.set()
        self._next.set()

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    # ---------- bot 协程调用 ----------
    async def checkpoint(self, label: str) -> None:
        """每个 STEP 前调用：写运行日志 + 检查停止；单步模式下暂停"""
        from bot.logging_system import log_run
        from bot.runtime import check_stop

        log_run(label)
        # 无论是否单步模式都记录当前步骤，便于异常时知道卡在哪一步
        self.current_step = label

        check_stop(label)

        if not self.enabled:
            return
        if self._abort.is_set():
            raise StepAborted(label)

        self.current_step = label
        self._next.clear()
        if self.on_wait:
            try:
                self.on_wait(label)
            except Exception:
                pass

        # 轮询等待：不阻塞事件循环，也便于响应「停止」
        while not self._next.is_set():
            if self._abort.is_set():
                raise StepAborted(label)
            check_stop(label)
            await asyncio.sleep(0.1)

        self._next.clear()
        if self._abort.is_set():
            raise StepAborted(label)
