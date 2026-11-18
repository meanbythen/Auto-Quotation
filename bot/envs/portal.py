"""
理赔门户环境适配器

  MockPortal  本地模拟网页（mock_web/index.html）
  RealPortal  真实内网门户
              · 配置 PORTAL_CDP_URL  → 接管已打开的浏览器（不新开窗口）
              · 未配置              → 新开窗口 + 独立 profile（登录一次永久复用）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import bot.config as cfg
from bot.logging_system import log_run
from bot.page_objects import find_claim_rows, step_home_list_claims

from .base import (install_dialog_auto_accept, launch_kwargs,
                   launch_persistent, probe_self_browser)
from bot.browser import PORTAL_SELF_PORT


class MockPortal:
    label = "mock"

    def __init__(self, home_url: str, headless: bool = False):
        self.home_url = home_url
        self.headless = headless
        self.page = None
        self._browser = None
        self._ctx = None

    @property
    def pages(self) -> list:
        try:
            return list(self._ctx.pages)
        except Exception:
            return []

    @property
    def context(self):
        """BrowserContext：采集/录制注入脚本时需要（None = 未启动）"""
        return self._ctx

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

    async def refresh(self) -> None:
        """强制刷新（业务系统必须刷新才会更新列表）"""
        await self.page.reload(wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

    async def ensure_ready(self, timeout_s: int = 240) -> None:
        return None

    async def list_claims(self) -> list[str]:
        return await step_home_list_claims(self.page, {})

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass


class RealPortal:
    label = "real"

    def __init__(self, home_url: str, cdp_url: str = "", profile_dir=None,
                 headless: bool = False):
        self.cdp_url = (cdp_url or "").strip()
        self.profile_dir = profile_dir or cfg.PORTAL_PROFILE_DIR
        self.headless = headless
        self.page = None
        self._browser = None
        self._ctx = None
        if self.cdp_url:
            self.label = "real-cdp"
        self.home_url = self._cached_home(home_url) or home_url
        self._base_home = home_url

    # ---------- 记住真实首页地址（配置的地址常被杀到 404，人工点『返回首页』后的地址才有效） ----------
    @staticmethod
    def _home_cache_path():
        from bot.logging_system import app_data_dir
        d = app_data_dir() / "config"
        d.mkdir(parents=True, exist_ok=True)
        return d / "portal_home.json"

    def _cached_home(self, base_url: str) -> str:
        try:
            import json
            data = json.loads(self._home_cache_path().read_text(encoding="utf-8") or "{}")
            if data.get("base") == base_url and data.get("home"):
                cached = str(data["home"])
                print(f"  [i] 使用上次恢复的真实首页: {cached}")
                return cached
        except Exception:
            pass
        return ""

    def _remember_home(self, base_url: str) -> None:
        try:
            import json
            self._home_cache_path().write_text(json.dumps(
                {"base": base_url, "home": self.home_url}, ensure_ascii=False,
                indent=2), encoding="utf-8")
        except Exception:
            pass

    @property
    def pages(self) -> list:
        try:
            return list(self._ctx.pages)
        except Exception:
            return []

    @property
    def context(self):
        """BrowserContext：采集/录制注入脚本时需要（None = 未启动）"""
        return self._ctx

    async def open(self, pw) -> None:
        # 没有显式配置 CDP 时，自动探测本机已启动的浏览器（用户自己打开的 360/Chrome）
        if not self.cdp_url:
            try:
                from bot.browser import detect_cdp
                found = detect_cdp()
                if found:
                    self.cdp_url = found
                    self.label = "real-cdp"
                    print(f"\n  [i] 自动检测到可接管的已打开浏览器: {found}")
                    log_run(f"自动接管已打开浏览器（CDP）: {found}")
            except Exception:
                pass

        # 选了 360：Playwright 直接启动 360 壳不一定兼容 → 先用调试端口启动再接管
        if not self.cdp_url and self._prefers_360():
            await self._launch_360_with_debug_port()

        if self.cdp_url:
            print(f"\n🔗 连接已打开浏览器（CDP {self.cdp_url}）")
            self._browser = await pw.chromium.connect_over_cdp(self.cdp_url)
            self._ctx = self._browser.contexts[0] if self._browser.contexts \
                else await self._browser.new_context()
            target = self._pick_portal_page()
            if target is None:
                target = await self._ctx.new_page()
                await target.goto(self.home_url, wait_until="domcontentloaded")
            self.page = target
            await self.page.bring_to_front()
            await self._recover_from_404()
            print(f"  [✓] 已接管现有页面: {self.page.url}")
            log_run(f"门户复用已打开浏览器（CDP）: {self.page.url}")
            return

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # 上次程序拉起的门户浏览器还活着 → 直接接管，避免 profile 锁冲突（Target closed）
        if not self.cdp_url:
            self.cdp_url = probe_self_browser(PORTAL_SELF_PORT, "门户")
            if self.cdp_url:
                self._browser = await pw.chromium.connect_over_cdp(self.cdp_url)
                self._ctx = self._browser.contexts[0] if self._browser.contexts \
                    else await self._browser.new_context()
                target = self._pick_portal_page()
                if target is None:
                    target = await self._ctx.new_page()
                    await target.goto(self.home_url, wait_until="domcontentloaded")
                self.page = target
                await self.page.bring_to_front()
                await self._recover_from_404()
                print(f"  [✓] 已接管程序浏览器现有页面: {self.page.url}")
                return
        try:
            self._ctx = await launch_persistent(
                pw, self.profile_dir, PORTAL_SELF_PORT, "门户")
        except Exception as exc:
            if not self._prefers_360():
                raise
            print(f"  ⚠️ 360 安全浏览器无法被程序驱动（{type(exc).__name__}）")
            print("     360se 不支持调试端口，自动化只能改用 Chromium 内核浏览器")
            log_run("360se 无法自动化（无调试端口），改用 Chrome")
            cfg.BOT_BROWSER_PREFERENCE = "chrome"
            cfg.BOT_BROWSER_PATH = ""
            self._ctx = await launch_persistent(
                pw, self.profile_dir, PORTAL_SELF_PORT, "门户")
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        print(f"  [i] 真实门户使用独立 profile: {self.profile_dir}")
        install_dialog_auto_accept(self._ctx)

    @staticmethod
    def _prefers_360() -> bool:
        pref = (getattr(cfg, "BOT_BROWSER_PREFERENCE", "") or "").lower()
        path = (getattr(cfg, "BOT_BROWSER_PATH", "") or "").lower()
        return pref == "360" or "360se" in path or "360chrome" in path

    async def _launch_360_with_debug_port(self, port: int = 9222,
                                          timeout_s: float = 20.0) -> None:
        """没有可接管的浏览器时：用调试端口拉起 360，再交给 Playwright 接管
        （360 安全浏览器不是标准 Chromium 壳，直接 launch 常常失败，这样最稳）"""
        import subprocess
        from bot.browser import detect_cdp, resolve

        info = resolve("360", getattr(cfg, "BOT_BROWSER_PATH", "") or "")
        exe = info.get("path") or ""
        # 360 安全浏览器（360se）屏蔽了 DevTools：进程带参数也不监听端口 → 无法自动化
        if Path(exe).name.lower() == "360se.exe":
            print("\n  ⚠️ 360 安全浏览器（360se）不支持自动化：DevTools 调试端口被屏蔽")
            print("     已自动改用 Chrome（如需 360 界面，请安装 360 极速浏览器 Chromium 内核版）")
            log_run("360se 不支持自动化 → 自动改用 Chrome")
            cfg.BOT_BROWSER_PREFERENCE = "chrome"
            cfg.BOT_BROWSER_PATH = ""
            return
        if not exe:
            print("  ⚠️ 未找到可自动化的 360 浏览器，改用 Chrome")
            cfg.BOT_BROWSER_PREFERENCE = "chrome"
            return

        found = detect_cdp((port,))
        if found:
            self.cdp_url = found
            self.label = "real-cdp"
            return

        profile = Path(str(self.profile_dir) + "_360")
        profile.mkdir(parents=True, exist_ok=True)
        cmd = [exe, f"--remote-debugging-port={port}",
               f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
               self.home_url]
        try:
            print(f"\n🌐 启动 360 浏览器（可接管）: {exe}")
            print(f"   调试端口 {port} · 登录一次后自动复用（{profile}）")
            log_run(f"启动 360 浏览器（调试端口 {port}）{exe}")
            subprocess.Popen(cmd, close_fds=True)
        except Exception as exc:
            print(f"  ⚠️ 启动 360 失败: {exc}")
            log_run(f"启动 360 失败: {exc}")
            return

        waited = 0.0
        while waited < timeout_s:
            await asyncio.sleep(1)
            waited += 1
            found = detect_cdp((port,), timeout=0.6)
            if found:
                self.cdp_url = found
                self.label = "real-cdp"
                print(f"  [✓] 360 浏览器已就绪（{found}），请在其中登录门户")
                log_run(f"360 浏览器已就绪（CDP {found}）")
                return
        print("  ⚠️ 360 调试端口 20s 内未就绪，改用 Playwright 直接启动")
        print("     若直接启动也失败：360 安全浏览器不支持自动化，请改用 360 极速浏览器"
              "（Chromium 内核）或 Edge/Chrome")

    def _pick_portal_page(self):
        """在已打开的浏览器里挑门户页面：优先首页/案件列表，跳过报价、定损等详情页"""
        from urllib.parse import urlparse
        try:
            host = urlparse(self.home_url).hostname or ""
        except Exception:
            host = ""
        best, best_score = None, -99
        try:
            pages = list(self._ctx.pages)
        except Exception:
            pages = []
        for pg in pages:
            u = (pg.url or "")
            score = 0
            if host and host in u:
                score += 3
            low = u.lower()
            if "quote" in low or "/loss" in low:
                score -= 4
            if "404" in u:
                score -= 6
            if "claimOperate" in u or "claimOperation" in u or "claimcar" in u:
                score += 2
            if u.rstrip("/").endswith(("index", "index.html", "home", "main")):
                score += 1
            if score > best_score:
                best, best_score = pg, score
        if best is None or best_score < 0:
            return None
        return best

    async def _recover_from_404(self) -> bool:
        """门户跳到 404 时点页面中间的『返回首页』（刷新/重进都会落到 404）"""
        try:
            url = self.page.url or ""
        except Exception:
            url = ""
        try:
            body = ""
            try:
                body = (await self.page.locator("body").inner_text(timeout=2000)) or ""
            except Exception:
                body = ""
            is_404 = ("/404" in url or "404" in url
                      or "404" in body[:400] or "页面不存在" in body[:800]
                      or "找不到页面" in body[:800])
        except Exception:
            is_404 = "/404" in url
        if not is_404:
            return False

        print(f"  [i] 门户跳到了 404（{url}），自动点『返回首页』...")
        log_run(f"门户 404 → 自动返回首页: {url}")
        selectors = [
            "a:has-text('返回首页')", "button:has-text('返回首页')",
            "text=返回首页", "a:has-text('返回')", "button:has-text('返回')",
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if await el.count() == 0:
                    continue
                async with self.page.expect_navigation(timeout=15000):
                    await el.click()
                await self.page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(1)
                new_url = self.page.url or ""
                print(f"  [✓] 已返回首页: {new_url}")
                if "/404" not in new_url and new_url.startswith("http"):
                    self.home_url = new_url      # 记住真实首页地址，后续不再进 404
                    self._remember_home(self._base_home)
                return True
            except Exception:
                continue
        print(f"  ⚠️ 404 页面未找到『返回首页』，请人工点一下；当前地址: {url}")
        return False

    async def go_home(self) -> None:
        await self.page.goto(self.home_url, wait_until="networkidle")
        await asyncio.sleep(1)
        await self._recover_from_404()

    async def refresh(self) -> None:
        """强制刷新（真实门户常为 hash 路由 / SPA，重新 goto 不会真正刷新列表）"""
        try:
            if await self._recover_from_404():
                await asyncio.sleep(1)
                return
            await self.page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            await self.go_home()
        await asyncio.sleep(1)
        await self._recover_from_404()

    async def ensure_ready(self, timeout_s: int = 240) -> None:
        """等待页面出现报案号列表（未登录时提示人工登录，程序自动等待）"""
        waited = 0
        announced = False
        while waited < timeout_s:
            try:
                if await find_claim_rows(self.page):
                    print(f"  [✓] 门户首页已就绪")
                    return
                # 登录后常被重定向到 /claimOperate/404 → 自动点『返回首页』
                if await self._recover_from_404():
                    continue
                if not announced:
                    print(f"\n{'='*60}")
                    print(f"  🔐 未检测到案件列表，请在已打开的浏览器中完成门户登录")
                    print(f"  登录后程序会自动继续（最多等待 {timeout_s} 秒）")
                    print(f"  当前页面：{self.page.url}")
                    print(f"{'='*60}")
                    announced = True
            except Exception:
                pass
            await asyncio.sleep(5)
            waited += 5
        raise RuntimeError(f"等待门户登录超时（{timeout_s}s），请确认已登录 {self.page.url}")

    async def list_claims(self) -> list[str]:
        claims = await step_home_list_claims(self.page, {})
        if claims:
            return claims
        # 自愈：可能停在报价/详情页，或首页地址不对 → 重新打开首页再扫一次
        try:
            print(f"  [i] 首页未读到报案号（当前 {self.page.url[:100]}）→ 重新打开首页再扫")
            await self.page.goto(self.home_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)
            await self._recover_from_404()
            claims = await step_home_list_claims(self.page, {})
            if not claims:
                print(f"  ⚠️ 重新打开首页后仍为 0 个报案号（{self.page.url[:100]}）；"
                      f"如果浏览器里明明有案件，请把这段日志发回")
                log_run(f"首页重扫仍为 0（{self.page.url[:100]}）")
        except Exception as exc:
            print(f"  [!] 回首页重扫失败: {exc}")
        return claims

    async def close(self) -> None:
        # 真实环境故意保持打开：persistent 保留登录态，CDP 是用户自己的浏览器
        if self.cdp_url:
            print(f"💡 理赔门户窗口保持打开（CDP 模式接管的是你自己的浏览器）")
        else:
            print(f"💡 理赔门户窗口保持打开（登录态复用 {self.profile_dir}）")
