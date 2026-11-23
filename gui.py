"""
理赔报价机器人 —— 对标 n8n 深色科技风 UI

布局:
  ┌──────────────────────────────────────────────────┐
  │ 顶部标题栏（Logo + 报案号输入 + 运行控制 + 状态） │
  ├────┬─────────────────────────────────────────────┤
  │ 🤖 │                                             │
  │ 📼 │            主工作区（Tab 切换）              │
  │ 🔍 │                                             │
  │ 🎨 │                                             │
  ├────┴─────────────────────────────────────────────┤
  │            终端风格实时日志（Monaco 字体）         │
  └──────────────────────────────────────────────────┘
"""

from __future__ import annotations  # ← 必须在最顶端（除 docstring/注释）

# ============== Windows 高 DPI 修复（必须在 import tkinter 之前）==============
import sys as _sys
if _sys.platform == "win32":
    try:
        import ctypes as _ct
        _ct.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor V2
    except Exception:
        try:
            _ct.windll.user32.SetProcessDPIAware()   # 降级兼容
        except Exception:
            pass
    if getattr(_sys, "frozen", False):
        import os; os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
# ==========================================================================
import asyncio
import io
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog

from bot.logging_system import (
    setup, get_log_dir, tail, CATEGORY_LABELS, environment_summary,
    log_system, log_run, log_audit, log_error, log_run_text, log_err_text,
)
from bot.version import APP_NAME, version_text, recent_changes, latest
from bot.config import TRANSFER_TARGET_DEFAULT, PRICE_CACHE_VALID_DAYS

# ============== 主题配色常量 ==============
BG = "#0f1729"
SURFACE = "#1a2238"
SURFACE_2 = "#232d48"
BORDER = "#2a3552"
ACCENT = "#00c8ff"
ACCENT_HOVER = "#00e1ff"
SUCCESS = "#00d26a"
DANGER = "#ff4d6a"
WARNING = "#ffb547"
TEXT = "#e8edf5"
TEXT_MUTED = "#8899b4"
TEXT_DIM = "#5a6b87"

# ============== 运行模式（四种方式都调用同一套流程逻辑 bot/flows/claim_flow.py）==============
MODE_OPTIONS = [
    ("mock_loop", "模拟循环挂机"),
    ("real_loop", "真实循环挂机"),
    ("single",    "单步测试"),
    ("manual",    "人工测试"),
]
MODE_LABELS = dict(MODE_OPTIONS)                    # key → 显示名
MODE_KEYS = {v: k for k, v in MODE_OPTIONS}         # 显示名 → key
MODE_HINTS = {
    "mock_loop": "本地 mock 门户 + mock 一车一件，自动取号 → 处理 → 没新案件时持续刷新等待（手动停止才结束）",
    "real_loop": "真实内网门户 + 真实一车一件，自动取号循环；没新案件时持续刷新等待（生产挂机）",
    "single":    "只跑一个报案号；在『🧪 单步测试』页可逐步暂停确认",
    "manual":    "你自己在浏览器里操作，程序逐步校验，并与自动执行结果对比",
}


class _StreamLogRedirector(io.TextIOBase):
    """把 print / traceback 同时落到日志文件与当前日志盒

    - 日志盒切换页面时会被 destroy → unbind() 后只写文件，不再报 TclError
    - GUI 只装一次这个对象（见 main()），避免每建一个页面就层层重定向
    """

    def __init__(self, category: str = "run"):
        self.category = category
        self.widget = None
        self.lock = threading.Lock()

    # ---------- 绑定 ----------
    def bind(self, widget):
        self.widget = widget

    def unbind(self):
        self.widget = None

    # ---------- io.TextIOBase ----------
    def write(self, text):
        if not text:
            return 0
        if self.category == "error":
            log_err_text(text)
        else:
            log_run_text(text)
        w = self.widget
        if w is None:
            return len(text)
        try:
            w.after(0, self._append, w, text)
        except Exception:
            self.widget = None
        return len(text)

    def _append(self, w, text):
        try:
            if not w.winfo_exists():
                return
            w.configure(state="normal")
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if i > 0:
                    w.insert("end", "\n")
                self._colorize(w, line)
            w.see("end")
            w.configure(state="disabled")
        except Exception:
            pass

    @staticmethod
    def _colorize(w, line):
        if "✅" in line or "🎉" in line or "[✓]" in line or "💾" in line:
            w.insert("end", line, "success")
        elif "❌" in line or "🚫" in line or "Error" in line or "失败" in line:
            w.insert("end", line, "danger")
        elif "⚠️" in line or "⏹" in line or "[!]" in line or "超市场价" in line:
            w.insert("end", line, "warning")
        elif "🔹 STEP" in line:
            w.insert("end", line, "step")
        elif "🛠️" in line or "修复" in line:
            w.insert("end", line, "cyan")
        else:
            w.insert("end", line, "default")

    def flush(self):
        pass


# 全局单例：stdout → 运行日志，stderr → 错误日志
RUN_STREAM = _StreamLogRedirector("run")
ERR_STREAM = _StreamLogRedirector("error")


def _safe_state(widget, state: str) -> None:
    """页面切换后控件可能已销毁，改按钮状态时统一兜底"""
    try:
        if widget is not None and widget.winfo_exists():
            widget.configure(state=state)
    except Exception:
        pass


