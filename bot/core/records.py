"""
运行记录 —— 一次运行的逐步结果，落盘为 JSON / Markdown

用途：
  1) 模拟环境跑一遍、真实环境跑一遍 → 自动对比两份记录，互相印证流程是否一致
  2) 人工测试（人工操作 + 程序校验）也产出同结构记录 → 与自动执行记录对比
  3) 排障：某一步耗时/失败原因一目了然

目录：data/compare/
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(value):
    """把任意对象转成可 JSON 序列化的形式"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return str(value)


def records_dir() -> Path:
    from bot.logging_system import app_data_dir
    d = app_data_dir() / "compare"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class StepRecord:
    id: str
    name: str
    status: str = "running"        # running / ok / failed / skipped / not_observed
    started_at: str = ""
    duration_ms: int = 0
    detail: dict = field(default_factory=dict)


@dataclass
class RunRecord:
    mode: str                      # mock_loop / real_loop / single / step / manual
    env_portal: str                # mock / real / real-cdp
    env_market: str                # mock / real
    claim_no: str
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = "running"        # done / failed / skipped / stopped / dry_run
    route: str = ""                # normal / high_amount / no_parts / dry_run
    steps: List[StepRecord] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    note: str = ""

    def __post_init__(self):
        if not self.run_id:
            self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + (self.claim_no or "x")
        if not self.started_at:
            self.started_at = _now()

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "claim_no": self.claim_no,
            "env": {"portal": self.env_portal, "market": self.env_market},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "route": self.route,
            "note": self.note,
            "steps": [
                {
                    "id": s.id, "name": s.name, "status": s.status,
                    "started_at": s.started_at, "duration_ms": s.duration_ms,
                    "detail": _safe(s.detail),
                }
                for s in self.steps
            ],
            "data": _safe(self.data),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        env = d.get("env") or {}
        rec = cls(
            mode=d.get("mode", ""), env_portal=env.get("portal", ""),
            env_market=env.get("market", ""), claim_no=d.get("claim_no", ""),
            run_id=d.get("run_id", ""), started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""), status=d.get("status", ""),
            route=d.get("route", ""), note=d.get("note", ""),
        )
        rec.steps = [StepRecord(**{k: s.get(k, v) for k, v in
                                   (("id", ""), ("name", ""), ("status", "running"),
                                    ("started_at", ""), ("duration_ms", 0),
                                    ("detail", {}))})
                     for s in d.get("steps", [])]
        rec.data = d.get("data") or {}
        return rec

    # ---------- 落盘 ----------
    def save(self, out_dir: Path | None = None) -> Path:
        d = out_dir or records_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def step(self, sid: str) -> StepRecord | None:
        for s in self.steps:
            if s.id == sid:
                return s
        return None

    def mark(self, sid: str, status: str, **detail) -> None:
        s = self.step(sid)
        if s is None:
            s = StepRecord(id=sid, name=sid, started_at=_now())
            self.steps.append(s)
        s.status = status
        s.detail.update(detail)


class Recorder:
    """流程里用的逐步记录器（异步上下文管理器）"""

    def __init__(self, record: RunRecord):
        self.record = record

    def begin(self) -> "Recorder":
        self.record.started_at = _now()
        self.record.status = "running"
        return self

    def mark(self, sid: str, status: str, **detail) -> None:
        """补记一个步骤状态（skipped / not_observed 等），不抛异常"""
        self.record.mark(sid, status, **detail)

    @asynccontextmanager
    async def step(self, sid: str, name: str):
        """async with recorder.step("STEP 1", "首页 → 点击报案号") as st: ..."""
        st = StepRecord(id=sid, name=name, started_at=_now())
        self.record.steps.append(st)
        t0 = time.perf_counter()
        try:
            yield st
        except Exception as exc:
            st.status = "failed"
            st.detail["error"] = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            raise
        else:
            if st.status == "running":
                st.status = "ok"
        finally:
            st.duration_ms = int((time.perf_counter() - t0) * 1000)

    def finish(self, status: str, *, route: str = "", note: str = "", data: dict | None = None):
        self.record.status = status
        self.record.route = route or self.record.route
        self.record.note = note or self.record.note
        if data:
            self.record.data.update(_safe(data))
        self.record.finished_at = _now()


