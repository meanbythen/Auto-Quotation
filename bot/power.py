"""Windows 防休眠 / 防熄屏 / 防屏保

程序运行时调用 keep_awake()，退出前调用 allow_sleep()。

原理：SetThreadExecutionState 给系统一个「有程序正在使用」的信号
  - ES_CONTINUOUS      持续生效（直到再次调用清除）
  - ES_SYSTEM_REQUIRED 阻止系统进入睡眠
  - ES_DISPLAY_REQUIRED 阻止显示器关闭 / 屏保启动

注意：该 API 无法绕过组策略强制锁屏，也无法阻止手动 Win+L。
"""
from __future__ import annotations

import sys
import threading

# SetThreadExecutionState 的标志位
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_ES_KEEP_AWAKE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

_lock = threading.Lock()
_active = False


def _apply(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        # restype 必须设成 DWORD，否则 64 位下返回值会被截断
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState.restype = ctypes.c_ulong
        kernel32.SetThreadExecutionState.argtypes = [ctypes.c_ulong]
        return bool(kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def keep_awake() -> bool:
    """阻止休眠 / 熄屏 / 屏保。返回是否成功（非 Windows 返回 False）"""
    global _active
    with _lock:
        if _active:
            return True
        ok = _apply(_ES_KEEP_AWAKE)
        _active = ok
        if ok:
            print("[i] 已开启防休眠/防熄屏（屏幕不会自动关闭，也不会进屏保）")
        else:
            print("[!] 防休眠设置失败（非 Windows 或系统拒绝），屏幕仍可能自动关闭")
        return ok


def allow_sleep() -> None:
    """恢复正常电源策略（程序退出前调用）"""
    global _active
    with _lock:
        if not _active:
            return
        _apply(ES_CONTINUOUS)
        _active = False
        print("[i] 已恢复正常休眠策略")


def is_active() -> bool:
    return _active


class Awake:
    """上下文管理器：with Awake(): ... 结束时自动恢复"""

    def __enter__(self) -> "Awake":
        keep_awake()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        allow_sleep()
        return False