def export_parts_xlsx(path: str, rows) -> int:
    """把零件价格记录写成 Excel（write_only 流式写入，几万行也不吃内存）

    rows 为可迭代的 dict：vin/part_code/part_name/price/source/status/fresh/
    brand/model/fetched_at/expires_at
    """
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from bot.pricing.service import _SOURCE_LABELS

    head = ["VIN", "零件编码", "零件名称", "价格(元)", "来源", "状态",
            "品牌", "车型", "缓存时间", "到期时间"]
    widths = [21, 14, 26, 11, 13, 9, 12, 14, 20, 20]
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("零件价格")
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    head_cells = []
    for h in head:
        c = WriteOnlyCell(ws, value=h)
        c.font = Font(bold=True, color="FFFFFF", name="微软雅黑", size=10)
        c.fill = PatternFill("solid", fgColor="4472C4")
        c.alignment = Alignment(horizontal="center", vertical="center")
        head_cells.append(c)
    ws.append(head_cells)
    n = 0
    for r in rows:
        ws.append([
            r.get("vin", ""),
            r.get("part_code", ""),
            r.get("part_name", ""),
            r.get("price"),
            _SOURCE_LABELS.get(r.get("source", ""), r.get("source", "")),
            "有效" if r.get("fresh") else "已过期",
            r.get("brand", ""),
            r.get("model", ""),
            (r.get("fetched_at") or "").replace("T", " "),
            (r.get("expires_at") or "").replace("T", " "),
        ])
        n += 1
    ws.freeze_panes = "A2"
    wb.save(path)
    return n


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("🤖 Picc Quote Bot · 理赔报价机器人")
        # 计算居中位置（兼容多显示器）
        w, h = 1320, 860
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 40)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(980, 680)
        self.root.configure(bg=BG)

        self.is_running = False
        self.is_recording = False
        self.worker_thread = None
        self.current_view = "console"

        # ---------- 双按钮状态机 ----------
        # 两按钮状态：(开始挂机按钮文案/状态, 停止扫描按钮文案/状态)
        # 初始：(▶ 开始挂机 / normal,  ◼ 停止扫描 / disabled)
        # 拉起浏览器后：(◼ 停止挂机 / normal,  ▶ 开始扫描 / normal)
        # 扫描中：(◼ 停止挂机 / normal,  ◼ 停止扫描 / normal)
        # 暂停扫描：(◼ 停止挂机 / normal,  ▶ 开始扫描 / normal)
        self._loop_env = None       # 已拉起的 Environment
        self._loop_loop = None      # 持久 asyncio loop（在后台线程跑 run_forever）
        self._loop_loop_thread = None
        self._loop_scan_task = None # 当前扫描的 coroutine task（loop.create_task 返回的）
        self._pause_event = None    # asyncio.Event，set→暂停扫描
        self._browser_only = False  # True = 浏览器已拉起但没开始扫描
        # 扫描意图标志：由主线程同步置位/清除，避免 "_loop_scan_task 还没赋值" 时按钮状态错乱
        self._scan_active = False

        # 全局参数（跨页面存活：控件放在控制台页，变量本身跟着 App）
        self.tol_var = tk.StringVar(value="0")
        self.poll_var = tk.StringVar(value="10")
        self.market_real_var = tk.BooleanVar(value=False)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.skill_learn_var = tk.BooleanVar(value=False)
        self.skip_market_var = tk.BooleanVar(value=False)
        self.market_thresh_var = tk.StringVar(value="2000")
        # 🍃 一车一件减速参数 / 查询配额（控制台可改，点『开始扫描』时写入 bot.config）
        self.vin_delay_min_var = tk.StringVar(value="8")
        self.vin_delay_max_var = tk.StringVar(value="14")
        self.part_delay_min_var = tk.StringVar(value="5")
        self.part_delay_max_var = tk.StringVar(value="9")
        self.claim_cd_var = tk.StringVar(value="30")
        self.quota_short_var = tk.StringVar(value="20")
        self.quota_long_var = tk.StringVar(value="80")
        # 🍃 一车一件反爬开关（默认全开：成本低、效果最直接）
        self.market_rate_var = tk.BooleanVar(value=True)      # 速率限流（人类节奏）
        self.market_breaker_var = tk.BooleanVar(value=True)   # 封禁熔断（检测到封禁即停止比价+休眠）
        # 运行模式：四种方式都调用同一套流程逻辑（bot/flows/claim_flow.py）
        self.mode_var = tk.StringVar(value=MODE_LABELS["mock_loop"])
        # 超出权限补救：平级移交的目标人员（工号-姓名）
        self.transfer_var = tk.StringVar(value=TRANSFER_TARGET_DEFAULT)
        # 浏览器（多浏览器支持：auto/chrome/edge/360/custom）
        self.browser_var = tk.StringVar(value="auto")
        self.browser_path_var = tk.StringVar(value="")
        # 理赔门户：真实内网 / 本地 mock；复用已打开浏览器的 CDP 地址
        self.portal_real_var = tk.BooleanVar(value=False)
        self.portal_cdp_var = tk.StringVar(value="")
        self._status_value = ("就绪", TEXT_DIM)

        log_system(f"GUI 初始化 · {version_text()}")
        log_system(f"日志目录: {get_log_dir()}")

        # ============== 全局样式 ==============
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Sidebar.TButton",
                        background=BG, foreground=TEXT_MUTED, borderwidth=0,
                        padding=(16, 20), font=("Segoe UI Emoji", 14))
        style.map("Sidebar.TButton",
                  background=[("active", SURFACE_2), ("pressed", SURFACE)],
                  foreground=[("active", TEXT), ("pressed", ACCENT)])
        style.configure("SidebarActive.TButton",
                        background=SURFACE, foreground=ACCENT, borderwidth=0,
                        padding=(16, 20), font=("Segoe UI Emoji", 14))

        self._load_ui_config()
        self._build()
        self._autodetect_portal()

    # ============== 主布局 ==============
    def _build(self):
        # 顶栏
        self._build_topbar()
        # 主体: sidebar + content
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self._build_console_view()  # 首页 = 控制台
        self.root.after(800, self._learn_poll)   # 学习模式状态条

    def _build_topbar(self):
        """顶栏只保留 Logo + 名称，其余全部搬到控制台页"""
        bar = tk.Frame(self.root, bg=SURFACE, height=56)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="🤖", bg=SURFACE, fg=ACCENT,
                 font=("Segoe UI Emoji", 20)).pack(side="left", padx=(18, 6))
        tk.Label(bar, text="PICC Quote Bot", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI Semibold", 15)).pack(side="left")

    def _mini_entry(self, parent, var, width=3):
        """控制台内联数字输入框（减速参数 / 查询配额用）"""
        return tk.Entry(parent, textvariable=var, width=width, bg=SURFACE_2, fg=TEXT,
                        insertbackground=TEXT, relief="flat", font=("Consolas", 9),
                        justify="center", highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=ACCENT)

    def _make_btn(self, parent, text, bg_color, hover_color, cmd, disabled=False):
        """圆角风格按钮模拟"""
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg_color, fg="#000000" if bg_color == ACCENT else TEXT,
                      activebackground=hover_color,
                      activeforeground=TEXT,
                      font=("Segoe UI Semibold", 10),
                      relief="flat", cursor="hand2",
                      padx=14, pady=4, bd=0,
                      state="disabled" if disabled else "normal")
        return b

    def _set_status(self, text, color):
        self._status_value = (text, color)
        dot = self._ui("status_dot")
        if dot is not None:
            dot.configure(fg=color)
        txt = self._ui("status_text")
        if txt is not None:
            txt.configure(text=text, fg=color)

    # ============== Sidebar ==============
    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=BG, width=64)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        tk.Frame(sb, bg=BORDER, width=1).pack(side="right", fill="y")

        self.sb_icons = {}
        items = [
            ("console", "🏠", "🏠 控制台"),
            ("test", "🧪", "🧪 单步测试"),
            ("editor", "⬡", "⬡ 流程编辑器"),
            ("record", "📼", "📼 录制学习"),
            ("diff", "🔍", "🔍 流程比对"),
            ("logs", "📄", "📄 日志中心"),
            ("worklog", "📒", "📒 工作记录"),
            ("data", "🗄", "🗄 数据管理"),
        ]
        for key, icon, tooltip in items:
            b = tk.Button(sb, text=icon, command=lambda k=key: self._switch_view(k),
                          bg=BG, fg=TEXT_MUTED, activebackground=SURFACE_2,
                          activeforeground=ACCENT, relief="flat",
                          cursor="hand2", font=("Segoe UI Emoji", 18),
                          width=4, height=2, bd=0)
            b.pack(fill="x", pady=2)
            self.sb_icons[key] = b

        # 注意：不能在这里调 _switch_view，因为 self.content 还没赋值
        # 默认 view 由 _build() 末尾 _build_console_view() 直接建
        # 但 sidebar 按钮的 selected 样式要先设好
        if "console" in self.sb_icons:
            self.sb_icons["console"].configure(bg=SURFACE, fg=ACCENT)

    def _switch_view(self, key):
        self.current_view = key
        # 旧页面的日志盒即将销毁 → 先解绑，避免往已销毁控件里写字
        RUN_STREAM.unbind()
        for k, b in self.sb_icons.items():
            if k == key:
                b.configure(bg=SURFACE, fg=ACCENT)
            else:
                b.configure(bg=BG, fg=TEXT_MUTED)
        for w in self.content.winfo_children():
            w.destroy()
        if key == "console": self._build_console_view()
        elif key == "test": self._build_test_view()
        elif key == "editor": self._build_editor_view()
        elif key == "record": self._build_record_view()
        elif key == "diff": self._build_diff_view()
        elif key == "logs": self._build_log_view()
        elif key == "worklog": self._build_worklog_view()
        elif key == "data": self._build_data_view()
        else: self._build_console_view()


    # ============== View 0: 控制台（程序首页）==============
    # 控制台流程进度卡上的短名称（key 用去掉 step_ 前缀的 action 名）
    _STEP_SHORT = {
        "home_list_claims": "首页取号",
        "home_click_claim": "点击报案号",
        "quote_open_loss_edit": "打开定损平台",
        "loss_sync": "同步定损",
        "loss_extract_data": "提取数据",
        "market_vin_search": "VIN 查询",
        "market_query_parts": "零件比价",
        "loss_fix_over_market": "修复超价",
        "loss_submit": "提交定损",
        "quote_finalize": "报价完成",
    }

    def _short_step(self, sid, desc):
        key = (sid or "").replace("step_", "")
        return self._STEP_SHORT.get(key, (desc or sid or "")[:6])

    def _make_card(self, parent, title):
        card = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=(0, 10))
        tk.Label(card, text=title, bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=14, pady=(10, 8))
        return card

    def _build_console_view(self):
        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=(10, 4))

        head = tk.Frame(wrap, bg=BG); head.pack(fill="x")
        tk.Label(head, text="🏠 控制台", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(head, text="运行参数 / 运行状态 / 实时日志",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=10)

        body = tk.Frame(wrap, bg=BG); body.pack(fill="both", expand=True, pady=(8, 0))
        # 左右 4:6（原来左列固定 420px，右列吃掉全部剩余宽度 → 左侧说明文字被截断）
        body.grid_columnconfigure(0, weight=4, uniform="console")
        body.grid_columnconfigure(1, weight=6, uniform="console")
        body.grid_rowconfigure(0, weight=1)

        # 左列可滚动：参数项较多，窗口高度不够时自动出滚动条（不再裁掉卡片）
        left_outer = tk.Frame(body, bg=BG)
        left_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left_canvas = tk.Canvas(left_outer, bg=BG, highlightthickness=0, bd=0,
                                width=200, height=200)
        left_canvas.pack(side="left", fill="both", expand=True)
        left_sb = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_sb.set)
        left = tk.Frame(left_canvas, bg=BG)
        _left_win = left_canvas.create_window((0, 0), window=left, anchor="nw")

        # 所有长说明文字改自动换行：窗口/分栏宽度变了，wraplength 跟着变
        self._wrap_labels: list = []

        def _wrap(lbl, margin: int = 40):
            self._wrap_labels.append((lbl, margin))
            return lbl

        def _reflow(width: int):
            for lbl, margin in self._wrap_labels:
                try:
                    lbl.configure(wraplength=max(120, width - margin))
                except Exception:
                    pass

        def _left_sync(_evt=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))
            need = left.winfo_reqheight() > left_canvas.winfo_height()
            if need and not left_sb.winfo_ismapped():
                left_sb.pack(side="right", fill="y", before=left_canvas)
            elif not need and left_sb.winfo_ismapped():
                left_sb.pack_forget()

        def _left_width(_evt):
            left_canvas.itemconfigure(_left_win, width=_evt.width)
            _reflow(_evt.width)
            _left_sync()

        def _left_wheel(evt):
            try:
                _, y = left_canvas.winfo_pointerxy()
                top = left_canvas.winfo_rooty()
                if top <= y <= top + left_canvas.winfo_height():
                    first, last = left_canvas.yview()
                    if first <= 0.0 and last >= 1.0:
                        return "break"
                    left_canvas.yview_scroll(-int(evt.delta / 120) or -1, "units")
                    return "break"
            except Exception:
                pass

        def _bind_left_wheel(w):
            w.bind("<MouseWheel>", _left_wheel)
            for c in w.winfo_children():
                _bind_left_wheel(c)

        left.bind("<Configure>", _left_sync)
        left_canvas.bind("<Configure>", _left_width)

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        # ---------- 左 1：运行状态 ----------
        card = self._make_card(left, "运行状态")
        row = tk.Frame(card, bg=SURFACE); row.pack(fill="x", padx=14)
        self.status_capsule = tk.Frame(row, bg=SURFACE_2, padx=12, pady=5)
        self.status_capsule.pack(side="left")
        self.status_dot = tk.Label(self.status_capsule, text="●", bg=SURFACE_2,
                                   fg=TEXT_DIM, font=("Segoe UI", 11))
        self.status_dot.pack(side="left")
        self.status_text = tk.Label(self.status_capsule, text="就绪", bg=SURFACE_2,
                                    fg=TEXT_DIM, font=("Segoe UI Semibold", 10))
        self.status_text.pack(side="left", padx=(4, 0))

        self.run_counter = tk.Frame(row, bg=SURFACE_2, padx=12, pady=2)
        self.run_counter.pack(side="right")
        tk.Label(self.run_counter, text="已处理", bg=SURFACE_2, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        self.run_counter_label = tk.Label(self.run_counter, text="0", bg=SURFACE_2,
                                          fg=ACCENT, font=("Consolas", 15, "bold"))
        self.run_counter_label.pack(side="left", padx=(6, 0))

        btns = tk.Frame(card, bg=SURFACE); btns.pack(fill="x", padx=14, pady=(10, 6))
        self.start_btn = self._make_btn(btns, "▶ 开始挂机", SUCCESS, SURFACE_2,
                                        self.on_start)
        self.start_btn.pack(side="left")
        self.scan_btn = self._make_btn(btns, "▶ 开始扫描", ACCENT, SURFACE_2,
                                       self.on_scan_button)
        self.scan_btn.pack(side="left", padx=(8, 0))
        self.pause_btn = self._make_btn(btns, "⏸ 暂停", WARNING, SURFACE_2,
                                        self.on_pause_button)
        self.pause_btn.pack(side="left", padx=(8, 0))

        # ---------- 🧠 学习模式（某步失败 → 暂停等你手动处理 → 存技能） ----------
        learn = tk.Frame(card, bg=SURFACE)
        learn.pack(fill="x", padx=14, pady=(0, 8))
        self.learn_info = _wrap(tk.Label(learn, text="🧠 学习模式：某步失败时会暂停，等你手动处理",
                                         bg=SURFACE, fg=TEXT_DIM, font=("Segoe UI", 8),
                                         anchor="w", justify="left"), 150)
        self.learn_info.pack(side="left", fill="x", expand=True)
        self.learn_skip_btn = tk.Button(learn, text="⏭ 跳过本案件", bg=SURFACE_2, fg=WARNING,
                                        relief="flat", bd=0, cursor="hand2", padx=6, pady=1,
                                        font=("Segoe UI", 8), state="disabled",
                                        command=lambda: self._learn_decide("skip"))
        self.learn_skip_btn.pack(side="right")
        self.learn_save_btn = tk.Button(learn, text="💾 保存学习", bg=SURFACE_2, fg=SUCCESS,
                                        relief="flat", bd=0, cursor="hand2", padx=6, pady=1,
                                        font=("Segoe UI", 8), state="disabled",
                                        command=lambda: self._learn_decide("save"))
        self.learn_save_btn.pack(side="right", padx=(0, 4))

        rmode = tk.Frame(card, bg=SURFACE); rmode.pack(fill="x", padx=14, pady=(0, 3))
        tk.Label(rmode, text="运行模式", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 9), width=10, anchor="w").pack(side="left")
        self.mode_combo = ttk.Combobox(rmode, textvariable=self.mode_var, state="readonly",
                                       width=16, values=[v for _, v in MODE_OPTIONS],
                                       font=("Segoe UI", 9))
        self.mode_combo.pack(side="left", ipady=1)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        self.mode_hint = _wrap(tk.Label(card, text=MODE_HINTS.get(self._mode_key(), ""),
                                        bg=SURFACE, fg=TEXT_DIM, font=("Segoe UI", 8),
                                        anchor="w", justify="left"), 70)
        self.mode_hint.pack(fill="x", padx=34, pady=(0, 10))
        # 从别的页面切回来时，恢复真实状态
        self._set_status(*self._status_value)
        self.start_btn.configure(state="disabled" if self.is_running else "normal")
        self.scan_btn.configure(state="normal" if self.is_running else "disabled")
        self.pause_btn.configure(state="disabled")

        # ---------- 左 2：运行参数 ----------
        card = self._make_card(left, "运行参数")

        def _param_row(title, var, unit, hint):
            """标题+输入框一行，说明文字另起一行自动换行（窄分栏也不会被截断）"""
            row = tk.Frame(card, bg=SURFACE); row.pack(fill="x", padx=14, pady=(0, 10))
            head = tk.Frame(row, bg=SURFACE); head.pack(fill="x")
            tk.Label(head, text=title, bg=SURFACE, fg=TEXT_MUTED,
                     font=("Segoe UI", 10), width=10, anchor="w").pack(side="left")
            tk.Entry(head, textvariable=var, width=6, bg=SURFACE_2, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=("Consolas", 10),
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT).pack(side="left", ipady=2)
            tk.Label(head, text=unit, bg=SURFACE, fg=TEXT_DIM,
                     font=("Segoe UI", 9)).pack(side="left", padx=6)
            lbl = _wrap(tk.Label(row, text=hint, bg=SURFACE, fg=TEXT_DIM,
                                 font=("Segoe UI", 8), anchor="w", justify="left"), 120)
            lbl.pack(fill="x", padx=(76, 0), pady=(2, 0))

        _param_row("价格容忍度", self.tol_var, "元", "超过市场价此金额以内不算超")
        _param_row("本地价阈值", self.market_thresh_var, "元",
                   "低于此值的零件不查一车一件（省流量），设 0 = 全部查询")
        _param_row("刷新间隔", self.poll_var, "秒", "没新案件时持续刷新等待")

        r2 = tk.Frame(card, bg=SURFACE); r2.pack(fill="x", padx=12, pady=(0, 10))
        tk.Checkbutton(r2, text="一车一件走真实外网（未勾选 = 本地 mock）",
                       variable=self.market_real_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x")
        tk.Checkbutton(r2, text="🚫 跳过查询零件价格（全部以定损价直接提交）",
                       variable=self.skip_market_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x")
        tk.Checkbutton(r2, text="⏱ 速率限流（模拟人工节奏，秒数见下方）",
                       variable=self.market_rate_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x")
        tk.Checkbutton(r2, text="🛑 封禁熔断（识别防爬/验证码/9016 → 停比价并暂停 30 分钟）",
                       variable=self.market_breaker_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x")
        tk.Checkbutton(r2, text="🧠 学习模式（某步失败时暂停，你手动处理后存成技能）",
                       variable=self.skill_learn_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x")
        _wrap(tk.Label(card, text="真实外网同样接管上面那个已打开的浏览器（自动选一车一件标签，不新开窗口）",
                       bg=SURFACE, fg=TEXT_DIM, font=("Segoe UI", 8), anchor="w",
                       justify="left"), 70).pack(anchor="w", padx=34, pady=(0, 6))

        # ---------- 🍃 减速参数 / 查询配额（可直接在控制台改，点『开始扫描』生效）----------
        def _tune_row(parent, title):
            row = tk.Frame(parent, bg=SURFACE)
            row.pack(fill="x", padx=14, pady=(0, 3))
            tk.Label(row, text=title, bg=SURFACE, fg=TEXT_MUTED,
                     font=("Segoe UI", 10), width=8, anchor="w").pack(side="left")
            return row

        def _tune_min_max(row, text, v_min, v_max, unit=""):
            tk.Label(row, text=text, bg=SURFACE, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(side="left", padx=(6, 1))
            self._mini_entry(row, v_min).pack(side="left", ipady=1)
            if v_max is not None:
                tk.Label(row, text="~", bg=SURFACE, fg=TEXT_DIM,
                         font=("Segoe UI", 8)).pack(side="left")
                self._mini_entry(row, v_max).pack(side="left", ipady=1)
            if unit:
                tk.Label(row, text=unit, bg=SURFACE, fg=TEXT_DIM,
                         font=("Segoe UI", 8)).pack(side="left")

        _tune_min_max(_tune_row(card, "减速参数"), "VIN", self.vin_delay_min_var,
                      self.vin_delay_max_var, "s")
        _tune_min_max(_tune_row(card, ""), "零件", self.part_delay_min_var,
                      self.part_delay_max_var, "s")
        _tune_min_max(_tune_row(card, ""), "案件间", self.claim_cd_var, None, "s")
        _tune_min_max(_tune_row(card, "查询配额"), "10 分钟", self.quota_short_var, None, "次")
        _tune_min_max(_tune_row(card, ""), "60 分钟", self.quota_long_var, None, "次")
        _wrap(tk.Label(card, text="改完直接点『开始扫描』生效（写进 bot.config，不用重新打包）；"
                                  "VIN/零件合计计数，超配额先等待，配额填 0 = 不限",
                       bg=SURFACE, fg=TEXT_DIM, font=("Segoe UI", 8), anchor="w",
                       justify="left"), 70).pack(fill="x", padx=34, pady=(0, 8))

        r3 = tk.Frame(card, bg=SURFACE); r3.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(r3, text="移交目标", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI", 10), width=10, anchor="w").pack(side="left")
        tk.Entry(r3, textvariable=self.transfer_var, width=16, bg=SURFACE_2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Consolas", 10),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", ipady=2)
        _wrap(tk.Label(card, text="工号-姓名（超权限案件平级移交回本人）", bg=SURFACE,
                       fg=TEXT_DIM, font=("Segoe UI", 8), anchor="w",
                       justify="left"), 70).pack(anchor="w", padx=34, pady=(0, 10))

        # ---------- 浏览器（多浏览器支持） ----------
        r4 = tk.Frame(card, bg=SURFACE); r4.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(r4, text="浏览器", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI", 10), width=10, anchor="w").pack(side="left")
        combo = ttk.Combobox(r4, textvariable=self.browser_var, state="readonly",
                             width=9, values=["auto", "chrome", "edge", "360", "custom"],
                             font=("Segoe UI", 9))
        combo.pack(side="left", ipady=1)
        tk.Button(r4, text="浏览…", bg=SURFACE_2, fg=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=6, pady=1, font=("Segoe UI", 8),
                  command=self._pick_browser).pack(side="left", padx=4)
        combo.bind("<<ComboboxSelected>>",
                   lambda e: self._refresh_browser_hint())

        r5 = tk.Frame(card, bg=SURFACE); r5.pack(fill="x", padx=14, pady=(0, 3))
        tk.Entry(r5, textvariable=self.browser_path_var, bg=SURFACE_2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Consolas", 8),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(fill="x", ipady=2)
        self.browser_hint = _wrap(tk.Label(card, text="", bg=SURFACE, fg=TEXT_DIM,
                                           font=("Segoe UI", 8), anchor="w",
                                           justify="left"), 40)
        self.browser_hint.pack(fill="x", padx=14, pady=(0, 10))
        self._refresh_browser_hint()

        # ---------- 理赔门户（真实内网 / 复用已打开浏览器） ----------
        r6 = tk.Frame(card, bg=SURFACE); r6.pack(fill="x", padx=12, pady=(0, 4))
        tk.Checkbutton(r6, text="理赔门户走真实内网（未勾选 = 本地模拟网页）",
                       variable=self.portal_real_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w", command=self._refresh_portal_hint).pack(fill="x")
        r7 = tk.Frame(card, bg=SURFACE); r7.pack(fill="x", padx=14, pady=(0, 3))
        tk.Label(r7, text="复用已开", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI", 10), width=10, anchor="w").pack(side="left")
        tk.Button(r7, text="🔗 连接", bg=SURFACE_2, fg=ACCENT, relief="flat",
                  bd=0, cursor="hand2", padx=8, pady=2, font=("Segoe UI", 8),
                  command=self._connect_browser).pack(side="right", padx=(4, 0))
        tk.Entry(r7, textvariable=self.portal_cdp_var, bg=SURFACE_2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Consolas", 9),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", fill="x", expand=True,
                                            ipady=2)
        self.portal_hint = _wrap(tk.Label(card, text="", bg=SURFACE, fg=TEXT_DIM,
                                          font=("Segoe UI", 8), anchor="w",
                                          justify="left"), 40)
        self.portal_hint.pack(fill="x", padx=14, pady=(0, 10))
        self._refresh_portal_hint()

        # ---------- 左 3：版本与日志 ----------
        card = self._make_card(left, "版本与日志")
        vrow = tk.Frame(card, bg=SURFACE); vrow.pack(fill="x", padx=14, pady=(0, 3))
        tk.Label(vrow, text=f"v{latest()['version']}", bg=SURFACE, fg=ACCENT,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Button(vrow, text="📂 日志目录", bg=SURFACE_2, fg=TEXT, relief="flat",
                  bd=0, cursor="hand2", padx=8, pady=2, font=("Segoe UI", 8),
                  command=self._open_log_dir).pack(side="right")
        tk.Button(vrow, text="🔍 对比报告", bg=SURFACE_2, fg=TEXT, relief="flat",
                  bd=0, cursor="hand2", padx=8, pady=2, font=("Segoe UI", 8),
                  command=self._open_compare_dir).pack(side="right", padx=(0, 6))
        tk.Button(vrow, text="📸 采集", bg=SURFACE_2, fg=TEXT, relief="flat",
                  bd=0, cursor="hand2", padx=8, pady=2, font=("Segoe UI", 8),
                  command=self._open_capture_dir).pack(side="right", padx=(0, 6))
        _wrap(tk.Label(card, text=f"最新更新：v{latest()['version']} · {latest()['title']}",
                       bg=SURFACE, fg=TEXT_MUTED, font=("Segoe UI", 8), justify="left",
                       anchor="w"), 30).pack(fill="x", padx=14, pady=(2, 8))

        # ---------- 右：流程进度 ----------
        tk.Label(right, text="🔄 流程进度", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        import bot.page_objects as _
        from bot.workflow import build_default_workflow
        wf = build_default_workflow()
        steps = [(n.action or n.id, n.desc or n.id) for n in wf.nodes if n.type == "call_step"]

        self._step_cards = {}
        cards_wrap = tk.Frame(right, bg=BG); cards_wrap.pack(fill="x", pady=(4, 10))
        for i, (sid, desc) in enumerate(steps):
            card = tk.Frame(cards_wrap, bg=SURFACE, highlightthickness=1,
                            highlightbackground=BORDER)
            card.grid(row=i // 4, column=i % 4, padx=4, pady=4, sticky="nsew")
            tk.Label(card, text=f"STEP {i + 1}", bg=SURFACE, fg=ACCENT,
                     font=("Consolas", 8)).pack(padx=4, pady=(6, 0))
            tk.Label(card, text=self._short_step(sid, desc), bg=SURFACE, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(padx=4, pady=(0, 6))
            self._step_cards[sid] = card
        for c in range(4):
            cards_wrap.columnconfigure(c, weight=1, uniform="step")

        # ---------- 右：运行日志 ----------
        tk.Label(right, text="📋 运行日志", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.log_box = self._make_log_box(right)
        _bind_left_wheel(left)   # 左列所有控件滚轮可滚动

        # 视图切回来时按钮是新建的初始态 → 按当前运行状态重新上色/改文案
        self._update_button_states()

    # ============== 流程编辑器（真·可编辑版）==============
    def _build_editor_view(self):
        """
        三栏：左工具栏 + 中 Canvas + 右属性面板
        支持：增删节点 / 改属性 / 条件分支 / 保存-打开 workflow.json
        """
        import bot.page_objects as _  # 触发 @register_step
        from bot.workflow import Workflow, WorkflowNode, STEP_REGISTRY, build_default_workflow

        target = Path(__file__).resolve().parent / "data" / "workflows" / "main_flow.json"
        if not target.exists():
            build_default_workflow().save(target)

        self._wf = Workflow.load(str(target))
        self._wf_path = str(target)
        self._wf_selected = None

        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True)

        # 顶栏
        top = tk.Frame(wrap, bg=SURFACE, height=44)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="⬡ 流程编辑器", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI Semibold", 13)).pack(side="left", padx=14, pady=10)
        self._wf_name_var = tk.StringVar(value=self._wf.name)
        tk.Label(top, textvariable=self._wf_name_var, bg=SURFACE, fg=TEXT_DIM,
                 font=("Consolas", 10)).pack(side="left", pady=10)

        tk.Button(top, text="📄 新建", bg=SURFACE_2, fg=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=4, font=("Segoe UI", 10),
                  command=self._wf_new).pack(side="right", padx=(6, 14), pady=8)
        tk.Button(top, text="💾 保存", bg=ACCENT, fg="#000", relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=4, font=("Segoe UI Semibold", 10),
                  command=self._wf_save).pack(side="right", padx=6, pady=8)
        tk.Button(top, text="📂 打开", bg=SURFACE_2, fg=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=4, font=("Segoe UI", 10),
                  command=self._wf_open).pack(side="right", padx=6, pady=8)

        # 三栏主体
        body = tk.Frame(wrap, bg=BG)
        body.pack(fill="both", expand=True)

        # 左工具栏
        tools = tk.Frame(body, bg=SURFACE, width=170)
        tools.pack(side="left", fill="y")
        tools.pack_propagate(False)

        tk.Label(tools, text="➕ 添加节点", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(12, 6))

        call_frame = tk.Frame(tools, bg=SURFACE)
        call_frame.pack(fill="x", padx=8)
        tk.Label(call_frame, text="▸ call_step", bg=SURFACE, fg=ACCENT,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w")
        self._wf_step_combo = ttk.Combobox(call_frame,
            values=sorted(STEP_REGISTRY.keys()), state="readonly", width=22)
        if STEP_REGISTRY:
            self._wf_step_combo.set(sorted(STEP_REGISTRY.keys())[0])
        self._wf_step_combo.pack(fill="x", pady=3)
        tk.Button(call_frame, text="＋ 添加", bg=ACCENT, fg="#000", relief="flat", bd=0,
                  cursor="hand2", padx=8, pady=2, font=("Segoe UI", 9),
                  command=self._wf_add_call_step).pack(fill="x", pady=(0, 8))

        tk.Button(tools, text="◆ 添加条件分支", bg=SURFACE_2, fg=WARNING,
                  relief="flat", bd=0, cursor="hand2", padx=10, pady=4,
                  font=("Segoe UI", 9), command=self._wf_add_condition).pack(fill="x", padx=8, pady=3)
        tk.Button(tools, text="⟳ 添加循环", bg=SURFACE_2, fg="#a78bfa",
                  relief="flat", bd=0, cursor="hand2", padx=10, pady=4,
                  font=("Segoe UI", 9), command=self._wf_add_loop).pack(fill="x", padx=8, pady=3)

        tk.Frame(tools, bg=BORDER, height=1).pack(fill="x", padx=8, pady=10)
        tk.Label(tools, text="🗑 节点操作", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(4, 4))
        tk.Button(tools, text="🗑 删除选中", bg="#3a1f2e", fg=DANGER,
                  relief="flat", bd=0, cursor="hand2", padx=10, pady=4,
                  font=("Segoe UI", 9), command=self._wf_delete_selected).pack(fill="x", padx=8, pady=2)
        tk.Button(tools, text="↑ 上移", bg=SURFACE_2, fg=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=10, pady=3, font=("Segoe UI", 9),
                  command=lambda: self._wf_move(-1)).pack(fill="x", padx=8, pady=2)
        tk.Button(tools, text="↓ 下移", bg=SURFACE_2, fg=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=10, pady=3, font=("Segoe UI", 9),
                  command=lambda: self._wf_move(+1)).pack(fill="x", padx=8, pady=2)
        tk.Button(tools, text="↩️ 恢复默认骨架", bg=SURFACE_2, fg=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=10, pady=3, font=("Segoe UI", 9),
                  command=self._wf_reset_default).pack(fill="x", padx=8, pady=(10, 0))

        # 中 Canvas
        canvas_wrap = tk.Frame(body, bg=BG)
        canvas_wrap.pack(side="left", fill="both", expand=True)
        self._wf_canvas = tk.Canvas(canvas_wrap, bg=BG, highlightthickness=0, cursor="hand2")
        self._wf_canvas.pack(fill="both", expand=True)
        self._wf_canvas.bind("<Configure>", lambda e: self._wf_redraw())
        self._wf_canvas.bind("<Button-1>", self._wf_canvas_click)
        self._wf_canvas.bind("<MouseWheel>", self._wf_canvas_scroll)

        # 右属性面板
        prop = tk.Frame(body, bg=SURFACE, width=320)
        prop.pack(side="right", fill="y")
        prop.pack_propagate(False)
        tk.Label(prop, text="⚙️ 节点属性", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=14, pady=(12, 8))
        self._wf_prop_frame = tk.Frame(prop, bg=SURFACE)
        self._wf_prop_frame.pack(fill="both", expand=True, padx=10)
        tk.Label(prop, text="💡 点 Canvas 上的节点选中后在此编辑",
                 bg=SURFACE, fg=TEXT_DIM, font=("Segoe UI", 8),
                 wraplength=300, justify="left").pack(anchor="w", padx=12, pady=10)

        self._wf_redraw()

    # ---------- 编辑器核心 ----------
    def _wf_next_id(self, prefix="n") -> str:
        existing = {n.id for n in self._wf.nodes}; i = 1
        while f"{prefix}{i}" in existing: i += 1
        return f"{prefix}{i}"

    def _wf_redraw(self):
        c = self._wf_canvas; c.delete("all")
        if not self._wf.nodes:
            c.create_text((c.winfo_width() or 400) // 2, (c.winfo_height() or 300) // 2,
                          text="（空 workflow — 从左侧添加第一个节点吧）",
                          fill=TEXT_DIM, font=("Segoe UI", 11))
            return

        NODE_H, GAP, center_x = 56, 70, max((c.winfo_width() or 600) // 2, 300)
        pos = {}
        for i, n in enumerate(self._wf.nodes):
            y = 30 + i * (NODE_H + GAP)
            pos[n.id] = (center_x - 110, y, 220, NODE_H)

        # 连线
        for n in self._wf.nodes:
            from_pos = pos[n.id]
            for target_id in [n.goto, n.then, n.else_]:
                if not target_id or target_id not in pos: continue
                fx, fy, fw, fh = from_pos
                tx, ty, tw, th = pos[target_id]
                x1, y1, x2, y2 = fx + fw / 2, fy + fh, tx + tw / 2, ty
                mid_y = (y1 + y2) / 2
                color, label = (TEXT_DIM, "")
                if target_id == n.then: color, label = (SUCCESS, "Y")
                elif target_id == n.else_: color, label = (DANGER, "N")
                c.create_line(x1, y1, x1, mid_y, x2, mid_y, x2, y2,
                              smooth=True, width=2, fill=color,
                              arrow=tk.LAST, arrowshape=(8, 10, 3))
                if label:
                    c.create_oval(x2 - 10, mid_y - 10, x2 + 10, mid_y + 10, fill=color, outline="")
                    c.create_text(x2, mid_y, text=label, fill="#000",
                                  font=("Segoe UI Semibold", 9))

        # 节点
        for n in self._wf.nodes:
            x, y, w, h = pos[n.id]
            self._wf_draw_node(n, x, y, w, h, n.id == self._wf_selected)

        # 选中框
        if self._wf_selected and self._wf_selected in pos:
            x, y, w, h = pos[self._wf_selected]
            c.create_rectangle(x - 4, y - 4, x + w + 4, y + h + 4,
                               outline=ACCENT, width=2, dash=(4, 3))
        self._wf_show_properties()

    def _wf_draw_node(self, node, x, y, w, h, selected):
        c = self._wf_canvas; t = node.type
        color = {"call_step": ACCENT, "condition": WARNING, "loop": "#a78bfa",
                 "action": TEXT_MUTED}.get(t, TEXT_MUTED)
        fill = {"call_step": "#1a2d3e", "condition": "#2e1f1a", "loop": "#241a2e",
                "action": "#1a2238"}.get(t, SURFACE_2)
        if t == "condition":
            cx, cy = x + w / 2, y + h / 2
            c.create_polygon([cx, y, x + w, cy, cx, y + h, x, cy],
                             fill=fill, outline=color, width=2, tags=("node", node.id))
            when = (node.when or ""); when = when[:26] + ("…" if len(when) > 26 else "")
            c.create_text(cx, cy, text="◆ " + (when or node.id), fill=TEXT,
                          font=("Consolas", 8))
        elif t == "loop":
            cx, cy = x + w / 2, y + h / 2
            c.create_polygon([x + 10, y, x + w - 10, y, x + w, cy,
                              x + w - 10, y + h, x + 10, y + h, x, cy],
                             fill=fill, outline=color, width=2, tags=("node", node.id))
            c.create_text(cx, cy, text=f"⟳ {node.loop_over or node.id}",
                          fill=TEXT, font=("Segoe UI", 9))
        else:
            c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=color, width=2,
                               tags=("node", node.id))
            c.create_rectangle(x, y, x + 4, y + h, fill=color, outline="")
            title = node.desc or node.action or node.id
            if len(title) > 26: title = title[:24] + "…"
            c.create_text(x + 14, y + 18, anchor="w", text=title, fill=TEXT,
                          font=("Segoe UI Semibold", 10))
            c.create_text(x + 14, y + 38, anchor="w",
                          text=f"{t} · {(node.action or node.id)[:32]}",
                          fill=TEXT_DIM, font=("Consolas", 8))

    def _wf_canvas_click(self, event):
        c = self._wf_canvas; item = c.find_closest(event.x, event.y)[0]
        tags = c.gettags(item)
        nid = None
        for t in tags:
            if t != "node":
                nid = t; break
        self._wf_selected = nid if (nid and self._wf.find(nid)) else None
        self._wf_redraw()

    def _wf_canvas_scroll(self, event):
        self._wf_canvas.yview_scroll(int(-event.delta / 120 * 2), "units")

    # 节点增删
    def _wf_add_call_step(self):
        import bot.page_objects as _
        from bot.workflow import WorkflowNode, STEP_REGISTRY
        sid = self._wf_step_combo.get()
        if not sid: return
        desc = STEP_REGISTRY.get(sid, {}).get("desc", sid)
        nid = self._wf_next_id("n")
        self._wf.nodes.append(WorkflowNode(id=nid, type="call_step", action=sid, desc=desc))
        if len(self._wf.nodes) >= 2:
            self._wf.nodes[-2].goto = nid
        self._wf_selected = nid; self._wf_redraw()

    def _wf_add_condition(self):
        from bot.workflow import WorkflowNode
        nid = self._wf_next_id("n")
        self._wf.nodes.append(WorkflowNode(
            id=nid, type="condition", desc="条件判断",
            when="context.get('test') == True",
            then=self._wf_next_id("y"), else_=self._wf_next_id("n")))
        self._wf_selected = nid; self._wf_redraw()

    def _wf_add_loop(self):
        from bot.workflow import WorkflowNode
        nid = self._wf_next_id("n")
        self._wf.nodes.append(WorkflowNode(
            id=nid, type="loop", desc="循环", loop_over="parts", loop_var="item"))
        self._wf_selected = nid; self._wf_redraw()

    def _wf_delete_selected(self):
        if self._wf_selected:
            self._wf.remove(self._wf_selected); self._wf_selected = None; self._wf_redraw()

    def _wf_move(self, delta):
        if not self._wf_selected: return
        idx = self._wf.index_of(self._wf_selected)
        if 0 <= idx + delta < len(self._wf.nodes):
            node = self._wf.nodes.pop(idx)
            self._wf.nodes.insert(idx + delta, node); self._wf_redraw()

    def _wf_reset_default(self):
        from bot.workflow import build_default_workflow
        self._wf = build_default_workflow(); self._wf_selected = None; self._wf_redraw()

    # 文件 IO
    def _wf_new(self):
        from bot.workflow import Workflow
        self._wf = Workflow(name="未命名", desc="新建流程")
        self._wf_selected = None; self._wf_redraw()

    def _wf_open(self):
        from tkinter import filedialog
        from bot.workflow import Workflow
        p = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json")],
            initialdir=str(Path(__file__).parent / "data" / "workflows"))
        if p:
            self._wf = Workflow.load(p); self._wf_path = p; self._wf_selected = None
            self._wf_redraw()

    def _wf_save(self):
        try:
            if not self._wf_path or not Path(self._wf_path).exists():
                from tkinter import filedialog
                p = filedialog.asksaveasfilename(
                    filetypes=[("JSON", "*.json")],
                    initialdir=str(Path(__file__).parent / "data" / "workflows"),
                    initialfile=f"{self._wf.name}.json", defaultextension=".json")
                if not p: return
                self._wf_path = p
            self._wf.save(self._wf_path)
            tk.messagebox.showinfo("保存成功", f"已保存:\n{self._wf_path}")
        except Exception as e:
            tk.messagebox.showerror("保存失败", str(e))

    # 属性面板
    def _wf_show_properties(self):
        frame = self._wf_prop_frame
        for w in frame.winfo_children(): w.destroy()
        if not self._wf_selected:
            tk.Label(frame, text="（没选中节点 — 点 Canvas 上的节点选中后编辑）",
                     bg=SURFACE, fg=TEXT_DIM, font=("Segoe UI", 9),
                     wraplength=230, justify="left").pack(anchor="w", pady=10)
            return
        node = self._wf.find(self._wf_selected)
        if not node: return

        tk.Label(frame, text=f"📌 {node.id}", bg=SURFACE, fg=ACCENT,
                 font=("Consolas", 10)).pack(anchor="w")
        tk.Label(frame, text=f"类型: {node.type}", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        self._wf_prop_entries = {}
        fields = [("描述", "desc", node.desc)]
        if node.type == "call_step":
            fields += [("action", "action", node.action)]
        elif node.type == "condition":
            fields += [("when 表达式", "when", node.when),
                       ("then →", "then", node.then),
                       ("else_ →", "else_", node.else_)]
        elif node.type == "loop":
            fields += [("loop_over", "loop_over", node.loop_over),
                       ("loop_var", "loop_var", node.loop_var)]
        else:
            fields += [("selector", "selector", node.selector),
                       ("value", "value", node.value)]
        fields += [("goto →", "goto", node.goto)]

        for label, key, val in fields:
            row = tk.Frame(frame, bg=SURFACE); row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=SURFACE, fg=TEXT_MUTED,
                     font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(val or ""))
            e = tk.Entry(row, textvariable=var, bg=SURFACE_2, fg=TEXT,
                        insertbackground=TEXT, relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT, font=("Consolas", 9))
            e.pack(side="left", fill="x", expand=True, padx=4, ipady=2)
            self._wf_prop_entries[key] = var

        # ---------- call_step 参数（kwargs）：如提交上级的 提交路径 / 组别代码 ----------
        self._wf_kw_entries = {}
        if node.type == "call_step":
            tk.Label(frame, text="⚙️ 参数 kwargs（留空 = 用默认值）", bg=SURFACE,
                     fg=TEXT_MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 2))
            for k, v in (node.kwargs or {}).items():
                tk.Label(frame, text=k, bg=SURFACE, fg=TEXT_DIM,
                         font=("Consolas", 8), anchor="w").pack(anchor="w")
                var = tk.StringVar(value="" if v is None else str(v))
                tk.Entry(frame, textvariable=var, bg=SURFACE_2, fg=TEXT,
                         insertbackground=TEXT, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, font=("Consolas", 9)).pack(
                             fill="x", pady=(0, 3), ipady=2)
                self._wf_kw_entries[k] = var
            # 新增参数行
            add_row = tk.Frame(frame, bg=SURFACE); add_row.pack(fill="x", pady=(4, 0))
            self._wf_kw_new_key = tk.StringVar()
            self._wf_kw_new_val = tk.StringVar()
            tk.Entry(add_row, textvariable=self._wf_kw_new_key, bg=SURFACE_2, fg=TEXT_DIM,
                     insertbackground=TEXT, relief="flat", bd=0, width=11,
                     highlightthickness=1, highlightbackground=BORDER,
                     font=("Consolas", 8)).pack(side="left", ipady=1)
            tk.Entry(add_row, textvariable=self._wf_kw_new_val, bg=SURFACE_2, fg=TEXT_DIM,
                     insertbackground=TEXT, relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=BORDER,
                     font=("Consolas", 8)).pack(side="left", fill="x", expand=True,
                                                padx=2, ipady=1)
            tk.Button(add_row, text="＋", bg=SURFACE_2, fg=ACCENT, relief="flat", bd=0,
                      cursor="hand2", padx=5, font=("Segoe UI", 9),
                      command=lambda n=node: self._wf_add_kwarg(n)).pack(side="left")

        tk.Button(frame, text="✅ 应用修改", bg=ACCENT, fg="#000", relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=4, font=("Segoe UI Semibold", 9),
                  command=lambda n=node: self._wf_apply(n)).pack(fill="x", pady=(12, 4))

    def _wf_add_kwarg(self, node):
        k = (self._wf_kw_new_key.get() or "").strip()
        if not k:
            messagebox.showwarning("参数名不能为空", "请先填写参数名，例如 submit_path")
            return
        node.kwargs = dict(node.kwargs or {})
        node.kwargs[k] = (self._wf_kw_new_val.get() or "").strip()
        log_audit(f"流程编辑器：节点 {node.id} 新增参数 {k}")
        self._wf_show_properties()
        self._wf_redraw()

    def _wf_apply(self, node):
        for key, var in self._wf_prop_entries.items():
            val = var.get().strip()
            if key in ("then", "else_", "goto") and val == "": val = None
            setattr(node, key, val)
        if node.type == "call_step":
            node.kwargs = {k: v.get().strip() for k, v in self._wf_kw_entries.items()}
        log_audit(f"流程编辑器：应用节点 {node.id} 属性 {node.action or ''}")
        self._wf_redraw()

    def _make_log_box(self, parent):
        box = tk.Text(parent, bg="#0a0f1e", fg="#c9d1d9", font=("Consolas", 10),
                      width=40, height=12, relief="flat", bd=0, wrap="word",
                      highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill="both", expand=True, pady=(12, 0))
        # 两套 tag 名都注册，兼容 _ThemeLogRedirector 的着色逻辑
        box.tag_configure("default", foreground="#c9d1d9")
        box.tag_configure("ok", foreground=SUCCESS)
        box.tag_configure("success", foreground=SUCCESS)
        box.tag_configure("err", foreground=DANGER)
        box.tag_configure("danger", foreground=DANGER)
        box.tag_configure("warn", foreground=WARNING)
        box.tag_configure("warning", foreground=WARNING)
        box.tag_configure("info", foreground=ACCENT)
        box.tag_configure("step", foreground=ACCENT, font=("Consolas", 10, "bold"))
        box.tag_configure("cyan", foreground="#7fe3ff")
        box.configure(state="disabled")
        RUN_STREAM.bind(box)
        return box

    # ============== View 2: 单步测试 ==============
    def _build_test_view(self):
        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        tk.Label(wrap, text="🧪 单步测试", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w")
        tk.Label(wrap, text="输入报案号 → 跑完整流程（自动识别 有换件 / 无换件 分支）→ 适合开发调试；勾选演练模式只验证流程、不提交",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 14))

        # 报案号输入（仅此一个单步专属参数）
        form = tk.Frame(wrap, bg=SURFACE); form.pack(fill="x", pady=(0, 14))
        r1 = tk.Frame(form, bg=SURFACE); r1.pack(fill="x", padx=14, pady=14)
        tk.Label(r1, text="报案号", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10), width=10, anchor="w").pack(side="left")
        self.claim_var = tk.StringVar(value="RDZA2026110000501913")
        tk.Entry(r1, textvariable=self.claim_var, width=44,
                 bg=SURFACE_2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Consolas", 10),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", padx=6, ipady=4)
        self.test_run_btn = self._make_btn(r1, "▶ 连续执行", ACCENT, SURFACE_2,
                                           lambda: self._start_single(False))
        self.test_run_btn.pack(side="left", padx=(10, 0))
        self.test_step_btn = self._make_btn(r1, "⏭ 单步调试", SURFACE_2, SURFACE,
                                            lambda: self._start_single(True))
        self.test_step_btn.pack(side="left", padx=(8, 0))

        # 单步调试控制行：下一步 / 停止
        r2 = tk.Frame(form, bg=SURFACE); r2.pack(fill="x", padx=14, pady=(0, 14))
        tk.Label(r2, text="调试", bg=SURFACE, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10), width=10, anchor="w").pack(side="left")
        self.step_next_btn = self._make_btn(r2, "▶ 下一步", SUCCESS, SURFACE_2,
                                            self.on_step_next, disabled=True)
        self.step_next_btn.pack(side="left")
        self.step_stop_btn = self._make_btn(r2, "⏹ 停止", DANGER, SURFACE_2,
                                            self.on_step_stop, disabled=True)
        self.step_stop_btn.pack(side="left", padx=(8, 0))
        tk.Label(r2, text="单步调试：每个 STEP 前暂停，点『下一步』继续",
                 bg=SURFACE, fg=TEXT_MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(14, 0))

        # 演练模式：不提交、不写台账，用于验证流程而不真正影响案件
        r3 = tk.Frame(form, bg=SURFACE); r3.pack(fill="x", padx=12, pady=(0, 12))
        tk.Checkbutton(r3, text="🛡 演练模式（跳过『提交定损 / 报价完成』，不写 Excel 工作记录）",
                       variable=self.dry_run_var, bg=SURFACE, fg=TEXT_MUTED,
                       selectcolor=SURFACE_2, activebackground=SURFACE,
                       activeforeground=TEXT, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x")

        tk.Label(wrap, text="📋 执行日志", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(0, 4))
        self.log_box = self._make_log_box(wrap)

    # ============== View 4: 录制学习 ==============
    def _build_record_view(self):
        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        tk.Label(wrap, text="📼 录制学习", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w")
        tk.Label(wrap, text="打开浏览器 → 手动操作一遍 → 程序记录每个点击/输入 → 保存为流程 JSON",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
        self.rec_env_hint = tk.Label(wrap, text="", bg=BG, fg=TEXT_DIM,
                                     font=("Segoe UI", 8), anchor="w", justify="left")
        self.rec_env_hint.pack(anchor="w", pady=(0, 10))

        ctrl = tk.Frame(wrap, bg=BG); ctrl.pack(fill="x", pady=(0, 8))
        self.rec_start_btn = self._make_btn(ctrl, "📼 录制人工操作", ACCENT, SURFACE_2,
                                            self.on_record_start)
        self.rec_start_btn.pack(side="left")
        self.rec_stop_btn = self._make_btn(ctrl, "■ 停止并保存", DANGER, SURFACE_2,
                                           self.on_record_stop, disabled=True)
        self.rec_stop_btn.pack(side="left", padx=(10, 0))
        self._make_btn(ctrl, "📸 采集生产页面", SURFACE_2, BORDER,
                       self.on_capture_pages).pack(side="left", padx=(10, 0))

        ctrl2 = tk.Frame(wrap, bg=BG); ctrl2.pack(fill="x", pady=(0, 12))
        self.capture_cmp_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl2, text="采集页面时同时采集 mock，生成结构差异报告",
                       variable=self.capture_cmp_var, bg=BG, fg=TEXT_MUTED,
                       selectcolor=SURFACE, activebackground=BG, activeforeground=TEXT,
                       font=("Segoe UI", 9), bd=0, highlightthickness=0).pack(side="left")
        self.rec_snap_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl2, text="录制时保存关键页面快照（弹窗/跳转/下拉 → HTML，供事后读 DOM）",
                       variable=self.rec_snap_var, bg=BG, fg=TEXT_MUTED,
                       selectcolor=SURFACE, activebackground=BG, activeforeground=TEXT,
                       font=("Segoe UI", 9), bd=0, highlightthickness=0).pack(side="left",
                                                                             padx=(12, 0))
        self._make_btn(ctrl2, "📂 打开采集目录", SURFACE_2, BORDER,
                       self._open_capture_dir).pack(side="left", padx=(12, 0))

        tk.Label(wrap, text="🎬 实时动作预览", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(0, 4))
        self.rec_preview = tk.Text(wrap, bg="#0a0f1e", fg="#c9d1d9", font=("Consolas", 10),
                                   height=14, relief="flat", bd=0, wrap="none")
        self.rec_preview.pack(fill="both", expand=True)
        self.rec_preview.configure(state="disabled")

        tk.Label(wrap, text="📁 已保存流程", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(10, 4))
        self.rec_list = tk.Listbox(wrap, bg=SURFACE, fg=TEXT, height=5,
                                   relief="flat", bd=0, font=("Consolas", 10),
                                   selectbackground=SURFACE_2, selectforeground=ACCENT)
        self.rec_list.pack(fill="x")
        self._refresh_record_list()
        self._refresh_record_hint()

    def _refresh_record_hint(self):
        try:
            spec = self._current_spec()
        except Exception:
            return
        if hasattr(self, "rec_env_hint"):
            self.rec_env_hint.configure(
                text=f"当前环境: 门户={spec.portal} · 一车一件={spec.market}"
                     f"（跟随控制台『运行模式 / 环境』设置；选『真实循环挂机』即为生产环境）"
                     f"\n⚠️ 只在『程序打开或接管』的浏览器窗口里操作才会被记录；"
                     f"自己另开的浏览器程序默认收不到动作")

    def _refresh_record_list(self):
        if not hasattr(self, "rec_list"):
            return
        self.rec_list.delete(0, tk.END)
        wf_dir = APP_DIR / "data" / "workflows"
        if wf_dir.exists():
            for f in sorted(wf_dir.glob("*.json")):
                self.rec_list.insert(tk.END, f.name)

    def on_record_start(self):
        name = simpledialog.askstring("录制人工操作", "这次录制叫什么名字？",
                                      initialvalue="flow_recorded")
        if not name:
            return
        self._rec_name = name
        self._rec_stop_flag = threading.Event()
        self._rec_start_btn_guard()
        threading.Thread(target=self._record_thread, daemon=True).start()

    def _rec_start_btn_guard(self):
        _safe_state(self.rec_start_btn, "disabled")
        _safe_state(getattr(self, "rec_stop_btn", None), "normal")
        self._append_rec("⏺ 录制已开始，浏览器即将打开 —— 请手动操作一遍流程")

    def _append_rec(self, line):
        if not hasattr(self, "rec_preview"):
            return
        try:
            if not self.rec_preview.winfo_exists():
                return
            self.rec_preview.configure(state="normal")
            self.rec_preview.insert(tk.END, line + "\n")
            self.rec_preview.see(tk.END)
            self.rec_preview.configure(state="disabled")
        except Exception:
            pass

    def _record_thread(self):
        """人工操作录制：走统一运行器（环境跟随『运行模式』，生产/模拟同一套逻辑）"""
        from bot.runners import run_capture

        spec = self._current_spec()

        def push(line):
            self.root.after(0, lambda l=line: self._append_rec(l))

        try:
            snap = bool(getattr(self, "rec_snap_var", None) and self.rec_snap_var.get())
            asyncio.run(run_capture(self._rec_name, spec, kind="actions",
                                    duration_s=3600, stop_event=self._rec_stop_flag,
                                    snapshot_pages=snap,
                                    logger=push))
            self.root.after(0, lambda: self._append_rec(
                "✅ 录制结束 → data/capture（改进草案） 与 data/workflows（流程比对）"))
        except Exception as e:
            self.root.after(0, lambda: self._append_rec(f"❌ 录制失败: {e}"))
        finally:
            self.root.after(0, self._rec_finish_ui)

    def _rec_finish_ui(self):
        _safe_state(self.rec_start_btn, "normal")
        _safe_state(getattr(self, "rec_stop_btn", None), "disabled")
        try:
            self._refresh_record_list()
        except Exception:
            pass

    def on_record_stop(self):
        if getattr(self, "_rec_stop_flag", None):
            self._rec_stop_flag.set()
            self.rec_stop_btn.configure(state="disabled")

    # ---------- 采集生产页面结构 ----------
    def on_capture_pages(self):
        name = simpledialog.askstring("采集生产页面", "这次采集叫什么名字？",
                                      initialvalue="prod_snapshot")
        if not name:
            return
        with_mock = bool(getattr(self, "capture_cmp_var", None)
                         and self.capture_cmp_var.get())
        _safe_state(getattr(self, "rec_start_btn", None), "disabled")
        self._append_rec(f"📸 开始采集页面结构: {name}（对比 mock={with_mock}）")
        threading.Thread(target=self._capture_thread, args=(name, with_mock),
                         daemon=True).start()

    def _capture_thread(self, name, with_mock):
        from bot.runners import run_capture

        spec = self._current_spec()

        def push(line):
            self.root.after(0, lambda l=line: self._append_rec(l))

        try:
            asyncio.run(run_capture(name, spec, kind="page",
                                    with_mock_compare=with_mock, logger=push))
        except Exception as e:
            self.root.after(0, lambda: self._append_rec(f"❌ 采集失败: {e}"))
        finally:
            self.root.after(0, lambda: _safe_state(
                getattr(self, "rec_start_btn", None), "normal"))

    def _open_capture_dir(self):
        try:
            from bot.capture import capture_dir
            os.startfile(str(capture_dir()))
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ============== View 5: 流程比对 ==============
    def _build_diff_view(self):
        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        tk.Label(wrap, text="🔍 流程比对", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w")
        tk.Label(wrap, text="录制流程 vs 程序黄金标准 → LCS 对齐算出 缺失/多余/错位 的步骤",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 14))

        ctrl = tk.Frame(wrap, bg=BG); ctrl.pack(fill="x", pady=(0, 12))
        tk.Label(ctrl, text="录制流程", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 10)).pack(side="left")
        self.diff_rec_var = tk.StringVar()
        self.diff_rec_combo = ttk.Combobox(ctrl, textvariable=self.diff_rec_var, width=28,
                                           state="readonly", font=("Consolas", 10))
        self.diff_rec_combo.pack(side="left", padx=8)

        self._make_btn(ctrl, "🔄 刷新列表", SURFACE_2, BORDER,
                       self._refresh_diff_choices).pack(side="left", padx=(6, 0))
        self._make_btn(ctrl, "▶ 开始比对", ACCENT, SURFACE_2,
                       self.on_diff_run).pack(side="left", padx=(6, 0))

        self.diff_box = tk.Text(wrap, bg="#0a0f1e", fg="#c9d1d9", font=("Consolas", 10),
                                relief="flat", bd=0, wrap="word")
        self.diff_box.pack(fill="both", expand=True)
        self.diff_box.tag_configure("ok", foreground=SUCCESS)
        self.diff_box.tag_configure("err", foreground=DANGER)
        self.diff_box.tag_configure("warn", foreground=WARNING)
        self.diff_box.configure(state="disabled")
        self._refresh_diff_choices()

    def _refresh_diff_choices(self):
        wf_dir = APP_DIR / "data" / "workflows"
        names = [f.name for f in sorted(wf_dir.glob("*.json"))] if wf_dir.exists() else []
        if hasattr(self, "diff_rec_combo"):
            self.diff_rec_combo["values"] = names
            if names:
                self.diff_rec_var.set(names[0])

    def on_diff_run(self):
        from bot.recorder import get_expected_steps
        from bot.workflow_diff import diff_workflows, format_diff_report

        rec_name = self.diff_rec_var.get()
        if not rec_name:
            messagebox.showwarning("提示", "还没有录制文件，请先在 📼 录制学习 里录一遍")
            return
        try:
            data = json.loads((APP_DIR / "data" / "workflows" / rec_name)
                              .read_text(encoding="utf-8"))
            recorded = data.get("steps", [])
        except Exception as e:
            messagebox.showerror("比对失败", f"读取录制文件出错: {e}")
            return

        result = diff_workflows(get_expected_steps(), recorded)
        report = format_diff_report(result)

        self.diff_box.configure(state="normal")
        self.diff_box.delete("1.0", tk.END)
        for line in report.split("\n"):
            tag = ("ok" if line.startswith("✅") else
                   "err" if line.startswith("❌") else
                   "warn" if line.startswith("⚠️") else "ok")
            self.diff_box.insert(tk.END, line + "\n", tag)
        self.diff_box.configure(state="disabled")

    # ============== View 6: 日志中心 ==============
    _LOG_LABELS = list(CATEGORY_LABELS.values()) + ["全部"]

    # ============== View 7: 工作记录（业务台账，给用户看）==============
    _WL_DETAIL_COLS = [
        ("time", "处理时间", 140),
        ("claim", "报案号", 175),
        ("plate", "车牌号", 90),
        ("vin", "VIN", 165),
        ("parts", "零件数", 60),
        ("ok", "合规数", 60),
        ("over", "超价数", 60),
        ("fixed", "改了报价", 80),
        ("fixed_n", "改价零件", 75),
        ("result", "处理结果", 100),
        ("step", "跳过步骤", 90),
        ("note", "备注", 240),
    ]
    _WL_STATS_COLS = [
        ("day", "日期", 100),
        ("total", "处理总数", 80),
        ("done", "全流程完成", 90),
        ("skip", "跳过", 70),
        ("blocked", "需人工", 70),
        ("fixed_case", "修改报价案件", 100),
        ("fixed_parts", "修改零件数", 90),
        ("parts", "零件总数", 80),
        ("over", "超市场价零件", 100),
    ]

    def _build_worklog_view(self):
        from bot.ledger import current_ledger_path

        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        head = tk.Frame(wrap, bg=BG); head.pack(fill="x")
        tk.Label(head, text="📒 工作记录", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(head, text="只记录业务处理明细，不含程序运行日志",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=10)
        tk.Label(head, text=f"文件: {current_ledger_path()}", bg=BG, fg=TEXT_DIM,
                 font=("Consolas", 8)).pack(side="right")

        bar = tk.Frame(wrap, bg=BG); bar.pack(fill="x", pady=(8, 6))
        self._make_btn(bar, "🔄 刷新", SURFACE_2, BORDER,
                       self._refresh_worklog).pack(side="left")
        self._make_btn(bar, "📊 打开 Excel", SURFACE_2, BORDER,
                       self._open_ledger_file).pack(side="left", padx=8)
        self._make_btn(bar, "📂 打开目录", SURFACE_2, BORDER,
                       self._open_ledger_dir).pack(side="left")

        st = ttk.Style()
        st.configure("WL.Treeview", background=SURFACE, fieldbackground=SURFACE,
                     foreground=TEXT, rowheight=24, borderwidth=0,
                     font=("Microsoft YaHei UI", 9))
        st.configure("WL.Treeview.Heading", background=SURFACE_2, foreground=TEXT_MUTED,
                     font=("Segoe UI Semibold", 9), relief="flat")
        st.map("WL.Treeview", background=[("selected", SURFACE_2)],
               foreground=[("selected", ACCENT)])

        tk.Label(wrap, text="📋 处理明细（每笔报案号一行）", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(6, 2))
        self.wl_detail = self._make_wl_tree(wrap, self._WL_DETAIL_COLS, height=13)

        tk.Label(wrap, text="📊 统计汇总（按日期，自动累计）", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(10, 2))
        self.wl_stats = self._make_wl_tree(wrap, self._WL_STATS_COLS, height=7)

        self._refresh_worklog()

    def _make_wl_tree(self, parent, cols, height):
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="both", expand=True)
        keys = [c[0] for c in cols]
        tree = ttk.Treeview(box, columns=keys, show="headings", height=height,
                            style="WL.Treeview", selectmode="browse")
        for key, title, width in cols:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=50, anchor="w", stretch=False)
        ysb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        tree.tag_configure("done", foreground=SUCCESS)
        tree.tag_configure("skip", foreground=WARNING)
        tree.tag_configure("blocked", foreground=DANGER)
        tree.tag_configure("total", foreground=ACCENT)
        return tree

    def _refresh_worklog(self):
        from bot.ledger import load_records
        if not hasattr(self, "wl_detail"):
            return
        for tree in (self.wl_detail, self.wl_stats):
            for item in tree.get_children():
                tree.delete(item)
        try:
            main_rows, stats_rows = load_records()
        except Exception as exc:
            print(f"[!] 读取工作记录失败: {exc}")
            return

        for row in reversed(main_rows):          # 最新在最上面
            vals = [("" if v is None else str(v)) for v in row]
            vals += [""] * (len(self._WL_DETAIL_COLS) - len(vals))
            result = vals[9]
            tag = ("done" if "完成" in result
                   else "skip" if "跳过" in result
                   else "blocked")
            self.wl_detail.insert("", "end", values=vals[:len(self._WL_DETAIL_COLS)], tags=(tag,))

        for row in stats_rows:
            vals = [("" if v is None else str(v)) for v in row]
            vals += [""] * (len(self._WL_STATS_COLS) - len(vals))
            tag = ("total",) if vals[0] == "合计" else ()
            self.wl_stats.insert("", "end", values=vals[:len(self._WL_STATS_COLS)], tags=tag)

    def _open_ledger_file(self):
        from bot.ledger import current_ledger_path
        path = current_ledger_path()
        if not path.exists():
            messagebox.showinfo("工作记录", "本月还没有工作记录，跑一笔案件后会自动生成。")
            return
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror("工作记录", f"打开失败: {exc}")

    def _open_ledger_dir(self):
        from bot.ledger import current_ledger_path
        d = current_ledger_path().parent
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except Exception as exc:
            messagebox.showerror("工作记录", f"打开失败: {exc}")

    # ============== View: 数据管理（本地零件价格库）==============
    _DATA_COLS = [
        ("vin", "VIN", 170),
        ("code", "零件编码", 115),
        ("name", "零件名称", 170),
        ("price", "价格(元)", 85),
        ("source", "来源", 90),
        ("status", "状态", 70),
        ("car", "品牌/车型", 130),
        ("fetched", "缓存时间", 135),
        ("expires", "到期时间", 135),
    ]

    def _open_price_store(self):
        from bot.config import PRICE_CACHE_DB_NAME
        from bot.logging_system import app_data_dir
        from bot.pricing import PriceStore
        return PriceStore(app_data_dir() / PRICE_CACHE_DB_NAME)

    def _build_data_view(self):
        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        head = tk.Frame(wrap, bg=BG); head.pack(fill="x")
        tk.Label(head, text="🗄 数据管理", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(head, text="本地零件价格库 · 统计 + 条件查询", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=10)
        db_text = ""
        try:
            from bot.config import PRICE_CACHE_DB_NAME
            from bot.logging_system import app_data_dir
            db_text = str(app_data_dir() / PRICE_CACHE_DB_NAME)
        except Exception:
            pass
        tk.Label(head, text=db_text, bg=BG, fg=TEXT_DIM,
                 font=("Consolas", 8)).pack(side="right")

        # ---- 统计卡片 ----
        stats = tk.Frame(wrap, bg=BG); stats.pack(fill="x", pady=(10, 0))
        self.data_stat_vars = {}
        cards = [("vehicles", "🚗 车辆数"), ("prices", "📦 报价记录"),
                 ("fresh", "✅ 有效"), ("expired", "⏳ 已过期"),
                 ("hit", "💾 缓存命中"), ("remote", "🌐 远程查询"),
                 ("rate", "🎯 命中率")]
        for i, (key, title) in enumerate(cards):
            card = tk.Frame(stats, bg=SURFACE, highlightthickness=1,
                            highlightbackground=BORDER)
            card.grid(row=0, column=i, sticky="ew", padx=(0, 6))
            stats.columnconfigure(i, weight=1)
            tk.Label(card, text=title, bg=SURFACE, fg=TEXT_MUTED,
                     font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(6, 0))
            var = tk.StringVar(value="—")
            tk.Label(card, textvariable=var, bg=SURFACE, fg=ACCENT,
                     font=("Segoe UI Semibold", 15)).pack(anchor="w", padx=10, pady=(0, 6))
            self.data_stat_vars[key] = var

        self.data_src_var = tk.StringVar(value="来源分布：—")
        tk.Label(wrap, textvariable=self.data_src_var, bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))

        # ---- 价格有效期（超过则回源更新）----
        valid_bar = tk.Frame(wrap, bg=BG); valid_bar.pack(fill="x", pady=(6, 0))
        tk.Label(valid_bar, text="价格有效期", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        try:
            _s = self._open_price_store()
            try:
                _days_now = _s.get_valid_days(PRICE_CACHE_VALID_DAYS)
            finally:
                _s.close()
        except Exception:
            _days_now = PRICE_CACHE_VALID_DAYS
        self.data_valid_var = tk.StringVar(value=str(_days_now))
        valid_entry = tk.Entry(valid_bar, textvariable=self.data_valid_var, width=6,
                               bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                               relief="flat", bd=0)
        valid_entry.pack(side="left", padx=(6, 4), ipady=3)
        valid_entry.bind("<Return>", lambda _e: self._save_valid_days())
        tk.Label(valid_bar,
                 text="天，超过则重新查询外部端口（主机厂通常 90 天更新一次价格）",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left")
        self._make_btn(valid_bar, "💾 保存", SURFACE_2, BORDER,
                       self._save_valid_days).pack(side="left", padx=8)
        self.data_valid_hint = tk.StringVar(value="")
        tk.Label(valid_bar, textvariable=self.data_valid_hint, bg=BG, fg=SUCCESS,
                 font=("Segoe UI", 9)).pack(side="left")

        # ---- 条件查询 ----
        bar = tk.Frame(wrap, bg=BG); bar.pack(fill="x", pady=(8, 6))
        self.data_vin_var = tk.StringVar()
        self.data_code_var = tk.StringVar()
        self.data_name_var = tk.StringVar()
        self.data_src_pick = tk.StringVar(value="全部来源")
        self.data_status_pick = tk.StringVar(value="全部状态")

        def _entry(label, var, width):
            tk.Label(bar, text=label, bg=BG, fg=TEXT_MUTED,
                     font=("Segoe UI", 9)).pack(side="left")
            e = tk.Entry(bar, textvariable=var, width=width, bg=SURFACE, fg=TEXT,
                         insertbackground=TEXT, relief="flat", bd=0)
            e.pack(side="left", padx=(4, 10), ipady=4)
            e.bind("<Return>", lambda _e: self._refresh_data())
            return e

        _entry("VIN", self.data_vin_var, 20)
        _entry("零件编码", self.data_code_var, 14)
        _entry("零件名称", self.data_name_var, 14)
        self.data_src_combo = ttk.Combobox(bar, textvariable=self.data_src_pick, width=12,
                                           state="readonly", values=["全部来源"],
                                           font=("Segoe UI", 9))
        self.data_src_combo.pack(side="left", padx=(0, 8))
        ttk.Combobox(bar, textvariable=self.data_status_pick, width=10,
                     state="readonly", values=["全部状态", "有效", "已过期"],
                     font=("Segoe UI", 9)).pack(side="left", padx=(0, 10))
        self._make_btn(bar, "🔎 查询", ACCENT, SURFACE_2,
                       self._data_query).pack(side="left")
        self._make_btn(bar, "✖ 重置", SURFACE_2, BORDER,
                       self._data_reset).pack(side="left", padx=6)
        self._make_btn(bar, "🔄 刷新", SURFACE_2, BORDER,
                       self._refresh_data).pack(side="left")

        # ---- 结果表 ----
        st = ttk.Style()
        st.configure("DM.Treeview", background=SURFACE, fieldbackground=SURFACE,
                     foreground=TEXT, rowheight=23, borderwidth=0,
                     font=("Microsoft YaHei UI", 9))
        st.configure("DM.Treeview.Heading", background=SURFACE_2, foreground=TEXT_MUTED,
                     font=("Segoe UI Semibold", 9), relief="flat")
        st.map("DM.Treeview", background=[("selected", SURFACE_2)],
               foreground=[("selected", ACCENT)])

        box = tk.Frame(wrap, bg=BG); box.pack(fill="both", expand=True)
        keys = [c[0] for c in self._DATA_COLS]
        self.data_tree = ttk.Treeview(box, columns=keys, show="headings",
                                      style="DM.Treeview", selectmode="browse")
        for key, title, width in self._DATA_COLS:
            self.data_tree.heading(key, text=title, anchor="w")
            self.data_tree.column(key, width=width, minwidth=60, anchor="w", stretch=False)
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.data_tree.yview)
        xsb = ttk.Scrollbar(box, orient="horizontal", command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.data_tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.data_tree.tag_configure("fresh", foreground=SUCCESS)
        self.data_tree.tag_configure("expired", foreground=WARNING)

        # ---- 分页 / 导出 ----
        self.data_page = 1
        self.data_page_size = tk.StringVar(value="500")
        self.data_page_var = tk.StringVar(value="第 1 / 1 页")
        foot = tk.Frame(wrap, bg=BG); foot.pack(fill="x", pady=(6, 0))
        self.data_count_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.data_count_var, bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(foot, text="每页", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 4))
        size_combo = ttk.Combobox(foot, textvariable=self.data_page_size, width=6,
                                  state="readonly",
                                  values=["100", "200", "500", "1000", "2000"],
                                  font=("Segoe UI", 9))
        size_combo.pack(side="left")
        size_combo.bind("<<ComboboxSelected>>", lambda _e: self._data_query())
        self._make_btn(foot, "📤 导出 Excel", SURFACE_2, BORDER,
                       self._data_export).pack(side="right")
        self._make_btn(foot, "▶ 下一页", SURFACE_2, BORDER,
                       self._data_next).pack(side="right", padx=6)
        tk.Label(foot, textvariable=self.data_page_var, bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=4)
        self._make_btn(foot, "◀ 上一页", SURFACE_2, BORDER,
                       self._data_prev).pack(side="right")

        self._refresh_data()

    def _data_reset(self):
        self.data_vin_var.set("")
        self.data_code_var.set("")
        self.data_name_var.set("")
        self.data_src_pick.set("全部来源")
        self.data_status_pick.set("全部状态")
        self._data_query()

    def _data_query(self):
        """查询 / 改每页条数 / 重置 → 回到第 1 页"""
        self.data_page = 1
        self._refresh_data()

    def _data_prev(self):
        if getattr(self, "data_page", 1) > 1:
            self.data_page -= 1
            self._refresh_data()

    def _data_next(self):
        self.data_page = getattr(self, "data_page", 1) + 1
        self._refresh_data()

    def _data_export(self):
        """把当前筛选条件下的全部记录导出 Excel（不只当前页）"""
        from tkinter import filedialog
        filters = dict(getattr(self, "_data_filters", {}) or {})
        name = f"零件价格_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出零件价格数据", defaultextension=".xlsx",
            initialfile=name, filetypes=[("Excel 工作簿", "*.xlsx")])
        if not path:
            return
        self.data_count_var.set("正在导出…")
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        store = None
        try:
            store = self._open_price_store()
            n = export_parts_xlsx(path, store.iter_parts(**filters))
            self.data_count_var.set(f"已导出 {n} 条 → {path}")
            print(f"  [✓] 数据管理：已导出 {n} 条 → {path}")
        except Exception as exc:
            self.data_count_var.set("导出失败")
            messagebox.showerror("导出失败", f"{exc}", parent=self.root)
        finally:
            try:
                if store is not None:
                    store.close()
            except Exception:
                pass

    def _save_valid_days(self):
        """保存价格有效期：立即重算已缓存记录的到期时间，挂机循环下次判断即用新值"""
        raw = (self.data_valid_var.get() or "").strip()
        try:
            days = int(raw)
        except ValueError:
            messagebox.showwarning("输入有误", "有效期请填写整数天数，例如 90", parent=self.root)
            return
        if not 1 <= days <= 3650:
            messagebox.showwarning("输入有误", "有效期需在 1 ~ 3650 天之间", parent=self.root)
            return
        store = None
        try:
            store = self._open_price_store()
            n = store.set_valid_days(days)
        except Exception as exc:
            messagebox.showerror("保存失败", f"{exc}", parent=self.root)
            return
        finally:
            try:
                if store is not None:
                    store.close()
            except Exception:
                pass
        # 运行中的单例服务立即生效；config 常量同步，供日志展示新值
        try:
            from bot.pricing import get_price_service
            get_price_service().valid_days = days
        except Exception:
            pass
        try:
            from bot import config as _cfg
            _cfg.PRICE_CACHE_VALID_DAYS = days
        except Exception:
            pass
        self.data_valid_var.set(str(days))
        self.data_valid_hint.set(f"已保存：{days} 天，重算 {n} 条历史记录")
        try:
            log_audit(f"价格有效期改为 {days} 天（重算 {n} 条历史记录）")
        except Exception:
            pass
        self._refresh_data()

    def _refresh_data(self):
        from bot.pricing.service import _SOURCE_LABELS
        if not hasattr(self, "data_tree"):
            return
        src_by_label = {"全部来源": ""}
        status_map = {"全部状态": "", "有效": "fresh", "已过期": "expired"}
        try:
            size = max(1, int(self.data_page_size.get() or 500))
        except Exception:
            size = 500
        page = max(1, int(getattr(self, "data_page", 1)))
        store = None
        try:
            store = self._open_price_store()
            st = store.stats()
            src_rows = store.source_stats()
            for code, _cnt in src_rows:
                src_by_label[_SOURCE_LABELS.get(code, code or "未知")] = code
            self.data_src_combo["values"] = list(src_by_label.keys())
            if self.data_src_pick.get() not in src_by_label:
                self.data_src_pick.set("全部来源")
            filters = dict(
                vin=self.data_vin_var.get(),
                part_code=self.data_code_var.get(),
                part_name=self.data_name_var.get(),
                source=src_by_label.get(self.data_src_pick.get(), ""),
                status=status_map.get(self.data_status_pick.get(), ""))
            rows, total = store.search_parts(**filters, limit=size,
                                            offset=(page - 1) * size)
            pages = max(1, (total + size - 1) // size)
            if page > pages:      # 翻过头（或筛选后总数变少）→ 落到最后一页
                page = pages
                rows, total = store.search_parts(**filters, limit=size,
                                                 offset=(page - 1) * size)
        except Exception as exc:
            print(f"[!] 读取本地价格库失败: {exc}")
            self.data_count_var.set("读取失败")
            return
        finally:
            try:
                if store is not None:
                    store.close()
            except Exception:
                pass

        self.data_page = page
        self._data_filters = filters
        self.data_stat_vars["vehicles"].set(str(st.vehicles))
        self.data_stat_vars["prices"].set(str(st.prices))
        self.data_stat_vars["fresh"].set(str(st.fresh_prices))
        self.data_stat_vars["expired"].set(str(max(0, st.prices - st.fresh_prices)))
        self.data_stat_vars["hit"].set(str(st.hit_count))
        self.data_stat_vars["remote"].set(str(st.remote_count))
        self.data_stat_vars["rate"].set(f"{st.hit_rate * 100:.1f}%")
        self.data_src_var.set("来源分布：" + ("、".join(
            f"{_SOURCE_LABELS.get(c, c or '未知')} {n}" for c, n in src_rows) or "暂无记录"))

        for item in self.data_tree.get_children():
            self.data_tree.delete(item)
        for r in rows:
            car = " ".join(x for x in (r["brand"], r["model"]) if x)
            price = "" if r["price"] is None else f"{r['price']:g}"
            self.data_tree.insert("", "end", values=(
                r["vin"], r["part_code"], r["part_name"], price,
                _SOURCE_LABELS.get(r["source"], r["source"]),
                "有效" if r["fresh"] else "已过期", car,
                r["fetched_at"].replace("T", " "),
                r["expires_at"].replace("T", " ")),
                tags=("fresh" if r["fresh"] else "expired",))
        self.data_page_var.set(f"第 {page} / {pages} 页")
        self.data_count_var.set(f"共 {total} 条 · 本页 {len(rows)} 条")

    def _build_log_view(self):
        wrap = tk.Frame(self.content, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=(14, 4))

        head = tk.Frame(wrap, bg=BG); head.pack(fill="x")
        tk.Label(head, text="📄 日志中心", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(side="left")
        tk.Label(head, text=version_text(), bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=10)
        tk.Label(head, text=f"目录: {get_log_dir()}", bg=BG, fg=TEXT_DIM,
                 font=("Consolas", 8)).pack(side="right")

        tk.Label(wrap, text="系统 / 运行 / 操作 / 错误 四类日志按天写入 data/logs/，这里查看最新尾部（每 5 秒自动刷新）",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        bar = tk.Frame(wrap, bg=BG); bar.pack(fill="x", pady=(0, 8))
        self.log_cat_var = tk.StringVar(value=CATEGORY_LABELS["run"])
        combo = ttk.Combobox(bar, textvariable=self.log_cat_var, width=12, state="readonly",
                             values=self._LOG_LABELS, font=("Segoe UI", 10))
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_log_view())

        tk.Label(bar, text="行数", bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=(12, 4))
        self.log_lines_var = tk.StringVar(value="300")
        tk.Entry(bar, textvariable=self.log_lines_var, width=6, bg=SURFACE_2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Consolas", 10),
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", ipady=2)

        self._make_btn(bar, "🔄 刷新", SURFACE_2, BORDER,
                       self._refresh_log_view).pack(side="left", padx=8)
        self._make_btn(bar, "📂 打开日志目录", SURFACE_2, BORDER,
                       self._open_log_dir).pack(side="left")

        self.log_view_box = tk.Text(wrap, bg="#0a0f1e", fg="#c9d1d9", font=("Consolas", 9),
                                    relief="flat", bd=0, wrap="none")
        self.log_view_box.pack(fill="both", expand=True, pady=(4, 0))
        self.log_view_box.tag_configure("default", foreground="#c9d1d9")
        self.log_view_box.tag_configure("danger", foreground=DANGER)
        self.log_view_box.tag_configure("warning", foreground=WARNING)
        self.log_view_box.tag_configure("success", foreground=SUCCESS)
        self.log_view_box.tag_configure("step", foreground=ACCENT)
        self.log_view_box.configure(state="disabled")

        self._refresh_log_view()
        self._schedule_log_refresh()

    def _log_cat_key(self):
        label = self.log_cat_var.get() if hasattr(self, "log_cat_var") else ""
        if label == "全部":
            return None
        for k, v in CATEGORY_LABELS.items():
            if v == label:
                return k
        return "run"

    def _refresh_log_view(self):
        box = self._ui("log_view_box")
        if box is None:
            return
        try:
            lines = max(20, int(self.log_lines_var.get() or 300))
        except Exception:
            lines = 300
        cat = self._log_cat_key()
        if cat is None:
            text = "\n".join(
                f"===== {CATEGORY_LABELS[c]}（{c}）=====\n{tail(c, max(20, lines // 4))}"
                for c in CATEGORY_LABELS)
        else:
            text = tail(cat, lines)

        box.configure(state="normal")
        box.delete("1.0", tk.END)
        for line in text.split("\n"):
            if "ERROR" in line:
                tag = "danger"
            elif "WARNING" in line:
                tag = "warning"
            elif "STEP " in line:
                tag = "step"
            elif "✅" in line or "💾" in line:
                tag = "success"
            else:
                tag = "default"
            box.insert(tk.END, line + "\n", tag)
        box.see(tk.END)
        box.configure(state="disabled")

    def _open_log_dir(self):
        try:
            os.startfile(str(get_log_dir()))
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _open_compare_dir(self):
        """运行记录 + 对比报告目录（data/compare）"""
        try:
            from bot.core.records import records_dir
            os.startfile(str(records_dir()))
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _schedule_log_refresh(self):
        if getattr(self, "current_view", "") != "logs":
            return
        self._refresh_log_view()
        self.root.after(5000, self._schedule_log_refresh)

    # ============== 运行控制 ==============
    def _clear_log(self):
        box = self._ui("log_box")
        if box is None:
            return
        box.configure(state="normal")
        box.delete("1.0", tk.END)
        box.configure(state="disabled")

    def _tolerance(self):
        """价格容忍度：绝对金额（元），如 2 = 超出市场价 2 元以内不算超"""
        try:
            return max(0.0, float(self.tol_var.get().strip() or 0))
        except Exception:
            return 0.0

    def _market_threshold(self):
        """本地价阈值：低于此值的零件不查一车一件；0 = 全部查询"""
        try:
            return max(0.0, float(self.market_thresh_var.get().strip() or 0))
        except Exception:
            return 0.0

    def _apply_market_tuning(self):
        """把控制台『减速参数 / 查询配额』写进 bot.config（运行时生效，无需重新打包）"""
        import bot.config as bot_cfg

        def _num(var, default, lo=0.0):
            try:
                v = float(str(var.get()).strip())
            except Exception:
                return default
            return v if v >= lo else default

        vin_min = _num(self.vin_delay_min_var, bot_cfg.MARKET_VIN_DELAY_MIN)
        vin_max = _num(self.vin_delay_max_var, bot_cfg.MARKET_VIN_DELAY_MAX)
        part_min = _num(self.part_delay_min_var, bot_cfg.MARKET_PART_DELAY_MIN)
        part_max = _num(self.part_delay_max_var, bot_cfg.MARKET_PART_DELAY_MAX)
        claim_cd = _num(self.claim_cd_var, bot_cfg.MARKET_CLAIM_COOLDOWN)
        quota_short = int(_num(self.quota_short_var, bot_cfg.MARKET_QUOTA_MAX_SHORT, 0))
        quota_long = int(_num(self.quota_long_var, bot_cfg.MARKET_QUOTA_MAX_LONG, 0))
        if vin_max < vin_min:
            vin_min, vin_max = vin_max, vin_min
        if part_max < part_min:
            part_min, part_max = part_max, part_min

        bot_cfg.MARKET_VIN_DELAY_MIN = vin_min
        bot_cfg.MARKET_VIN_DELAY_MAX = vin_max
        bot_cfg.MARKET_PART_DELAY_MIN = part_min
        bot_cfg.MARKET_PART_DELAY_MAX = part_max
        bot_cfg.MARKET_CLAIM_COOLDOWN = claim_cd
        bot_cfg.MARKET_QUOTA_MAX_SHORT = quota_short if quota_short > 0 else 999999   # 0 = 不限
        bot_cfg.MARKET_QUOTA_MAX_LONG = quota_long if quota_long > 0 else 999999
        log_run(f"一车一件减速参数: VIN {vin_min:g}~{vin_max:g}s | 零件 {part_min:g}~{part_max:g}s | "
                f"案件间 {claim_cd:g}s | 配额 {bot_cfg.MARKET_QUOTA_MAX_SHORT} 次/10min、"
                f"{bot_cfg.MARKET_QUOTA_MAX_LONG} 次/60min")
        self._save_ui_config()   # 参数随手存档，下次启动还是这套值

    def _poll_interval(self):
        """循环挂机刷新间隔（秒）：没新案件时按此间隔持续刷新等待"""
        try:
            return max(1.0, float(self.poll_var.get().strip() or 10))
        except Exception:
            return 10.0

    def _transfer_target(self):
        val = self.transfer_var.get().strip() if hasattr(self, "transfer_var") else ""
        return val or TRANSFER_TARGET_DEFAULT

    # ---------- 运行模式 ----------
    def _mode_key(self) -> str:
        return MODE_KEYS.get((self.mode_var.get() or "").strip(), "mock_loop")

    def _refresh_mode_hint(self):
        label = getattr(self, "mode_hint", None)
        if label is None or not label.winfo_exists():
            return
        label.configure(text=MODE_HINTS.get(self._mode_key(), ""))

    def _on_mode_change(self):
        """切换运行模式时同步环境开关（循环模式自带环境，单步/人工沿用『环境』区设置）"""
        mode = self._mode_key()
        if mode == "mock_loop":
            self.portal_real_var.set(False)
            self.market_real_var.set(False)
            self._refresh_portal_hint()
        elif mode == "real_loop":
            self.portal_real_var.set(True)
            self.market_real_var.set(True)
            self._refresh_portal_hint()
        log_audit(f"运行模式切换为 {MODE_LABELS.get(mode, mode)}")
        self._refresh_mode_hint()

    def _current_spec(self):
        """把『运行模式 + 环境开关』折算成 EnvSpec（四种模式共用）"""
        from bot.envs import EnvSpec
        mode = self._mode_key()
        if mode == "real_loop":
            return EnvSpec.real()
        if mode == "mock_loop":
            return EnvSpec.mock()
        return EnvSpec.from_flags(use_mock_portal=not self.portal_real_var.get(),
                                  use_mock_market=not self.market_real_var.get())

    def _begin_run(self, status_text: str):
        self.is_running = True
        self._update_button_states()
        self._set_status(status_text, ACCENT)

    def _end_run(self, status_text: str = "就绪", color: str = SUCCESS):
        self.is_running = False
        self._update_button_states()
        self._set_status(status_text, color)

    # ========== 三按钮状态机 ==========
    #   start_btn : 浏览器生命周期（开始/停止挂机）
    #   scan_btn  : 业务循环启停（开始/停止扫描）
    #   pause_btn : 仅暂停/恢复业务动作（扫描任务仍在，浏览器不关）
    @staticmethod
    def _btn_colors(text):
        """▶ → SUCCESS 绿+深字；⏸ → WARNING 橙+深字；◼ → DANGER 红+浅字"""
        if text.startswith("▶"):
            return SUCCESS, "#00230f"
        if text.startswith("⏸"):
            return WARNING, "#2a1a00"
        return DANGER, TEXT

    def _update_button_states(self):
        start = self._ui("start_btn")
        scan = self._ui("scan_btn")
        pause = self._ui("pause_btn")
        if start is None or scan is None:
            return
        browser_open = self._loop_env is not None and self._loop_env.started
        scanning = bool(self._scan_active)
        paused = self._pause_event is not None and self._pause_event.is_set()

        start_text = "◼ 停止挂机" if browser_open else "▶ 开始挂机"
        scan_text = "◼ 停止扫描" if scanning else "▶ 开始扫描"
        pause_text = "▶ 恢复" if paused else "⏸ 暂停"

        start_bg, start_fg = self._btn_colors(start_text)
        start.configure(text=start_text, bg=start_bg, fg=start_fg, state="normal")
        scan_bg, scan_fg = self._btn_colors(scan_text)
        scan.configure(text=scan_text, bg=scan_bg, fg=scan_fg,
                       state="normal" if browser_open else "disabled")

        if pause is not None:
            pause_bg, pause_fg = self._btn_colors(pause_text)
            pause.configure(text=pause_text, bg=pause_bg, fg=pause_fg,
                            state="normal" if scanning else "disabled")

        # 单步/人工测试运行中禁用挂机相关按钮
        if self.is_running and self._loop_env is None:
            start.configure(state="disabled")
            scan.configure(state="disabled")
            if pause is not None:
                pause.configure(state="disabled")

    def _ensure_loop_thread(self):
        """启动持久 asyncio loop 线程（只启一次，后续复用）"""
        if self._loop_loop_thread is not None and self._loop_loop_thread.is_alive():
            return
        import asyncio as _asyncio

        def run():
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            self._loop_loop = loop
            log_run("[internal] 挂机持久 loop 已启动")
            loop.run_forever()
            try:
                loop.close()
            except Exception:
                pass

        self._loop_loop_thread = threading.Thread(target=run, daemon=True)
        self._loop_loop_thread.start()
        # 等 loop 真的起来
        import time as _t
        for _ in range(50):
            if self._loop_loop is not None:
                break
            _t.sleep(0.05)

    # ---------- 回调 1: start_btn（▶ 开始挂机 / ◼ 停止挂机）----------
    def on_start(self):
        if self.is_running and self._loop_env is None:
            # 单步/人工测试正在跑，不允许同时挂机
            messagebox.showinfo("提示", "单步调试/人工测试正在运行，如需挂机请先停止当前任务")
            return

        if self._loop_env is None or not self._loop_env.started:
            # ====== 开始挂机 ======
            self._open_browser_and_wait_login()
        else:
            # ====== 停止挂机：先让扫描停（stop_requested），再关浏览器 ======
            self._close_browser()

    # ---------- 回调 2: scan_btn（▶ 开始扫描 / ◼ 停止扫描）----------
    def on_scan_button(self):
        """只负责业务循环的启停，不承担暂停语义"""
        if self._loop_env is None or not self._loop_env.started:
            messagebox.showinfo("提示", "请先点『▶ 开始挂机』拉起浏览器并登录两个系统")
            return
        if self._scan_active:
            self._stop_scan()
        else:
            self._resume_or_start_scan()

    # ---------- 回调 3: pause_btn（⏸ 暂停 / ▶ 恢复）----------
    def on_pause_button(self):
        """只暂停/恢复业务动作：扫描任务与浏览器都保持不动"""
        if not self._scan_active or self._pause_event is None:
            return
        if self._pause_event.is_set():
            self._pause_event.clear()
            self._set_status("▶ 已恢复 · 继续自动挂机中", ACCENT)
            log_run("▶ 已恢复扫描（暂停期间的人工操作已保留）")
            log_audit("点击『▶ 恢复』（pause_event.clear）")
        else:
            self._pause_event.set()
            self._set_status("⏸ 暂停已生效 · 当前案件处理完后停 · 浏览器保持开启", WARNING)
            log_run("⏸️ 暂停指令已发出：当前案件处理完后停止取新案件（浏览器不关）")
            log_run("   👉 现在可以在浏览器里自由查询/操作，完成后点『▶ 恢复』继续")
            log_audit("点击『⏸ 暂停』（pause_event.set）")
        self._update_button_states()

    # ---------- 具体动作：拉起浏览器 / 关浏览器 / 开始扫描 / 停止扫描 ----------
    def _open_browser_and_wait_login(self):
        """拉起两个门户，保持登录状态，等待用户手动登录后再点『开始扫描』"""
        from bot.main import DEFAULT_CLAIM_NO
        from bot.runners.loop import open_loop_browser
        from bot.runtime import clear_stop
        import asyncio as _asyncio

        self._apply_browser_config()
        spec = self._current_spec()
        clear_stop()
        self._ensure_loop_thread()
        assert self._loop_loop is not None

        self._clear_log()
        self._set_status("浏览器启动中，请稍候...", TEXT_MUTED)
        self.start_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")

        log_audit(f"点击『开始挂机』（{spec.label}）→ 拉起浏览器等待登录")

        def run():
            try:
                fut = _asyncio.run_coroutine_threadsafe(
                    open_loop_browser(spec, headless=False),
                    self._loop_loop)
                env = fut.result(timeout=120)
                self._loop_env = env
                self.root.after(0, lambda: self._on_browser_opened(spec))
            except Exception as e:
                log_error("拉起浏览器失败", e)
                self.root.after(0, self._on_browser_failed)

        threading.Thread(target=run, daemon=True).start()

    def _on_browser_opened(self, spec):
        self._set_status("两个门户已打开 ✅ 请手动登录两个系统，然后点『▶ 开始扫描』", WARNING)
        log_run(f"✅ 浏览器已拉起：理赔门户 + 一车一件 | {spec.label}")
        print(f"\n  🔐 浏览器已拉起，请在理赔门户和一车一件里手动登录账号，然后点击『▶ 开始扫描』")
        self._update_button_states()

    def _on_browser_failed(self):
        self._loop_env = None
        self._scan_active = False
        self._update_button_states()
        self._set_status("浏览器启动失败", DANGER)

    def _close_browser(self):
        """停止挂机：先暂停扫描 + stop_requested，等 worker 退出，再真正关浏览器"""
        from bot.runners.loop import close_loop_browser
        from bot.runtime import request_stop
        import asyncio as _asyncio

        log_audit("点击『◼ 停止挂机』")
        request_stop()
        # 让 pause_event.clear()（即使暂停也立刻能响应 stop_requested）
        if self._pause_event is not None and self._pause_event.is_set():
            self._pause_event.clear()
        self.start_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self._set_status("正在关闭浏览器...", TEXT_MUTED)

        def run():
            try:
                # 等扫描任务先被 stop_requested 中断（最多 10s）
                if self._loop_scan_task is not None:
                    try:
                        self._loop_scan_task.result(timeout=10)
                    except Exception:
                        pass
                    self._loop_scan_task = None
                fut = _asyncio.run_coroutine_threadsafe(
                    close_loop_browser(self._loop_env),
                    self._loop_loop)
                fut.result(timeout=30)
            except Exception as e:
                log_error("关闭浏览器失败", e)
            finally:
                self._loop_env = None
                self._pause_event = None
                self._browser_only = False
                self._scan_active = False
                self.root.after(0, lambda: (
                    self._update_button_states(),
                    self._set_status("浏览器已关闭", SUCCESS),
                ))

        threading.Thread(target=run, daemon=True).start()

    def _resume_or_start_scan(self):
        """开始扫描：新建一个 run_loop_scan 任务（暂停恢复走 pause_btn，不在这里）"""
        from bot.main import DEFAULT_CLAIM_NO
        from bot.runners.loop import run_loop_scan
        from bot.runtime import clear_stop
        from bot.step_gate import StepGate
        import asyncio as _asyncio

        assert self._loop_loop is not None
        assert self._loop_env is not None
        self._ensure_loop_thread()
        self._apply_market_tuning()   # 控制台改的减速参数/配额，启动前同步进 bot.config
        self.start_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")

        # 扫描还没跑过 / 已经停止 → 新建任务
        self._clear_log()
        counter = self._ui("run_counter_label")
        if counter is not None:
            counter.configure(text="0")

        clear_stop()
        StepGate.instance().disable()
        try:
            from bot.skills.hooks import LearnSession
            LearnSession.instance().clear()   # 清掉上一轮残留的学习挂起
        except Exception:
            pass
        self._pause_event = _asyncio.Event()
        spec = self._current_spec()
        tol = self._tolerance()
        transfer_target = self._transfer_target()
        poll_interval = self._poll_interval()

        log_audit(f"点击『▶ 开始扫描』| {spec.label}")
        self._set_status("扫描中...", ACCENT)

        def _scan_done(fut):
            """run_loop_scan 结束回调（在持久 loop 线程里被触发，需切回主线程）"""
            self._loop_scan_task = None
            self.root.after(0, self._on_scan_done)

        # 用 run_coroutine_threadsafe 在持久 loop 上创建 run_loop_scan 任务
        def launch():
            scan_coro = run_loop_scan(
                self._loop_env, spec,
                pause_event=self._pause_event,
                tolerance=tol, max_count=9999,
                logger=self._loop_logger,
                transfer_target=transfer_target, mode="loop",
                poll_interval=poll_interval,
                skill_learning=bool(self.skill_learn_var.get()),
                skip_market=bool(self.skip_market_var.get()),
                market_price_threshold=self._market_threshold(),
                market_rate_limit=bool(self.market_rate_var.get()),
                market_breaker=bool(self.market_breaker_var.get()),
            )
            fut = _asyncio.run_coroutine_threadsafe(scan_coro, self._loop_loop)
            self._loop_scan_task = fut
            fut.add_done_callback(_scan_done)
            # task 已就绪 → 回主线程刷新按钮（此时 _scan_active 已为 True）
            try:
                self.root.after(0, self._update_button_states)
            except Exception:
                pass

        threading.Thread(target=launch, daemon=True).start()
        # 主线程同步置位，保证按钮立即变成『◼ 停止扫描』（不等子线程）
        self._scan_active = True
        self._update_button_states()

    def _on_scan_done(self):
        """扫描任务结束（正常退出 / stop_requested 退出都走这里）"""
        self._pause_event = None
        self._loop_scan_task = None
        self._scan_active = False
        log_run("挂机扫描已结束（浏览器保持开启）")
        # 看 env 还活着吗（停止挂机时 env 已经清了）
        if self._loop_env is not None and self._loop_env.started:
            self._set_status("扫描已停止，浏览器保持开启", WARNING)
        self._update_button_states()

    def _stop_scan(self):
        """◼ 停止扫描：request_stop → run_loop_scan 退出，浏览器保持开启。

        暂停中也能停：loop 里 stop_requested 优先级高于 pause_event。
        """
        from bot.runtime import request_stop

        log_audit("点击『◼ 停止扫描』（浏览器保持开启）")
        log_run("⏹ 正在停止扫描（当前案件处理完即退出，浏览器保持开启）")
        request_stop()
        self._set_status("正在停止扫描...", WARNING)
        scan = self._ui("scan_btn")
        if scan is not None:
            scan.configure(text="⏳ 停止中...", state="disabled")
        pause = self._ui("pause_btn")
        if pause is not None:
            pause.configure(state="disabled")

    def _on_loop_msg(self, msg):
        w = self._ui("run_counter_label")
        if w is None:
            return
        m = re.search(r"#(\d+)", msg)
        if m:
            w.configure(text=m.group(1))

    def _on_loop_done(self):
        """兼容老 _start_single / _start_manual（不走持久 loop 的一次性任务）结束时调用"""
        self._end_run()

    def on_stop(self):
        """保留兼容：老的『■ 停止』按钮行为（单步/人工测试停止用）"""
        from bot.runtime import request_stop
        from bot.step_gate import StepGate
        log_audit("点击『停止』（单步/人工测试）")
        request_stop()
        try:
            StepGate.instance().abort()
        except Exception:
            pass

    def _autodetect_portal(self):
        """启动时探测内网门户是否可达：可达则默认走真实门户，避免误开本地模拟页"""
        import bot.config as cfg
        from urllib.parse import urlparse

        def probe():
            ok = False
            try:
                u = urlparse(cfg.PORTAL_REAL_URL)
                port = u.port or (443 if u.scheme == "https" else 80)
                socket.create_connection((u.hostname, port), timeout=2).close()
                ok = True
            except Exception:
                ok = False
            try:
                self.root.after(0, lambda: self._on_portal_probe(ok))
            except Exception:
                pass

        threading.Thread(target=probe, daemon=True).start()

    def _on_portal_probe(self, ok: bool):
        import bot.config as cfg
        if not ok or self.portal_real_var.get():
            return
        self.portal_real_var.set(True)
        log_audit(f"检测到内网门户可达 → 默认走真实门户 {cfg.PORTAL_REAL_URL}")
        self._refresh_portal_hint()

    def _connect_browser(self):
        """探测本机已打开的浏览器调试端口，并列出其中的标签页"""
        if getattr(self, "_cdp_busy", False):
            return
        self._cdp_busy = True
        self._set_status("正在探测浏览器…", WARNING)
        threading.Thread(target=self._connect_browser_thread, daemon=True).start()

    def _connect_browser_thread(self):
        try:
            import urllib.request
            from bot.browser import detect_cdp

            log_run("🔗 正在探测已打开的浏览器调试端口（9222/9223/9224/9333）…")
            cdp = detect_cdp(timeout=1.0)
            if not cdp:
                log_run("❌ 没有发现调试端口。请关闭浏览器后用带参数的快捷方式启动：\n"
                        "   chrome.exe --remote-debugging-port=9222 "
                        "--user-data-dir=\"data\\portal_profile\"")
                self.root.after(0, self._set_status, "未连接浏览器", DANGER)
                return

            self.root.after(0, self.portal_cdp_var.set, cdp)
            self.root.after(0, self._refresh_portal_hint)

            with urllib.request.urlopen(cdp.rstrip("/") + "/json/list",
                                        timeout=3) as resp:
                targets = json.loads(resp.read().decode("utf-8", "ignore"))
            tabs = [t for t in targets if t.get("type") == "page"]
            log_run(f"✅ 已发现浏览器: {cdp} · 标签页 {len(tabs)} 个")
            home_pick = ""
            for t in tabs:
                url = (t.get("url") or "").strip()
                title = (t.get("title") or "").strip() or "(无标题)"
                mark = ""
                if "claimOperate" in url or "claimcar" in url:
                    mark = "  ← 推荐（理赔门户）"
                    home_pick = home_pick or url
                log_run(f"   ● {title} | {url[:110]}{mark}")
            if home_pick:
                log_run(f"✅ 接管后优先操作该标签: {home_pick[:110]}")
            self.root.after(0, self._set_status, "已连接浏览器", SUCCESS)
        except Exception as exc:
            log_run(f"❌ 连接失败: {exc}")
            self.root.after(0, self._set_status, "连接失败", DANGER)
        finally:
            self._cdp_busy = False

    def _refresh_portal_hint(self):
        label = getattr(self, "portal_hint", None)
        if label is None or not label.winfo_exists():
            return
        import bot.config as cfg
        cdp = (self.portal_cdp_var.get() or "").strip()
        if not self.portal_real_var.get():
            label.configure(
                text=f"当前：本地模拟门户（127.0.0.1）\n勾选上面的选项即可走真实内网：{cfg.PORTAL_REAL_URL}",
                fg=TEXT_DIM)
        elif cdp:
            label.configure(
                text=f"当前：连接你已打开的浏览器 {cdp}\n（留空会自动检测 9222/9223/9224/9333，不新开窗口）",
                fg=SUCCESS if cdp.startswith("http") else WARNING)
        else:
            label.configure(
                text=f"当前：真实内网 {cfg.PORTAL_REAL_URL} · 程序新开窗口"
                     f"（留空会先自动检测已打开浏览器；登录一次复用）",
                fg=TEXT_MUTED)

    def _apply_browser_config(self):
        """把控制台选择的浏览器写入 bot.config（main 启动时读取）"""
        import bot.config as cfg
        pref = (self.browser_var.get() or "auto").strip() or "auto"
        path = (self.browser_path_var.get() or "").strip()
        if pref == "custom" and not path:
            pref = "auto"
            log_audit("浏览器：选了自定义但路径为空 → 回退 auto")
        cfg.BOT_BROWSER_PREFERENCE = pref
        cfg.BOT_BROWSER_PATH = path if pref == "custom" else ""

        # 未填 CDP 时自动探测本机已打开的浏览器（如用 --remote-debugging-port=9222 启动的 360）
        cdp = (self.portal_cdp_var.get() or "").strip()
        if not cdp:
            try:
                from bot.browser import detect_cdp
                cdp = detect_cdp(timeout=0.6)
                if cdp:
                    log_audit(f"自动检测到已打开浏览器 {cdp} → 直接接管，不再新开窗口")
            except Exception:
                cdp = ""
        cfg.PORTAL_CDP_URL = cdp
        cfg.MARKET_CDP_URL = cdp   # 一车一件复用同一个已打开浏览器
        self._save_ui_config()
        log_audit(f"浏览器设置: {pref}"
                  + (f" ({path})" if pref == "custom" else "")
                  + f" | 门户={'真实内网' if self.portal_real_var.get() else '本地 mock'}"
                  + (f" | CDP={cfg.PORTAL_CDP_URL}" if cfg.PORTAL_CDP_URL else ""))

    # ---------- 界面设置持久化（浏览器选择等，打包后 data/config/ui.json） ----------
    def _ui_config_path(self):
        from bot.logging_system import app_data_dir
        d = app_data_dir() / "config"
        d.mkdir(parents=True, exist_ok=True)
        return d / "ui.json"

    def _load_ui_config(self):
        try:
            p = self._ui_config_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            for key, var in (("browser", self.browser_var),
                             ("browser_path", self.browser_path_var),
                             ("portal_cdp", self.portal_cdp_var),
                             ("poll_interval", self.poll_var),
                             ("tolerance", self.tol_var),
                             ("vin_delay_min", self.vin_delay_min_var),
                             ("vin_delay_max", self.vin_delay_max_var),
                             ("part_delay_min", self.part_delay_min_var),
                             ("part_delay_max", self.part_delay_max_var),
                             ("claim_cooldown", self.claim_cd_var),
                             ("quota_short", self.quota_short_var),
                             ("quota_long", self.quota_long_var)):
                val = data.get(key)
                if isinstance(val, str) and val:
                    var.set(val)
                elif isinstance(val, (int, float)):
                    var.set(f"{val:g}")
        except Exception as exc:
            log_system(f"读取界面设置失败: {exc}")

    def _save_ui_config(self):
        try:
            p = self._ui_config_path()
            p.write_text(json.dumps({
                "browser": self.browser_var.get(),
                "browser_path": self.browser_path_var.get(),
                "portal_cdp": self.portal_cdp_var.get(),
                "poll_interval": self.poll_var.get(),
                "tolerance": self.tol_var.get(),
                "vin_delay_min": self.vin_delay_min_var.get(),
                "vin_delay_max": self.vin_delay_max_var.get(),
                "part_delay_min": self.part_delay_min_var.get(),
                "part_delay_max": self.part_delay_max_var.get(),
                "claim_cooldown": self.claim_cd_var.get(),
                "quota_short": self.quota_short_var.get(),
                "quota_long": self.quota_long_var.get(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log_system(f"保存界面设置失败: {exc}")

    def _pick_browser(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(
            title="选择浏览器可执行文件",
            filetypes=[("可执行文件", "*.exe"), ("全部文件", "*.*")],
        )
        if not p:
            return
        self.browser_path_var.set(p)
        self.browser_var.set("custom")
        self._refresh_browser_hint()

    def _refresh_browser_hint(self):
        label = getattr(self, "browser_hint", None)
        if label is None or not label.winfo_exists():
            return
        try:
            from bot.browser import resolve
            info = resolve(self.browser_var.get(), self.browser_path_var.get())
        except Exception as exc:
            label.configure(text=f"浏览器检测失败: {exc}", fg=DANGER)
            return
        if info.get("path"):
            text = f"实际使用：{info['label']} · {info['path']}"
            color = TEXT_MUTED
        else:
            text = f"实际使用：{info['label']}（打包版不带内置浏览器，建议选 chrome 或指定路径）"
            color = WARNING
        if info.get("warning"):
            text = info["warning"]
            color = WARNING
        label.configure(text=text, fg=color)

    def _start_single(self, step_mode: bool = False):
        from bot.main import DEFAULT_CLAIM_NO
        from bot.runners import run_single
        from bot.step_gate import StepGate, StepAborted
        from bot.runtime import clear_stop

        self._apply_browser_config()
        spec = self._current_spec()

        claim = ""
        if hasattr(self, "claim_var"):
            claim = self.claim_var.get().strip()
        claim = claim or DEFAULT_CLAIM_NO
        tol = self._tolerance()
        dry_run = bool(self.dry_run_var.get()) if hasattr(self, "dry_run_var") else False
        transfer_target = self._transfer_target()
        self._apply_market_tuning()   # 控制台改的减速参数/配额，运行前同步进 bot.config
        log_audit(
            f"点击『{'单步调试' if step_mode else '连续执行'}』报案号={claim} | {spec.label}"
            + ("（演练模式）" if dry_run else "")
        )

        gate = StepGate.instance()
        if step_mode:
            gate.enable()
            gate.on_wait = lambda label: self.root.after(0, self._on_step_wait, label)
        else:
            gate.disable()
            gate.on_wait = None
        clear_stop()

        self._clear_log()
        self._begin_run("演练中" if dry_run else ("单步调试中" if step_mode else "运行中"))
        for name in ("test_run_btn", "test_step_btn", "step_next_btn"):
            w = self._ui(name)
            if w is not None:
                w.configure(state="disabled")
        w = self._ui("step_stop_btn")
        if w is not None:
            w.configure(state="normal" if step_mode else "disabled")

        async def worker():
            try:
                await run_single(claim, spec, tolerance=tol, dry_run=dry_run,
                                 transfer_target=transfer_target,
                                 mode="step" if step_mode else "single",
                                 skill_learning=bool(self.skill_learn_var.get()),
                                 skip_market=bool(self.skip_market_var.get()),
                                 market_price_threshold=self._market_threshold(),
                                 market_rate_limit=bool(self.market_rate_var.get()),
                                 market_breaker=bool(self.market_breaker_var.get()))
            except StepAborted:
                log_run("单步调试已中止")
            except Exception as e:
                log_error("单次运行异常", e)
                print(f"[err] {e}")
            finally:
                gate.disable()
                gate.on_wait = None
                self.root.after(0, self._on_single_done)

        threading.Thread(target=lambda: asyncio.run(worker()), daemon=True).start()

    def _ui(self, name):
        """安全取控件：视图被切换/销毁后返回 None"""
        w = getattr(self, name, None)
        try:
            if w is not None and w.winfo_exists():
                return w
        except Exception:
            pass
        return None

    # ---------- 🧠 学习模式：失败挂起 → 手动处理 → 存技能 ----------
    def _learn_poll(self):
        """每秒刷新学习模式状态条：有挂起会话就亮起『保存学习 / 跳过本案件』"""
        try:
            from bot.skills.hooks import LearnSession
            info = LearnSession.instance().snapshot()
        except Exception:
            info = None
        lbl = self._ui("learn_info")
        save_btn = self._ui("learn_save_btn")
        skip_btn = self._ui("learn_skip_btn")
        try:
            if info is not None:
                if lbl is not None:
                    lbl.configure(
                        text=f"🧠 卡在 {info.get('step', '')} · 请在浏览器手动处理，"
                             f"然后点『💾 保存学习』",
                        fg=WARNING)
                for b in (save_btn, skip_btn):
                    if b is not None:
                        b.configure(state="normal")
            else:
                if lbl is not None:
                    lbl.configure(text="🧠 学习模式：某步失败时会暂停，等你手动处理",
                                  fg=TEXT_DIM)
                for b in (save_btn, skip_btn):
                    if b is not None:
                        b.configure(state="disabled")
        except Exception:
            pass
        try:
            self.root.after(1000, self._learn_poll)
        except Exception:
            pass

    def _learn_decide(self, action: str):
        """『💾 保存学习』/『⏭ 跳过本案件』：通知挂起中的机器人协程"""
        try:
            from bot.skills.hooks import LearnSession
            if not LearnSession.instance().decide(action):
                return
            label = "保存学习" if action == "save" else "跳过本案件"
            print(f"\n🧠 学习模式 → {label}")
            log_audit(f"学习模式: {label}")
        except Exception as e:
            messagebox.showerror("学习模式", f"{type(e).__name__}: {e}")

    def _on_single_done(self):
        self._end_run()
        for name in ("test_run_btn", "test_step_btn"):
            w = self._ui(name)
            if w is not None:
                w.configure(state="normal")
        for name in ("step_next_btn", "step_stop_btn"):
            w = self._ui(name)
            if w is not None:
                w.configure(state="disabled")

    # ---------- 单步调试 ----------
    def _on_step_wait(self, label):
        """bot 到达下一个 STEP，暂停等待用户点『下一步』（已切回主线程）"""
        w = self._ui("step_next_btn")
        if w is None:
            return
        w.configure(state="normal")
        self._set_status("已暂停 · 等待下一步", WARNING)
        print(f"\n⏸ 暂停 —— 即将执行：{label}    （点『▶ 下一步』继续）")

    def on_step_next(self):
        from bot.step_gate import StepGate
        log_audit("点击『下一步』")
        w = self._ui("step_next_btn")
        if w is not None:
            w.configure(state="disabled")
        self._set_status("单步执行中", ACCENT)
        StepGate.instance().next_step()

    def on_step_stop(self):
        from bot.step_gate import StepGate
        from bot.runtime import request_stop
        log_audit("点击『停止』（单步调试）")
        request_stop()
        StepGate.instance().abort()
        for name in ("step_next_btn", "step_stop_btn"):
            w = self._ui(name)
            if w is not None:
                w.configure(state="disabled")
        self._set_status("已停止", DANGER)

    def _start_loop(self, mode: str = "mock_loop"):
        from bot.main import DEFAULT_CLAIM_NO
        from bot.runners import run_loop
        from bot.runtime import clear_stop

        self._apply_browser_config()
        spec = self._current_spec()

        tol = self._tolerance()
        transfer_target = self._transfer_target()
        poll_interval = self._poll_interval()
        self._apply_market_tuning()   # 控制台改的减速参数/配额，运行前同步进 bot.config
        clear_stop()
        log_audit(f"点击『开始挂机』（{MODE_LABELS.get(mode, mode)}）| {spec.label}")

        self._clear_log()
        self._begin_run(f"{MODE_LABELS.get(mode, mode)}中")
        counter = self._ui("run_counter_label")
        if counter is not None:
            counter.configure(text="0")

        async def worker():
            try:
                await run_loop(DEFAULT_CLAIM_NO, spec, tolerance=tol, max_count=9999,
                               logger=self._loop_logger,
                               transfer_target=transfer_target, mode=mode,
                               poll_interval=poll_interval,
                               skill_learning=bool(self.skill_learn_var.get()),
                               skip_market=bool(self.skip_market_var.get()),
                               market_price_threshold=self._market_threshold(),
                               market_rate_limit=bool(self.market_rate_var.get()),
                               market_breaker=bool(self.market_breaker_var.get()))
            except Exception as e:
                log_error("挂机异常", e)
                print(f"[err] {e}")
            finally:
                self.root.after(0, self._on_loop_done)

        threading.Thread(target=lambda: asyncio.run(worker()), daemon=True).start()

    def _start_manual(self):
        """人工测试：人工在浏览器操作，程序逐步校验并生成对比记录"""
        from bot.main import DEFAULT_CLAIM_NO
        from bot.runners import run_manual
        from bot.runtime import clear_stop

        self._apply_browser_config()
        spec = self._current_spec()

        claim = ""
        if hasattr(self, "claim_var"):
            claim = self.claim_var.get().strip()
        claim = claim or DEFAULT_CLAIM_NO
        transfer_target = self._transfer_target()
        clear_stop()
        log_audit(f"点击『人工测试』报案号={claim} | {spec.label}")

        self._clear_log()
        self._begin_run("人工测试 · 等待你操作")
        print("\n  提示：运行后请在浏览器里按 STEP 顺序操作，程序只观察、不点击。")

        async def worker():
            try:
                await run_manual(claim, spec, timeout_s=900,
                                 transfer_target=transfer_target,
                                 logger=self._loop_logger)
            except Exception as e:
                log_error("人工测试异常", e)
                print(f"[err] {e}")
            finally:
                self.root.after(0, self._on_loop_done)

        threading.Thread(target=lambda: asyncio.run(worker()), daemon=True).start()

    def _loop_logger(self, msg, *args):
        """run_loop 的进度回调（在 bot 线程里调用，切回主线程更新计数器）"""
        try:
            self.root.after(0, self._on_loop_msg, str(msg))
        except Exception:
            pass

    def _on_loop_msg(self, msg):
        w = self._ui("run_counter_label")
        if w is None:
            return
        m = re.search(r"#(\d+)", msg)
        if m:
            w.configure(text=m.group(1))

    def _on_loop_done(self):
        self._end_run()
        log_run("挂机任务已结束")

    def on_stop(self):
        from bot.runtime import request_stop
        from bot.step_gate import StepGate

        log_audit("点击『■ 停止』")
        request_stop()
        try:
            StepGate.instance().abort()
        except Exception:
            pass
        self._set_status("停止中", WARNING)
        b = self._ui("scan_btn")
        if b is not None:
            b.configure(state="disabled")

    def _install_step_tracker(self): pass
    def _run_worker(self): pass
    async def _build_and_run(self): pass
    def _on_worker_done(self): pass

    # ---------- 退出 ----------
    def on_close(self):
        log_audit("点击『关闭窗口』")
        log_system("用户关闭窗口，程序退出")
        try:
            from bot.runtime import request_stop
            request_stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    """程序入口"""
    setup()

    # 统一接管 stdout / stderr → 日志盒 + 日志文件双写
    # （windowed 打包后 stdout/stderr 可能是 None，这样也不会丢日志）
    sys.stdout = RUN_STREAM
    sys.stderr = ERR_STREAM

    log_system(f"{APP_NAME} {version_text()} 启动")
    log_system(environment_summary())
    for line in recent_changes(3):
        log_system("最近变更 " + line)

    # windowed 模式下 stderr 可能为 None，额外留一份崩溃现场
    crash_log = APP_DIR / "data" / "gui_crash.log"
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        root = tk.Tk()
    except Exception as e:
        log_error("Tk 初始化失败", e)
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] Tk 初始化失败: {e}\n")
        raise

    try:
        app = App(root)
        root.protocol("WM_DELETE_WINDOW", app.on_close)
        # 程序运行期间不让系统休眠 / 熄屏 / 进屏保（关闭窗口时恢复）
        from bot.power import keep_awake, allow_sleep
        keep_awake()
        try:
            root.mainloop()
        finally:
            allow_sleep()
        log_system("主窗口已关闭，程序正常退出")
    except Exception as e:
        tb = traceback.format_exc()
        log_error("GUI 异常退出", e)
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] 启动失败:\n{tb}\n")
        # 有控制台时也打印一份
        try:
            if sys.__stdout__ is not None:
                print(tb, file=sys.__stdout__)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()

