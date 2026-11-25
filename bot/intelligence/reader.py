"""读取「报价处理意见列表」并取最新一条

真实站点 DOM 尚未固定，这里采用三级策略：
1. 标准 table（表头含 意见 / 意见说明 / 退回评价 / 发表时间）；
2. Element UI 表头与表体分离的 .el-table；
3. .el-timeline 时间线。

全部失败时只写诊断日志并返回 None —— 绝不抛异常打断主流程。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Optional

from playwright.async_api import Page

from bot.logging_system import log_audit, log_run

_EXTRACT_JS = r"""
() => {
  const KW = ['意见说明', '退回评价', '发表意见时间', '发表时间', '处理意见', '意见'];
  const norm = (s) => (s || '').replace(/\s+/g, '');
  const vis = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  };
  const cellsOf = (tr) => Array.from(tr.children).map((td) => (td.innerText || '').trim());
  const out = [];
  const push = (headers, rows, kind) => {
    out.push({ found: true, headers, rows, kind, empty: rows.length === 0 });
  };

  document.querySelectorAll('table').forEach((t) => {
    if (!vis(t)) return;
    const headers = Array.from(t.querySelectorAll('thead th')).map((th) => norm(th.innerText));
    const htext = headers.join('|');
    if (!KW.some((k) => htext.includes(k))) return;
    const rows = Array.from(t.querySelectorAll('tbody tr')).filter(vis)
      .map((tr) => ({ cells: cellsOf(tr), text: (tr.innerText || '').trim() }))
      .filter((r) => r.text);
    push(headers, rows, 'table');
  });

  document.querySelectorAll('.el-table').forEach((t) => {
    if (!vis(t)) return;
    const headers = Array.from(t.querySelectorAll('.el-table__header th')).map((th) => norm(th.innerText));
    const htext = headers.join('|');
    if (!KW.some((k) => htext.includes(k))) return;
    const rows = Array.from(t.querySelectorAll('.el-table__body tr')).filter(vis)
      .map((tr) => ({ cells: cellsOf(tr), text: (tr.innerText || '').trim() }))
      .filter((r) => r.text);
    push(headers, rows, 'el-table');
  });

  document.querySelectorAll('.el-timeline').forEach((t) => {
    if (!vis(t)) return;
    const rows = Array.from(t.querySelectorAll('.el-timeline-item')).filter(vis)
      .map((it) => ({ cells: [], text: (it.innerText || '').trim() }))
      .filter((r) => r.text);
    if (rows.length) push([], rows, 'timeline');
  });

  if (!out.length) return { found: false };
  out.sort((a, b) => (b.rows ? b.rows.length : 0) - (a.rows ? a.rows.length : 0));
  const best = out[0];
  return { found: true, headers: best.headers, rows: best.rows,
           kind: best.kind, empty: best.rows.length === 0 };
}
"""

_DIAG_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, '');
  const seen = [];
  document.querySelectorAll('table, .el-table').forEach((t) => {
    const ths = Array.from(t.querySelectorAll('thead th, .el-table__header th'))
      .map((th) => norm(th.innerText)).filter(Boolean);
    if (ths.length) seen.push(ths.join('|'));
  });
  return seen.slice(0, 5);
}
"""

# 表头别名：按长度倒序匹配，保证「意见说明」先于「意见」
_FIELD_ALIASES = (
    ("publish_time", ("发表意见时间", "发表时间", "操作时间", "处理时间", "时间", "日期")),
    ("staff_code",   ("人员工号", "员工工号", "操作人工号", "工号", "员工代码", "操作人")),
    ("staff_name",   ("姓名", "人员姓名", "操作人姓名")),
    ("role",         ("角色", "岗位", "处理角色")),
    ("org_name",     ("机构名称", "所属机构", "机构", "部门")),
    ("return_eval",  ("退回评价", "评价", "退回意见")),
    ("opinion_note", ("意见说明", "说明", "备注")),
    ("opinion",      ("处理意见", "意见内容", "意见")),
)

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M", "%Y年%m月%d日",
    "%m-%d %H:%M", "%m-%d",
)

_TIME_NOISE_RE = re.compile(r"(星期[一二三四五六日天]|周[一二三四五六日天])")


def _parse_time(text: str) -> Optional[datetime]:
    raw = _TIME_NOISE_RE.sub("", (text or "")).strip()
    if not raw:
        return None
    raw = raw.replace("T", " ")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # 带毫秒 / 额外字符时截取前 19 位再试
    m = re.match(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}([ ]\d{1,2}:\d{2}(:\d{2})?)?", raw)
    if m:
        return _parse_time(m.group(0))
    return None