# ============================================================
# 读取 + 对比
# ============================================================
def load_records(out_dir: Path | None = None) -> list[RunRecord]:
    d = out_dir or records_dir()
    if not d.exists():
        return []
    recs: list[RunRecord] = []
    for f in sorted(d.glob("*.json")):
        try:
            recs.append(RunRecord.from_dict(json.loads(f.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return recs


def find_previous(claim_no: str, portal_label: str, run_id: str,
                  out_dir: Path | None = None,
                  ignore_env: bool = False) -> RunRecord | None:
    """找同一报案号的最近一条已完成记录（用于自动对比）

    ignore_env=False：必须来自「另一套环境」（模拟 vs 真实互相印证）
    ignore_env=True ：不限环境（人工测试 vs 自动执行 对比用）
    """
    others = [r for r in load_records(out_dir)
              if r.claim_no == claim_no and r.run_id != run_id
              and (ignore_env or r.env_portal != portal_label)
              and r.status in ("done", "dry_run", "skipped", "failed", "partial")]
    return others[-1] if others else None


_STATUS_ICON = {
    "ok": "✅", "failed": "❌", "skipped": "⏭", "not_observed": "○",
    "dry_run": "🛡", "running": "…", "done": "✅", "stopped": "⏹",
}


def _icon(status: str) -> str:
    return _STATUS_ICON.get(status, status or "-")


def compare_markdown(a: RunRecord, b: RunRecord) -> str:
    """两份记录的逐步对照表"""
    steps = []
    for s in a.steps:
        steps.append(s.id)
    for s in b.steps:
        if s.id not in steps:
            steps.append(s.id)

    lines = [
        f"# 流程对比报告 · {a.claim_no}",
        "",
        f"- A：`{a.mode}` ｜ 门户 `{a.env_portal}` ｜ 一车一件 `{a.env_market}` ｜ 运行 {a.run_id}",
        f"- B：`{b.mode}` ｜ 门户 `{b.env_portal}` ｜ 一车一件 `{b.env_market}` ｜ 运行 {b.run_id}",
        "",
        "## 整体结果",
        "",
        "| 项目 | A | B | 一致 |",
        "| --- | --- | --- | --- |",
    ]
    pairs = [
        ("最终状态", a.status, b.status),
        ("流程走向", a.route, b.route),
        ("VIN", (a.data or {}).get("vin", ""), (b.data or {}).get("vin", "")),
        ("零件数", len((a.data or {}).get("parts") or []),
         len((b.data or {}).get("parts") or [])),
        ("超市场价件数", (a.data or {}).get("over_market", ""),
         (b.data or {}).get("over_market", "")),
    ]
    for name, x, y in pairs:
        same = "✅" if str(x) == str(y) else "⚠️"
        lines.append(f"| {name} | {x} | {y} | {same} |")

    lines += ["", "## 逐步对照", "",
              "| STEP | 名称 | A | B | 一致 |",
              "| --- | --- | --- | --- | --- |"]
    for sid in steps:
        sa, sb = a.step(sid), b.step(sid)
        name = (sa or sb).name if (sa or sb) else sid
        st_a = sa.status if sa else "—"
        st_b = sb.status if sb else "—"
        same = "✅" if st_a == st_b else "⚠️"
        lines.append(f"| {sid} | {name} | {_icon(st_a)} {st_a} | {_icon(st_b)} {st_b} | {same} |")

        for rec, tag in ((sa, "A"), (sb, "B")):
            if rec and rec.status == "failed" and rec.detail.get("error"):
                lines.append(f"|  |  |  |  | {tag} 失败：{rec.detail['error']} |")

    if a.note or b.note:
        lines += ["", "## 备注", f"- A：{a.note or '—'}", f"- B：{b.note or '—'}"]
    return "\n".join(lines) + "\n"


def write_comparison(a: RunRecord, b: RunRecord, out_dir: Path | None = None) -> Path:
    d = out_dir or records_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{a.claim_no}_compare_{a.run_id}_vs_{b.run_id}.md"
    path.write_text(compare_markdown(a, b), encoding="utf-8")
    return path


def auto_compare(record: RunRecord, out_dir: Path | None = None,
                 ignore_env: bool = False) -> Path | None:
    """运行结束后自动找同案件的参考记录并生成对比报告"""
    prev = find_previous(record.claim_no, record.env_portal, record.run_id,
                         out_dir, ignore_env=ignore_env)
    if prev is None:
        return None
    return write_comparison(prev, record, out_dir)
