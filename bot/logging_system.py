"""
统一日志系统

参考成熟软件的日志规范，分成四类、各写各的文件：

  system  系统日志   启动 / 退出 / 版本 / 运行环境 / 登录态 / 配置
  run     运行日志   每个 STEP、每个报案号的处理结果、比对结论
  audit   操作日志   每一次点击、每一次输入（可追溯"谁在什么时候点了什么"）
  error   错误日志   异常 + traceback（含来自 system/run 的 ERROR 级别）

文件命名与轮转：
  data/logs/system_20261029.log      ← 按天自动分文件
  data/logs/run_20261029.log
  data/logs/run_20261029_1.log       ← 单文件超过 8MB 自动切分
  30 天前的日志自动清理

行格式：
  2026-10-29 14:03:22.451 | INFO  | run    | STEP 3 定损平台 → 同步定损

用法：
  from bot.logging_system import setup, log_system, log_run, log_audit, log_error, tail
  setup()                       # 程序入口调用一次即可（幂等）
  log_system("GUI 启动")
  log_run("开始处理 报案号=...")
  log_audit("点击: 同步定损按钮")
  log_error("流程失败", exc)     # exc 传异常对象 → 自动附 traceback
"""
from __future__ import annotations

import io
import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

CATEGORIES = ("system", "run", "audit", "error")

CATEGORY_LABELS = {
    "system": "系统日志",
    "run": "运行日志",
    "audit": "操作日志",
    "error": "错误日志",
}

_FMT = "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(name)-6s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 8 * 1024 * 1024     # 单文件 8MB
KEEP_DAYS = 30                  # 保留 30 天

_lock = threading.RLock()
_configured = False
_log_dir: Path | None = None
_buf_lock = threading.Lock()
_bufs: dict[str, str] = {}


