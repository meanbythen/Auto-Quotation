"""
核心接口（Ports）—— 流程逻辑与「环境实现」之间的唯一契约

流程模块只依赖这里的 Protocol，不直接 import 浏览器、不发 HTTP、
不关心当前是模拟环境还是真实生产环境。

实现方：
  bot/envs/portal.py   MockPortal / RealPortal
  bot/envs/market.py   MockMarket / RealMarket

注意：用 Protocol 而不是 ABC —— 适配器不需要继承，只要形状一致即可，
方便以后接入新的环境（例如回放录像、第三方 RPA、HTTP API）。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PortalPort(Protocol):
    """理赔门户（报价 / 定损 / 任务中心）"""

    label: str                 # "mock" / "real" / "real-cdp"，用于日志与对比
    home_url: str              # 首页地址
    page: Any                  # 当前活动页（Playwright Page，流程只当它是「页面句柄」）
    pages: list                # 门户上下文里所有页面（人工测试需要观察 popup）

    async def open(self, pw: Any) -> None:
        """按环境配置启动/接管门户（pw = Playwright 实例）"""

    async def go_home(self) -> None:
        """回到门户首页并等待列表加载"""

    async def ensure_ready(self, timeout_s: int = 240) -> None:
        """等待环境可用（真实门户未登录时提示并等待人工登录）"""

    async def refresh(self) -> None:
        """强制刷新首页（业务系统列表不自动更新，必须刷新才能看到新任务）"""

    async def list_claims(self) -> list[str]:
        """读取首页所有可处理报案号"""

    async def close(self) -> None:
        """释放资源（真实环境可能故意保持打开）"""


@runtime_checkable
class MarketPort(Protocol):
    """一车一件（市场价查询）"""

    label: str
    home_url: str
    page: Any

    async def open(self, pw: Any) -> None:
        """启动/接管一车一件（真实外网首次需要人工登录一次）"""

    async def go_home(self) -> None:
        """回到 VIN 查询首页"""

    async def close(self) -> None:
        """释放资源"""