def _map_fields(headers: list[str], cells: list[str]) -> dict:
    """按表头把单元格映射到字段；表头缺失时整行文本当意见"""
    row: dict[str, str] = {}
    if headers and len(headers) > 1:
        pairs = [(alias, field) for field, aliases in _FIELD_ALIASES for alias in aliases]
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        used: set[str] = set()
        for idx, head in enumerate(headers):
            if idx >= len(cells) or not head:
                continue
            val = (cells[idx] or "").strip()
            if not val:
                continue
            field = ""
            for alias, name in pairs:
                if head == alias:
                    field = name
                    break
            if not field:
                for alias, name in pairs:
                    if alias in head:
                        field = name
                        break
            if field and field not in used:
                row[field] = val
                used.add(field)
    return row


async def _extract(page: Page) -> Optional[dict]:
    """跨 frame 提取意见表格，返回首个命中结果"""
    for frame in page.frames:
        try:
            info = await frame.evaluate(_EXTRACT_JS)
        except Exception:
            continue
        if info and info.get("found"):
            return info
    return None


async def _diagnose(page: Page) -> list[str]:
    seen: list[str] = []
    for frame in page.frames:
        try:
            found = await frame.evaluate(_DIAG_JS)
        except Exception:
            continue
        if found:
            seen.extend([str(x) for x in found if x])
    return seen[:5]


def _latest(opinions: list[dict]) -> tuple[Optional[dict], bool]:
    """按发表时间取最新；时间都解析不了则取最后一行并标记 order_uncertain"""
    if not opinions:
        return None, False
    paired = [(i, _parse_time(o.get("publish_time", ""))) for i, o in enumerate(opinions)]
    parsed = [(i, ts) for i, ts in paired if ts is not None]
    if parsed:
        idx, _ = max(parsed, key=lambda x: (x[1], x[0]))
        return opinions[idx], False
    return opinions[-1], True


async def read_latest_opinion(page: Page, timeout: float = 8.0,
                              claim_no: str = "") -> Optional[dict]:
    """读取并返回最新一条意见（结构化 dict）；无意见/读不到返回 None"""
    deadline = time.time() + max(0.5, float(timeout))
    info: Optional[dict] = None
    while True:
        info = await _extract(page)
        if info and info.get("rows"):
            break
        if time.time() >= deadline:
            break
        await asyncio.sleep(0.5)

    if not info or not info.get("rows"):
        seen = await _diagnose(page)
        log_run(f"意见列表未读取到 | 报案号={claim_no or '?'} | 可见表头={seen or '无'}")
        return None

    headers = [str(h or "") for h in (info.get("headers") or [])]
    opinions: list[dict] = []
    for item in info.get("rows") or []:
        cells = [str(c or "") for c in (item.get("cells") or [])]
        text = str(item.get("text") or "").strip()
        row = _map_fields(headers, cells)
        row["raw_text"] = text
        if not row.get("opinion") and not row.get("opinion_note"):
            row["opinion"] = text
        if not (row.get("opinion") or row.get("opinion_note") or row.get("return_eval")):
            continue
        opinions.append(row)

    latest, order_uncertain = _latest(opinions)
    if not latest:
        log_run(f"意见列表为空 | 报案号={claim_no or '?'} | 表头={headers} | 类型={info.get('kind')}")
        return None

    latest["order_uncertain"] = order_uncertain
    latest["row_count"] = len(opinions)
    latest["source_kind"] = str(info.get("kind") or "")
    latest["headers"] = headers
    return latest


def format_opinion(opinion: Optional[dict], limit: int = 40) -> str:
    """审计/日志用的一行摘要"""
    if not opinion:
        return "无意见"
    parts = []
    for key, label in (("opinion", "意见"), ("opinion_note", "说明"),
                       ("return_eval", "退回评价")):
        val = str(opinion.get(key) or "").strip()
        if val:
            parts.append(f"{label}={val[:limit]}")
    if not parts:
        parts.append(str(opinion.get("raw_text") or "")[:limit])
    who = str(opinion.get("staff_code") or "").strip()
    role = str(opinion.get("role") or "").strip()
    when = str(opinion.get("publish_time") or "").strip()
    prefix = " ".join(x for x in (role, who, when) if x)
    return (f"{prefix} " if prefix else "") + " / ".join(parts)


def log_read(claim_no: str, opinion: Optional[dict]) -> None:
    log_audit(f"读取报价处理意见 | {claim_no} | {format_opinion(opinion)}")