# ============================================================
# 目录
# ============================================================
def app_data_dir() -> Path:
    """打包后在 exe 同目录 data/，开发时在项目 data/"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


def get_log_dir() -> Path:
    global _log_dir
    if _log_dir is None:
        _log_dir = app_data_dir() / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
    return _log_dir


# ============================================================
# 按天 + 按大小 轮转的文件 Handler
# ============================================================
class DailyFileHandler(logging.Handler):
    def __init__(self, category: str, log_dir: Path,
                 max_bytes: int = MAX_BYTES, keep_days: int = KEEP_DAYS,
                 encoding: str = "utf-8"):
        super().__init__()
        self.category = category
        self.dir = Path(log_dir)
        self.max_bytes = max_bytes
        self.keep_days = keep_days
        self.encoding = encoding
        self._day = ""
        self._stream = None
        self._path: Path | None = None
        with _lock:
            self._cleanup_old()
            self._open_new_day()

    # ---------- 内部 ----------
    def _today(self) -> str:
        return datetime.now().strftime("%Y%m%d")

    def _path_for(self, day: str, index: int) -> Path:
        suffix = "" if index == 0 else f"_{index}"
        return self.dir / f"{self.category}_{day}{suffix}.log"

    def _open_new_day(self) -> None:
        self._close_stream()
        self._day = self._today()
        index = 0
        while True:
            p = self._path_for(self._day, index)
            if not p.exists() or p.stat().st_size < self.max_bytes:
                break
            index += 1
            if index > 9:      # 保险：单日最多 10 个分片
                break
        self._path = self._path_for(self._day, index)
        self._stream = open(self._path, "a", encoding=self.encoding, buffering=1)

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _rotate_if_needed(self) -> None:
        if self._stream is None:
            self._open_new_day()
            return
        if self._today() != self._day:
            self._open_new_day()
            return
        try:
            size = self._path.stat().st_size if self._path else 0
        except Exception:
            size = 0
        if size >= self.max_bytes:
            self._open_new_day()

    def _cleanup_old(self) -> None:
        try:
            cutoff = datetime.now().timestamp() - self.keep_days * 86400
            for f in self.dir.glob(f"{self.category}_*.log"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- logging.Handler ----------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            with _lock:
                self._rotate_if_needed()
                self._stream.write(self.format(record) + "\n")
                self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with _lock:
            self._close_stream()
        super().close()


# ============================================================
# console 输出 → 运行日志（CLI 模式下 print 也进日志）
# ============================================================
class _TeeStream(io.TextIOBase):
    """把 print 内容原样写进 run 日志，同时保留控制台输出"""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        if not text:
            return 0
        feed_stream_text("run", text)
        try:
            if self._stream is not None:
                self._stream.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        flush_stream_text("run")
        try:
            if self._stream is not None:
                self._stream.flush()
        except Exception:
            pass

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8") or "utf-8"

    def fileno(self):
        if self._stream is not None and hasattr(self._stream, "fileno"):
            return self._stream.fileno()
        raise OSError("no fileno")


# ============================================================
# 初始化
# ============================================================
def setup(level: int | str = logging.INFO) -> Path:
    """初始化四类日志（幂等）。返回日志目录"""
    global _configured
    with _lock:
        log_dir = get_log_dir()
        if _configured:
            return log_dir

        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)

        formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)
        for cat in CATEGORIES:
            logger = logging.getLogger(cat)
            logger.setLevel(level)
            logger.propagate = False
            # 清掉旧 handler（多次 setup / 热重载时避免重复写）
            for h in list(logger.handlers):
                logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
            handler = DailyFileHandler(cat, log_dir)
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        logging.captureWarnings(True)
        # CLI 模式：print 也落进运行日志（GUI 的 stdout 自带写日志，检测到就不再包一层）
        if not isinstance(sys.stdout, _TeeStream) and not hasattr(sys.stdout, "category"):
            sys.stdout = _TeeStream(sys.stdout)
        _configured = True
        return log_dir


def _logger(cat: str) -> logging.Logger:
    if not _configured:
        setup()
    return logging.getLogger(cat)


# ============================================================
# 对外 API
# ============================================================
def _write(cat: str, msg: str, level: int = logging.INFO) -> None:
    _logger(cat).log(level, msg)
    # ERROR 级别额外写到 error 日志，方便一处就能捞全部异常
    if level >= logging.ERROR and cat != "error":
        _logger("error").log(level, f"[{cat}] {msg}")


def log_system(msg: str) -> None:
    _write("system", msg)


def log_run(msg: str) -> None:
    _write("run", msg)


def log_audit(msg: str) -> None:
    _write("audit", msg)


def log_error(msg: str, exc: BaseException | None = None) -> None:
    """错误日志：自动附带 traceback"""
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        msg = f"{msg}\n{tb}"
    _write("error", msg, logging.ERROR)


def log_warn(msg: str, cat: str = "run") -> None:
    _write(cat, msg, logging.WARNING)


def log_run_text(text: str) -> None:
    """把 console 输出（print）原样落到运行日志"""
    feed_stream_text("run", text)


def log_err_text(text: str) -> None:
    """把 stderr 输出原样落到错误日志"""
    feed_stream_text("error", text)


def feed_stream_text(cat: str, text: str) -> None:
    """按行缓冲：print 往往分成多次 write，攒够一行再写日志，避免半截行"""
    if not text:
        return
    with _buf_lock:
        buf = _bufs.get(cat, "") + text
        lines = buf.split("\n")
        _bufs[cat] = lines.pop()
    for line in lines:
        if line.strip():
            _write(cat, line)


def flush_stream_text(cat: str) -> None:
    with _buf_lock:
        rest = _bufs.get(cat, "")
        _bufs[cat] = ""
    if rest.strip():
        _write(cat, rest)


# ============================================================
# 读取（GUI 日志中心用）
# ============================================================
def log_files(cat: str) -> list[Path]:
    """该分类按时间倒序的所有日志文件"""
    try:
        return sorted(get_log_dir().glob(f"{cat}_*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []


def tail(cat: str, lines: int = 200) -> str:
    """读最近 N 行；当天文件不够则往前补"""
    if cat not in CATEGORIES:
        return f"（未知日志分类: {cat}）"
    out: list[str] = []
    for f in log_files(cat):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                chunk = fp.readlines()
        except Exception as e:
            out.insert(0, f"（读取 {f.name} 失败: {e}）")
            continue
        remain = lines - len(out)
        if remain <= 0:
            break
        out = chunk[-remain:] + out
        if len(out) >= lines:
            break
    if not out:
        return f"（{CATEGORY_LABELS[cat]}为空 —— 还没有产生记录）"
    return "".join(out)


def tail_all(lines: int = 200) -> str:
    """四类日志合并预览（按文件时间近似排序，便于一眼看全）"""
    blocks = []
    for cat in CATEGORIES:
        content = tail(cat, max(20, lines // len(CATEGORIES)))
        blocks.append(f"===== {CATEGORY_LABELS[cat]}（{cat}）=====\n{content}")
    return "\n".join(blocks)


def environment_summary() -> str:
    """运行环境摘要（写入系统日志用）"""
    import platform
    frozen = getattr(sys, "frozen", False)
    return (
        f"环境: {platform.system()} {platform.release()} | "
        f"Python {platform.python_version()} | "
        f"运行方式: {'exe(frozen)' if frozen else '源码'}"
    )
