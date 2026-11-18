"""
工作记录 Excel —— 三 Sheet 模式（给用户看的工作日志）

Sheet 1:  "理赔报价记录"   每报案号一行：处理时间、处理结果、卡在哪一步、是否改了报价
Sheet 2:  "统计汇总"       按日期汇总：处理数 / 完成 / 跳过 / 修改报价案件 / 修改零件数 …
Sheet 3:  "零件明细"       每零件一行（可钻取、做 Pivot Table）

文件位置:
  data/ledger/工作记录_YYYYMM.xlsx   (按月自动分文件)
"""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# ========= Sheet 1: 主表（每报案号一行） =========
MAIN_HEADERS = [
    "处理时间", "报案号", "车牌号", "VIN",
    "零件数", "合规数", "超市场价数",
    "是否修改报价", "修改零件数", "处理结果", "跳过步骤", "备注",
]
MAIN_WIDTHS = [18, 24, 12, 22, 8, 8, 12, 13, 12, 14, 12, 34]

# ========= Sheet 2: 统计汇总 =========
STATS_HEADERS = [
    "日期", "处理总数", "全流程完成", "跳过(卡在某步)", "需人工",
    "修改报价案件", "修改零件数", "零件总数", "超市场价零件数",
]
STATS_WIDTHS = [14, 11, 12, 15, 10, 14, 12, 11, 15]

# ========= Sheet 3: 明细表（每零件一行） =========
DETAIL_HEADERS = [
    "时间", "报案号", "VIN",
    "序号", "零件编码", "零件名称",
    "价格方案", "价格类型代码", "S价格值(¥)",
    "配件类型",
    "原报价(¥)", "市场价(¥)", "提交报价(¥)",
    "合规?", "修复?", "修复率(%)", "超市场价比",
]

# 处理结果文案（写进 Excel，用户一眼能看懂）
RESULT_TEXT = {
    "done": "全流程完成",
    "skipped": "跳过",
    "blocked": "需人工",
    "failed": "失败",
}


# ---------- 基础 ----------
def _ledger_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent.parent
    d = base / "data" / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_path() -> Path:
    now = datetime.now()
    return _ledger_dir() / f"工作记录_{now.strftime('%Y%m')}.xlsx"


def current_ledger_path() -> Path:
    """给 GUI / 命令行用的当前月工作记录路径"""
    return _current_path()


def _thin():
    s = Side(border_style="thin", color="e0e0e0")
    return Border(left=s, right=s, top=s, bottom=s)


