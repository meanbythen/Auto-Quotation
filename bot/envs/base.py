"""
环境适配器公共能力：浏览器解析、原生弹窗自动接受
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from bot.browser import (
    launch_kwargs as _browser_launch_kwargs,
    detect_cdp, kill_profile_owner,
)
from bot.logging_system import log_run, log_warn


def launch_kwargs() -> dict:
    """按配置解析要用的浏览器（本机 Chrome / Edge / 360 / 内置 Chromium）"""
    import bot.config as cfg
    kwargs, info = _browser_launch_kwargs(
        getattr(cfg, "BOT_BROWSER_PREFERENCE", "auto"),
        getattr(cfg, "BOT_BROWSER_PATH", ""),
    )
    where = info.get("path") or "Playwright 内置"
    print(f"  [浏览器] {info['label']} → {where}")
    log_run(f"浏览器: {info['label']} → {where}")
    warning = info.get("warning")
    if warning:
        print(f"  ⚠️ {warning}")
        log_warn(warning)
    return kwargs


def probe_self_browser(port: int, what: str) -> str:
    """上一次程序拉起的浏览器可能还活着（占着 profile）→ 返回它的 CDP 地址

    不复用的话，旧实例持有 user-data-dir 锁，新实例会被单例机制顶掉（秒退），
    Playwright 随之报 'Target page, context or browser has been closed'。
    """
    url = detect_cdp([port])
    if url:
        print(f"  [i] 检测到上次程序打开的{what}浏览器仍在运行 → 直接接管（{url}）")
        log_run(f"接管上次程序拉起的{what}浏览器: {url}")
    return url


def sync_chrome_display_prefs(profile_dir) -> None:
    """把你日常 Chrome 的缩放设置同步到程序 profile

    程序 profile 是新建的，默认 100% 缩放；如果你日常浏览器对门户/一车一件
    调过缩放（比如 90% 才能看全表格），程序窗口就会显得“内容显示不全”。
    在启动前把 zoom 设置合并进程序 profile 的 Preferences（Chrome 未运行时写才生效）。
    """
    import json
    import os
    from pathlib import Path

    src = Path(os.environ.get("LOCALAPPDATA", "")) / \
        "Google/Chrome/User Data/Default/Preferences"
    dst = Path(profile_dir) / "Default" / "Preferences"
    try:
        if not src.exists():
            return
        s = json.loads(src.read_text(encoding="utf-8", errors="ignore"))
        dflt = s.get("partition", {}).get("default_zoom_level")
        exc = (s.get("profile", {}).get("content_settings", {})
               .get("exceptions", {}).get("zoom_factor"))
        if dflt is None and not exc:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            d = json.loads(dst.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            d = {}
        if dflt is not None:
            d.setdefault("partition", {})["default_zoom_level"] = dflt
        if exc:
            (d.setdefault("profile", {}).setdefault("content_settings", {})
             .setdefault("exceptions", {})["zoom_factor"]) = exc
        dst.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        print("  [i] 已同步你日常 Chrome 的页面缩放设置到程序浏览器")
        log_run("同步 Chrome 缩放设置到程序 profile")
    except Exception:
        pass


async def launch_persistent(pw, profile_dir, self_port: int, what: str):
    """带固定调试端口启动 persistent 浏览器；失败（疑旧实例占用）则清理重试一次

    ignore_default_args 去掉两个默认参数：
      --no-sandbox        → 触发窗口顶部黄色警告横幅（挤掉一截可视高度）
      --enable-automation → 触发「Chrome 正受到自动测试软件的控制」信息条
    """
    sync_chrome_display_prefs(profile_dir)
    args = ["--start-maximized", "--window-size=1600,900",
            "--disable-blink-features=AutomationControlled",
            f"--remote-debugging-port={self_port}"]
    kwargs = dict(user_data_dir=str(profile_dir), headless=False,
                  viewport=None, locale="zh-CN", args=args,
                  ignore_default_args=["--no-sandbox", "--enable-automation"],
                  **launch_kwargs())
    try:
        return await pw.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:
        msg = str(exc)
        if "Target" not in msg and "closed" not in msg and "Singleton" not in msg:
            raise
        print(f"  ⚠️ {what}浏览器启动失败（疑似旧实例占用配置目录）→ 清理后重试")
        log_warn(f"{what}浏览器启动失败，清理占用进程后重试: {msg.splitlines()[0][:160]}")
        n = kill_profile_owner(profile_dir)
        print(f"  [i] 已结束 {n} 个占用进程，2 秒后重试")
        await asyncio.sleep(2)
        return await pw.chromium.launch_persistent_context(**kwargs)


def install_dialog_auto_accept(ctx) -> None:
    """全局自动接受原生弹窗（alert/confirm）：
    人工点击页面触发的弹窗不会阻塞程序动作；程序点击产生的弹窗也被自动确认。
    各 step 内自己注册的 dialog handler 仍优先生效。"""

    async def _accept(d):
        try:
            await d.accept()
        except Exception:
            pass

    def _on_page(page):
        page.on("dialog", lambda d: asyncio.create_task(_accept(d)))

    try:
        ctx.on("page", _on_page)
        for pg in ctx.pages:
            pg.on("dialog", lambda d: asyncio.create_task(_accept(d)))
    except Exception:
        pass


async def wait_until(predicate: Callable[[], bool], timeout_s: int,
                     interval: float = 2.0,
                     on_wait: Optional[Callable[[int], None]] = None) -> bool:
    """轮询等待条件成立（真实门户登录等场景）"""
    waited = 0.0
    while waited < timeout_s:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if on_wait:
            try:
                on_wait(waited)
            except Exception:
                pass
        await asyncio.sleep(interval)
        waited += interval
    return False
