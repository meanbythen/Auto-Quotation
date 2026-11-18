"""核心层：接口契约 + 运行记录（不依赖任何具体环境实现）"""
from .ports import PortalPort, MarketPort
from .records import (
    StepRecord, RunRecord, Recorder,
    records_dir, load_records, find_previous,
    compare_markdown, write_comparison, auto_compare,
)

__all__ = [
    "PortalPort", "MarketPort",
    "StepRecord", "RunRecord", "Recorder",
    "records_dir", "load_records", "find_previous",
    "compare_markdown", "write_comparison", "auto_compare",
]