def _style_header(ws, headers, col_widths):
    """给 ws 的第一行写 headers + 表头样式 + 列宽"""
    from openpyxl.styles import Font, PatternFill, Alignment
    ws.append(headers)
    hfont = Font(bold=True, color="FFFFFF", size=11)
    hfill = PatternFill(start_color="00c8ff", end_color="00c8ff", fill_type="solid")
    for c, _ in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c)
        cell.font = hfont
        cell.fill = hfill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _thin()
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _num(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _split_step(note: str) -> str:
    """从备注里抠出『卡在 STEP 6：…』的步骤号，只保留 STEP + 编号"""
    import re
    text = str(note or "")
    m = re.search(r"STEP\s*\d+[0-9A-Za-z]*", text)
    if not m:
        return ""
    step = m.group(0).upper()
    return step if step.startswith("STEP ") else "STEP " + step[4:]


def _result_from_old(status: str, note: str = "") -> str:
    """把旧版 'done'/'skipped'/'blocked' 以及可能的中文转成统一文案"""
    s = str(status or "").strip()
    if s in RESULT_TEXT:
        return RESULT_TEXT[s]
    if s.startswith("全流程"):
        return "全流程完成"
    if "跳过" in s:
        return "跳过"
    if "人工" in s or "blocked" in s:
        return "需人工"
    if "失败" in s or "failed" in s:
        return "失败"
    return s or "未知"


def _migrate_main_sheet(wb):
    """旧版 10 列主表 → 新版 12 列主表（保留历史数据，不丢行）"""
    from openpyxl import Workbook  # noqa: F401  (确保 openpyxl 可用)
    ws_old = wb["理赔报价记录"]
    header = [str(c.value or "").strip() for c in ws_old[1]]
    if header == MAIN_HEADERS:
        return ws_old

    old_rows = [list(r) for r in ws_old.iter_rows(min_row=2, values_only=True)]
    idx = {name: i for i, name in enumerate(header)}
    col = lambda row, name: (row[idx[name]] if name in idx and idx[name] < len(row) else None)

    ws_new = wb.create_sheet("理赔报价记录_new")
    _style_header(ws_new, MAIN_HEADERS, MAIN_WIDTHS)
    for row in old_rows:
        if not row or not any(v not in (None, "") for v in row):
            continue
        note = col(row, "备注") or ""
        ws_new.append([
            col(row, "时间") or col(row, "处理时间"),
            col(row, "报案号"),
            col(row, "车牌号"),
            col(row, "VIN"),
            col(row, "零件数"),
            col(row, "合规数"),
            col(row, "超市场价数"),
            col(row, "是否修复") or col(row, "是否修改报价"),
            col(row, "修改零件数"),
            _result_from_old(col(row, "最终状态") or col(row, "处理结果"), note),
            col(row, "跳过步骤") or _split_step(note),
            note,
        ])
        _paint_row(ws_new, ws_new.max_row, len(MAIN_HEADERS))

    wb.remove(ws_old)
    ws_new.title = "理赔报价记录"
    wb.move_sheet(ws_new, offset=-(len(wb.sheetnames) - 1))
    return ws_new


def _ensure_wb(path: Path):
    """确保 wb 存在，三个 sheet 齐全且表头是当前版本"""
    from openpyxl import Workbook, load_workbook

    if path.exists() and path.stat().st_size > 0:
        wb = load_workbook(path)
        if "理赔报价记录" not in wb.sheetnames:
            ws1 = wb.create_sheet("理赔报价记录", 0)
            _style_header(ws1, MAIN_HEADERS, MAIN_WIDTHS)
        if "零件明细" not in wb.sheetnames:
            ws2 = wb.create_sheet("零件明细")
            _style_header(ws2, DETAIL_HEADERS,
                          [18, 24, 22, 6, 26, 22, 10, 12, 12, 12, 8, 8, 10, 12])
        # 主表表头升级（旧版 10 列 → 新版 12 列）
        _migrate_main_sheet(wb)
        return wb

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "理赔报价记录"
    _style_header(ws1, MAIN_HEADERS, MAIN_WIDTHS)

    ws2 = wb.create_sheet("零件明细")
    _style_header(ws2, DETAIL_HEADERS,
                  [18, 24, 22, 6, 26, 22, 10, 12, 12, 12, 8, 8, 10, 12])

    wb.save(path)
    return wb


def _paint_row(ws, row_idx, col_count, status_value=None):
    """隔行底色 + 边框 + 处理结果着色"""
    from openpyxl.styles import PatternFill, Font
    thin_fill = PatternFill(start_color="f5faff", end_color="f5faff", fill_type="solid")
    for c in range(1, col_count + 1):
        cell = ws.cell(row=row_idx, column=c)
        cell.border = _thin()
        if row_idx % 2 == 0:
            cell.fill = thin_fill
    if status_value is not None and col_count >= 10:
        sc = ws.cell(row=row_idx, column=10)  # 处理结果列
        color = {"done": "00a854", "全流程完成": "00a854",
                 "blocked": "ffb547", "需人工": "ffb547",
                 "skipped": "d97706", "跳过": "d97706"}.get(status_value, "ff4d6a")
        sc.font = Font(bold=True, color=color)


# ---------- 统计汇总 ----------
_AGG_KEYS = ["total", "done", "skip", "blocked", "fixed_case", "fixed_parts", "parts", "over"]


def _aggregate(rows) -> tuple[list[list], dict]:
    """把 12 列主表数据聚合成按日期的统计行 + 合计"""
    agg: dict[str, dict] = {}
    for row in rows:
        if not row or not row[0]:
            continue
        day = str(row[0])[:10]
        item = agg.setdefault(day, {k: 0 for k in _AGG_KEYS})
        result = str(row[9] or "")
        item["total"] += 1
        if "完成" in result:
            item["done"] += 1
        elif "跳过" in result or "失败" in result:
            item["skip"] += 1          # 失败＝卡在某步没走完
        else:
            item["blocked"] += 1
        if str(row[7] or "") == "是":
            item["fixed_case"] += 1
        item["fixed_parts"] += int(_num(row[8]))
        item["parts"] += int(_num(row[4]))
        item["over"] += int(_num(row[6]))

    out: list[list] = []
    totals = {k: 0 for k in _AGG_KEYS}
    for day in sorted(agg):
        it = agg[day]
        out.append([day] + [it[k] for k in _AGG_KEYS])
        for k in _AGG_KEYS:
            totals[k] += it[k]
    return out, totals


def rebuild_stats(wb) -> None:
    """按『理赔报价记录』重算『统计汇总』，永远与明细一致"""
    from openpyxl import Workbook  # noqa: F401
    if "统计汇总" in wb.sheetnames:
        del wb["统计汇总"]
    ws = wb.create_sheet("统计汇总", 1)
    _style_header(ws, STATS_HEADERS, STATS_WIDTHS)

    ws_main = wb["理赔报价记录"]
    stat_rows, totals = _aggregate(
        list(ws_main.iter_rows(min_row=2, values_only=True)))

    for row in stat_rows:
        ws.append(row)
        _paint_row(ws, ws.max_row, len(STATS_HEADERS))

    ws.append(["合计"] + [totals[k] for k in _AGG_KEYS])
    from openpyxl.styles import Font
    for c in range(1, len(STATS_HEADERS) + 1):
        cell = ws.cell(row=ws.max_row, column=c)
        cell.border = _thin()
        cell.font = Font(bold=True, color="0077cc")


# ---------- 主入口 ----------
def record(claim_no: str, vehicle=None, parts: list = None,
           status: str = "done", note: str = "",
           skip_step: str = "") -> Optional[str]:
    """
    追加一个报案号的完整记录（主表 1 行 + 明细表 N 行 + 重算统计）
    skip_step: 跳过时卡在哪一步，如 "STEP 6"
    """
    if not HAS_OPENPYXL:
        print("  [!] openpyxl 未安装，跳过 Excel 记录")
        return None

    try:
        path = _current_path()
        wb = _ensure_wb(path)
        ws_main = wb["理赔报价记录"]
        ws_detail = wb["零件明细"]

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = parts or []

        ok_count   = sum(1 for p in parts if getattr(p, "within_market", True))
        over_count = sum(1 for p in parts if getattr(p, "within_market", None) is False)
        fixed_count = sum(1 for p in parts if getattr(p, "fixed_price", None) is not None)
        fixed_yn = "是" if fixed_count > 0 else "否"

        plate = getattr(vehicle, "plate", "") or ""
        vin   = getattr(vehicle, "vin", "") or ""
        result_text = RESULT_TEXT.get(status, status)
        step_text = skip_step or ("" if status == "done" else _split_step(note))

        # ===== Sheet 1: 主表 1 行 =====
        ws_main.append([
            now_str, claim_no, plate, vin,
            len(parts), ok_count, over_count,
            fixed_yn, fixed_count, result_text, step_text, note,
        ])
        _paint_row(ws_main, ws_main.max_row, len(MAIN_HEADERS), result_text)

        # ===== Sheet 3: 明细表 N 行 =====
        total = len(parts) or 1
        over_ratio_pct = round(over_count * 100 / total, 1)

        for i, p in enumerate(parts, 1):
            local_price  = getattr(p, "local_price", None)
            market_price = getattr(p, "market_price", None)
            final_price  = getattr(p, "fixed_price", None) or local_price
            within       = getattr(p, "within_market", None)
            is_fixed     = getattr(p, "fixed_price", None) is not None

            if market_price and market_price > 0 and is_fixed:
                fix_rate = round((final_price - local_price) * 100 / local_price, 1)
            else:
                fix_rate = "-"

            ws_detail.append([
                now_str, claim_no, vin,
                i,
                getattr(p, "code", "") or "",
                getattr(p, "name", "") or "",
                # ===== 新增：价格方案 + S价格值（排查比价逻辑的关键证据） =====
                getattr(p, "price_scheme", "") or "",
                getattr(p, "price_type_code", "") or "",
                getattr(p, "s_price_value", None) or "-",
                # ===== 以下原有字段 =====
                getattr(p, "price_type", "") or "",
                local_price or "-",
                market_price or "-",
                final_price or "-",
                "✅" if within is True else ("❌" if within is False else "?"),
                "是" if is_fixed else "否",
                fix_rate,
                f"{over_ratio_pct}%" if i == 1 else "",
            ])
            _paint_row(ws_detail, ws_detail.max_row, len(DETAIL_HEADERS))

        # ===== Sheet 2: 统计汇总（每次重算，保证与明细一致） =====
        rebuild_stats(wb)

        wb.save(path)
        return str(path)

    except Exception as e:
        print(f"  [!] Excel 写入失败: {e}")
        import traceback; traceback.print_exc()
        return None


# ---------- 供 GUI 读取 ----------
def load_records(limit: int = 500) -> tuple[list[list], list[list]]:
    """读取当前月工作记录：返回 (主表数据行, 统计汇总数据行)，只取数据行不含表头"""
    if not HAS_OPENPYXL:
        return [], []
    path = _current_path()
    if not path.exists():
        return [], []
    try:
        wb = load_workbook(path, data_only=True)
    except Exception:
        return [], []
    main_rows: list[list] = []
    stats_rows: list[list] = []
    if "理赔报价记录" in wb.sheetnames:
        ws = wb["理赔报价记录"]
        header = [str(c.value or "").strip() for c in ws[1]]
        if header != MAIN_HEADERS:  # 旧文件 → 内存里映射成新列顺序
            idx = {n: i for i, n in enumerate(header)}
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0]:
                    continue
                note = row[idx["备注"]] if "备注" in idx and idx["备注"] < len(row) else ""
                main_rows.append([
                    row[0],
                    row[idx.get("报案号", 1)] if idx.get("报案号", 1) < len(row) else "",
                    row[idx.get("车牌号", 2)] if idx.get("车牌号", 2) < len(row) else "",
                    row[idx.get("VIN", 3)] if idx.get("VIN", 3) < len(row) else "",
                    row[idx.get("零件数", 4)] if idx.get("零件数", 4) < len(row) else "",
                    row[idx.get("合规数", 5)] if idx.get("合规数", 5) < len(row) else "",
                    row[idx.get("超市场价数", 6)] if idx.get("超市场价数", 6) < len(row) else "",
                    row[idx.get("是否修复", 7)] if idx.get("是否修复", 7) < len(row) else "",
                    "",
                    _result_from_old(row[idx.get("最终状态", 8)] if idx.get("最终状态", 8) < len(row) else "", note),
                    _split_step(note),
                    note,
                ])
        else:
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row and row[0]:
                    main_rows.append(list(row))
    if "统计汇总" in wb.sheetnames:
        for row in wb["统计汇总"].iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                stats_rows.append(list(row))
    else:
        # 旧文件没有统计 Sheet → 在内存里按主表补算，保证 GUI 统计表不为空
        stat_rows, totals = _aggregate(main_rows)
        stats_rows = list(stat_rows)
        if totals.get("total"):
            stats_rows.append(["合计"] + [totals[k] for k in _AGG_KEYS])
    if limit and len(main_rows) > limit:
        main_rows = main_rows[-limit:]
    return main_rows, stats_rows
