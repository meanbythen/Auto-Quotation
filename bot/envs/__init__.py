"""
环境装配 —— 把「理赔门户 + 一车一件」两套适配器组装成一个可用的运行环境

用法:
    env = build_environment(EnvSpec(portal="mock", market="mock"))
    async with env:
        await env.portal_page.goto(...)
"""
from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import async_playwright

import bot.config as cfg
from bot.serve_mock import ensure_mock_server

from .market import MockMarket, RealMarket
from .portal import MockPortal, RealPortal


@dataclass(frozen=True)
class EnvSpec:
    portal: str = "mock"      # mock / real
    market: str = "mock"      # mock / real

    @classmethod
    def mock(cls) -> "EnvSpec":
        return cls("mock", "mock")

    @classmethod
    def real(cls) -> "EnvSpec":
        return cls("real", "real")

    @classmethod
    def from_flags(cls, use_mock_portal: bool, use_mock_market: bool) -> "EnvSpec":
        return cls("mock" if use_mock_portal else "real",
                   "mock" if use_mock_market else "real")

    @property
    def label(self) -> str:
        return f"门户={self.portal} 一车一件={self.market}"

    @property
    def uses_mock(self) -> bool:
        return self.portal == "mock" or self.market == "mock"


def mock_urls() -> tuple[str, str]:
    base = f"http://{cfg.MOCK_SERVER_HOST}:{cfg.MOCK_SERVER_PORT}"
    return base + "/index.html", base + "/market_home.html"


def real_portal_url() -> str:
    return (getattr(cfg, "PORTAL_REAL_URL", "") or "").strip() or "http://10.164.128.92/claimOper/"


def build_environment(spec: EnvSpec, *, headless: bool = False,
                      portal_url: str = "", market_url: str = "") -> "Environment":
    """按环境规格创建适配器（不做任何启动动作，交给 async with）"""
    # page_objects 的市场 selector 运行时按这个开关切换
    cfg.USE_MOCK_PORTAL = (spec.portal == "mock")
    cfg.USE_MOCK_MARKET = (spec.market == "mock")

    mock_portal_url, mock_market_url = mock_urls()
    p_url = portal_url or (mock_portal_url if spec.portal == "mock" else real_portal_url())
    m_url = market_url or (mock_market_url if spec.market == "mock" else cfg.MARKET_REAL_URL)

    if spec.portal == "mock":
        portal = MockPortal(p_url, headless=headless)
    else:
        portal = RealPortal(p_url,
                            cdp_url=getattr(cfg, "PORTAL_CDP_URL", "") or "",
                            headless=False)

    if spec.market == "mock":
        market = MockMarket(m_url, headless=headless)
    else:
        # 一车一件同样支持接管已打开的浏览器：优先独立配置，否则复用门户的 CDP
        market = RealMarket(
            m_url, headless=False,
            cdp_url=(getattr(cfg, "MARKET_CDP_URL", "")
                     or getattr(cfg, "PORTAL_CDP_URL", "") or ""))

    return Environment(portal, market, spec)


class Environment:
    """一次运行的环境会话（持有 Playwright 生命周期）

    生命周期有两种用法：
      1) async with env:     # 老代码推荐，退出时自动关浏览器
      2) 手动:
           await env.start()
           ...跑业务循环（可能暂停/继续多次）...
           await env.stop()   # 真正关浏览器
    """

    def __init__(self, portal, market, spec: EnvSpec | None = None):
        self.portal = portal
        self.market = market
        self.spec = spec or EnvSpec(portal.label, market.label)
        self._pw = None
        self.started = False   # 是否已拉起浏览器
        self.stopped = False   # 是否已关浏览器（防止重复 stop）

    # ---------- 页面句柄（流程只认这两个） ----------
    @property
    def portal_page(self):
        return self.portal.page

    @property
    def market_page(self):
        return self.market.page

    @property
    def portal_pages(self) -> list:
        return self.portal.pages

    # ---------- 手动生命周期 ----------
    async def start(self) -> None:
        """拉起 Playwright + 打开两个门户页面。幂等（已拉起就跳过）。"""
        if self.started and self._pw is not None:
            return
        if self.spec.uses_mock:
            ensure_mock_server()
        self._pw = await async_playwright().start()
        try:
            await self.market.open(self._pw)
            if self.spec.market == "real":
                # 网络层反爬监听：真实的 HTTP 429 才是最准的限流信号
                try:
                    from bot.page_objects import (install_market_ban_listener,
                                                  market_clear_http_ban)
                    market_clear_http_ban()
                    install_market_ban_listener(self.market.page)
                except Exception:
                    pass
            await self.portal.open(self._pw)
            self.started = True
            self.stopped = False
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """关闭门户 + stop Playwright。幂等。"""
        if self.stopped:
            return
        for target in (self.market, self.portal):
            try:
                await target.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self.started = False
        self.stopped = True

    # ---------- async with 兼容层 ----------
    async def __aenter__(self) -> "Environment":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
