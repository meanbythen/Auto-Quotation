"""
一车一件环境适配器

  MockMarket  本地模拟网页（mock_web/market_home.html）
  RealMarket  真实外网（独立 profile，首次人工登录一次，之后永久复用）
"""
from __future__ import annotations

import asyncio
import sys

import bot.config as cfg
from bot.logging_system import log_run

from .base import (install_dialog_auto_accept, launch_kwargs,
                   launch_persistent, probe_self_browser)
from bot.browser import MARKET_SELF_PORT


class MockMarket:
    label = "mock"

    def __init__(self, home_url: str, headless: bool = False):
        self.home_url = home_url
        self.headless = headless
        self.page = None
        self._browser = None
        self._ctx = None

    async def open(self, pw) -> None:
        self._browser = await pw.chromium.launch(headless=self.headless, **launch_kwargs())
        self._ctx = await self._browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN")
        self.page = await self._ctx.new_page()
        self.page.set_default_timeout(15000)
        install_dialog_auto_accept(self._ctx)

    async def go_home(self) -> None:
        await self.page.goto(self.home_url, wait_until="networkidle")
        await asyncio.sleep(1)

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass


class RealMarket:
    label = "real"

    def __init__(self, home_url: str, profile_dir=None, headless: bool = False,
                 cdp_url: str = ""):
        self.home_url = home_url
        self.profile_dir = profile_dir or cfg.BOT_PROFILE_DIR
        self.headless = headless
        self.cdp_url = (cdp_url or "").strip()
        self.page = None
        self._ctx = None
        self._browser = None
        self._login_checked = False

    async def open(self, pw) -> None:
        # ① 复用已打开的浏览器（CDP）：门户和一车一件可以在同一个浏览器里
        if not self.cdp_url:
            try:
                from bot.browser import detect_cdp
                found = detect_cdp()
                if found:
                    self.cdp_url = found
            except Exception:
                pass
        if self.cdp_url:
            print(f"\n🔗 一车一件连接已打开浏览器（CDP {self.cdp_url}）")
            self._browser = await pw.chromium.connect_over_cdp(self.cdp_url)
            ctx = self._browser.contexts[0] if self._browser.contexts else None
            if ctx is not None:
                for p in ctx.pages:
                    if "dataenlighten" in (p.url or "").lower():
                        self.page = p
                        break
                if self.page is None:
                    self.page = await ctx.new_page()
            if self.page is None:
                raise RuntimeError(f"CDP {self.cdp_url} 未找到可用窗口")
            self._ctx = ctx
            self.label = "real-cdp"
            self.page.set_default_timeout(15000)
            install_dialog_auto_accept(ctx)
            await self.page.goto(self.home_url, wait_until="networkidle")
            await asyncio.sleep(2)
            await self._ensure_login()
            return

        # ② 上次程序拉起的一车一件浏览器还活着 → 直接接管，避免 profile 锁冲突
        if not self.cdp_url:
            self.cdp_url = probe_self_browser(MARKET_SELF_PORT, "一车一件")
            if self.cdp_url:
                self._browser = await pw.chromium.connect_over_cdp(self.cdp_url)
                ctx = self._browser.contexts[0] if self._browser.contexts else None
                if ctx is not None:
                    for p in ctx.pages:
                        if "dataenlighten" in (p.url or "").lower():
                            self.page = p
                            break
                    if self.page is None:
                        self.page = await ctx.new_page()
                if self.page is None:
                    raise RuntimeError(f"CDP {self.cdp_url} 未找到可用窗口")
                self._ctx = ctx
                self.label = "real-cdp"
                self.page.set_default_timeout(15000)
                install_dialog_auto_accept(ctx)
                await self.page.goto(self.home_url, wait_until="networkidle")
                await asyncio.sleep(2)
                await self._ensure_login()
                return

        # ③ 自己拉起独立 profile 的浏览器（登录一次复用）
        self.profile_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._ctx = await launch_persistent(
                pw, self.profile_dir, MARKET_SELF_PORT, "一车一件")
        except Exception as exc:
            pref = (getattr(cfg, "BOT_BROWSER_PREFERENCE", "") or "").lower()
            if pref != "360":
                raise
            print(f"  ⚠️ 360 安全浏览器无法被程序驱动（{type(exc).__name__}），改用 Chrome")
            cfg.BOT_BROWSER_PREFERENCE = "chrome"
            cfg.BOT_BROWSER_PATH = ""
            self._ctx = await launch_persistent(
                pw, self.profile_dir, MARKET_SELF_PORT, "一车一件")
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.page.set_default_timeout(15000)
        install_dialog_auto_accept(self._ctx)
        await self.page.goto(self.home_url, wait_until="networkidle")
        await asyncio.sleep(2)
        await self._ensure_login()

    async def _ensure_login(self) -> None:
        """首次使用：检测是否已登录（能看到 VIN 搜索框就算已登录）"""
        if self._login_checked:
            return
        self._login_checked = True
        need_login = False
        try:
            box = self.page.locator("input[placeholder*='VIN'], input[placeholder*='车架号']")
            need_login = not await box.count()
        except Exception:
            need_login = True
        if not need_login:
            return

        print(f"\n{'='*60}")
        print(f"  🔐 检测到一车一件未登录，请用手机验证码登录")
        print(f"{'='*60}")
        # 打包后的 GUI 没有 stdin：改为轮询等待，不阻塞
        if sys.stdin and sys.stdin.isatty():
            input("\n✅ 登录完成后按 Enter 继续...")
        else:
            print(f"  登录完成后程序会自动继续（最多等待 240 秒）")
            waited = 0
            while waited < 240:
                try:
                    if await self.page.locator(
                            "input[placeholder*='VIN'], input[placeholder*='车架号']").count():
                        break
                except Exception:
                    pass
                await asyncio.sleep(5)
                waited += 5
        await self.page.reload(wait_until="networkidle")
        await asyncio.sleep(2)
        log_run("一车一件登录完成")

    async def go_home(self) -> None:
        await self.page.goto(self.home_url, wait_until="networkidle")
        await asyncio.sleep(1)

    async def close(self) -> None:
        if self._browser is not None:
            # CDP 只断开连接，不关闭你已打开的浏览器
            try:
                await self._browser.close()
            except Exception:
                pass
            return
        # persistent context 不关，保留登录态
        print(f"💡 一车一件 persistent 浏览器保持打开（{self.profile_dir}）")
