r"""
浏览器选择 —— 支持多浏览器（Chrome / Edge / 360 / Playwright 内置 Chromium）

背景：PyInstaller onefile 打包不会把 Playwright 内置 Chromium 一起塞进 exe，
打包后 `_MEI...\playwright\...\chrome.exe` 不存在 → 启动即报
"Executable doesn't exist / looks like Playwright was just installed"。
解法：优先用本机已安装的 Chromium 内核浏览器（Chrome / Edge / 360），
找不到才回退 Playwright 内置 Chromium（源码运行 / 已执行 playwright install 时可用）。

用法：
    from bot.browser import resolve_browser, launch_kwargs
    kwargs, info = launch_kwargs("auto")     # 或 "chrome" / "edge" / "360" / 绝对路径
    p.chromium.launch(headless=True, **kwargs)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------- 候选路径（Windows 常见安装位置） ----------
_WIN_CANDIDATES: list[tuple[str, str, list[str]]] = [
    (
        "chrome",
        "Google Chrome",
        [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ],
    ),
    (
        "edge",
        "Microsoft Edge",
        [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
    ),
    (
        "360",
        "360 浏览器",
        [
            # 360 安全浏览器（Chromium 内核，用户实际安装位置）
            r"C:\Chrome-bin\360se.exe",
            # 360 极速浏览器（Chromium 内核，Playwright 兼容性最好）
            r"C:\Program Files (x86)\360\360Chrome\Chrome\Application\360chrome.exe",
            r"C:\Program Files\360\360Chrome\Chrome\Application\360chrome.exe",
            os.path.expandvars(
                r"%LocalAppData%\360Chrome\Chrome\Application\360chrome.exe"),
            # 360 极速浏览器 X（Chromium 内核）
            os.path.expandvars(
                r"%LocalAppData%\360ChromeX\Chrome\Application\360ChromeX.exe"),
            # 360 安全浏览器（兼容模式内核，实验性）
            os.path.expandvars(r"%AppData%\360se6\Application\360se.exe"),
            r"C:\Program Files (x86)\360\360se6\Application\360se.exe",
        ],
    ),
]

# 自动接管：探测这些本地端口上已启动的浏览器（需 --remote-debugging-port）
CDP_PORTS = (9222, 9223, 9224, 9333)

# 程序自己拉起的浏览器使用的固定调试端口（下次启动直接接管，避免 profile 锁冲突）
PORTAL_SELF_PORT = 9333
MARKET_SELF_PORT = 9224


def detect_cdp(ports=CDP_PORTS, timeout: float = 1.0) -> str:
    """探测本机已启动的浏览器调试端口，返回可连接的 CDP 地址（找不到返回空串）"""
    import json
    import urllib.request

    for port in ports:
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(url + "/json/version", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "ignore") or "{}")
            if data.get("webSocketDebuggerUrl"):
                return url
        except Exception:
            continue
    return ""


def kill_profile_owner(profile_dir) -> int:
    """结束占用指定 user-data-dir 的浏览器进程（Windows，按命令行匹配）

    旧实例不带调试端口时无法接管，只能杀掉后重新启动；返回结束的进程数。
    """
    import subprocess
    path = str(profile_dir).replace("\\", "/").lower().replace("'", "''")
    ps = (
        "$p = Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -and $_.Name -match 'chrome|msedge|360' -and "
        f"$_.CommandLine.Replace('\\\\','/').ToLower().Contains('{path}') }}; "
        "$n = 0; foreach ($x in $p) { "
        "try { Stop-Process -Id $x.ProcessId -Force -ErrorAction Stop; $n++ } catch {} }; "
        "Write-Output $n"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=40)
        return int((out.stdout or "").strip() or "0")
    except Exception:
        return 0


PREFERENCE_LABELS = {
    "auto": "自动（优先 Chrome → Edge → 360 → 内置 Chromium）",
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "360": "360 浏览器（360se 不支持自动化，会自动改用 Chrome）",
    "bundled": "Playwright 内置 Chromium（仅源码环境可靠）",
    "custom": "自定义路径",
}


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        try:
            if p and Path(p).is_file():
                return str(Path(p))
        except Exception:
            continue
    return None


def detect_candidates() -> list[dict]:
    """探测本机可用浏览器，返回 [{kind,label,path}]（path 为 None 表示 Playwright 内置）"""
    items: list[dict] = []
    for kind, label, paths in _WIN_CANDIDATES:
        path = _first_existing(paths)
        if path:
            items.append({"kind": kind, "label": label, "path": path})
    return items


def bundled_available() -> bool:
    """Playwright 内置 Chromium 是否真的存在（打包后通常为 False）"""
    if getattr(sys, "frozen", False):
        return False  # onefile 不打包浏览器内核
    roots = [
        Path(os.path.expandvars(r"%LocalAppData%\ms-playwright")),
        Path(sys.prefix) / "Lib" / "site-packages" / "playwright" / "driver"
        / "package" / ".local-browsers",
    ]
    for root in roots:
        try:
            if root.is_dir() and any(root.rglob("chrome.exe")):
                return True
        except Exception:
            continue
    return False


def resolve(preference: str = "auto", custom_path: str = "") -> dict:
    """
    解析最终要用的浏览器。

    preference: auto / chrome / edge / 360 / bundled / custom
    custom_path: preference=custom 时的 exe 绝对路径

    返回 {kind,label,path,executable_path}；path=None 表示用 Playwright 内置。
    """
    pref = (preference or "auto").strip().lower()
    candidates = detect_candidates()
    by_kind = {c["kind"]: c for c in candidates}

    if pref == "custom":
        p = (custom_path or "").strip().strip('"')
        if p and Path(p).is_file():
            return {"kind": "custom", "label": Path(p).stem, "path": p,
                    "executable_path": p}
        return {"kind": "bundled", "label": "Playwright 内置 Chromium",
                "path": None, "executable_path": None,
                "warning": f"自定义浏览器路径不存在：{p or '(空)'}，已回退内置 Chromium"}

    if pref in ("chrome", "edge", "360"):
        if pref in by_kind:
            c = by_kind[pref]
            return {"kind": c["kind"], "label": c["label"], "path": c["path"],
                    "executable_path": c["path"]}
        # 指定了某种浏览器但本机没装 → 明确告知，并自动改用其它可用浏览器
        fallback = next((by_kind[k] for k in ("chrome", "edge", "360") if k in by_kind), None)
        if fallback:
            return {"kind": fallback["kind"], "label": fallback["label"],
                    "path": fallback["path"], "executable_path": fallback["path"],
                    "warning": f"未检测到 {PREFERENCE_LABELS[pref]}，已自动改用 "
                               f"{fallback['label']}（如需指定，请选 custom 并填路径）"}
        return {"kind": "bundled", "label": "Playwright 内置 Chromium",
                "path": None, "executable_path": None,
                "warning": f"未检测到 {PREFERENCE_LABELS[pref]}，已回退 Playwright 内置 Chromium"} 

    if pref in ("bundled",):
        return {"kind": "bundled", "label": "Playwright 内置 Chromium",
                "path": None, "executable_path": None}

    # auto：Chrome → Edge → 360 → 内置
    for kind in ("chrome", "edge", "360"):
        if kind in by_kind:
            c = by_kind[kind]
            return {"kind": c["kind"], "label": c["label"], "path": c["path"],
                    "executable_path": c["path"]}
    return {"kind": "bundled", "label": "Playwright 内置 Chromium",
            "path": None, "executable_path": None,
            "warning": "未检测到 Chrome/Edge/360，回退 Playwright 内置 Chromium"
                       "（打包版若无内置浏览器会启动失败，请在控制台指定浏览器路径）"}


def launch_kwargs(preference: str = "auto", custom_path: str = "") -> tuple[dict, dict]:
    """返回 (传给 p.chromium.launch 的参数, 解析信息)"""
    info = resolve(preference, custom_path)
    kwargs: dict = {}
    if info.get("executable_path"):
        kwargs["executable_path"] = info["executable_path"]
    return kwargs, info
