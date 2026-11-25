"""运行期人工确认通道

后台 asyncio 线程调用 `ask()`；GUI 在启动时注册 handler，
由 handler 把对话框切到 Tk 主线程并阻塞等待用户选择。

未注册 handler（无界面运行）时直接返回 None，调用方按「跳过」处理。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

Handler = Callable[[dict, float], Optional[str]]

_handler: Optional[Handler] = None
_lock = threading.Lock()


def set_handler(fn: Optional[Handler]) -> None:
    global _handler
    with _lock:
        _handler = fn


def clear_handler() -> None:
    set_handler(None)


def available() -> bool:
    with _lock:
        return _handler is not None


def ask(payload: dict, timeout: float = 120.0) -> Optional[str]:
    """阻塞等待人工选择；返回动作名，跳过/超时/无界面返回 None"""
    with _lock:
        handler = _handler
    if handler is None:
        return None
    try:
        return handler(payload, timeout)
    except Exception:
        return None
