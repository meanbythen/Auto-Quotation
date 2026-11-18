"""
智能采集 —— 生产环境「页面结构」与「人工操作」记录

两个能力（都复用现有 EnvSpec / Recorder / bot/recorder.py，不重复实现）：
  1) snapshot  采集当前页面代码、表单控件、下拉选项、表格结构 → 对着改 mock_web
  2) actions   在浏览器里记录人工点击/输入 → 输出流程改进草案（映射到 STEP 1→9）

落盘：data/capture/<时间戳>-<名称>/
"""
from .actions import format_actions_markdown, record_actions
from .snapshot import (
    PAGES_REPORT,
    capture_dir,
    capture_environment,
    capture_page_snapshot,
    diff_snapshots,
    write_report,
)

__all__ = [
    "capture_dir", "capture_page_snapshot", "capture_environment",
    "diff_snapshots", "write_report", "PAGES_REPORT",
    "record_actions", "format_actions_markdown",
]
