"""低碳信息列表采集（P4：只采集，不改变报价流程）

定损平台的零件表是 bootstrap-table：定损表全选框 id 为 `btSelectAllD-0`，
低碳表为 `btSelectAllB-0`，低碳列表通常放在「低碳信息」页签里。

策略：
1. 找到「低碳」页签 → 点击展开
2. 解析当前可见的配件表（按表头识别编码/名称/数量/价格）
3. 切回「定损」页签，避免影响后续 STEP
4. 结果落库 low_carbon_parts，并在日志里给出数量

任何一步失败只记录原因，不抛错、不阻断主流程。
"""
from __future__ import annotations

import json
from typing import Optional

from playwright.async_api import Page

import bot.config as cfg
from bot.logging_system import app_data_dir, log_audit, log_run

_SEG_KEYS = {
    "code": ("编码", "配件号", "零件号"),
    "name": ("名称",),
    "qty": ("数量",),
    "price": ("金额", "价格", "单价"),
}

_READ_TABLES_JS = """
() => {
  const out = [];
  document.querySelectorAll('table').forEach((t) => {
    const st = getComputedStyle(t);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    const box = t.getBoundingClientRect();
    if (box.width < 50 || box.height < 20) return;
    const heads = [...t.querySelectorAll('thead th')].map(th => (th.innerText || '').trim());
    const rows = [];
    t.querySelectorAll('tbody tr').forEach((tr) => {
      const cells = [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim());
      if (cells.some(c => c)) rows.push(cells);
    });
    if (heads.length || rows.length) out.push({heads, rows});
  });
  return out;
}
"""

_TAB_SELECTORS = [
    ".el-tabs__item:has-text('低碳')",
    "[role='tab']:has-text('低碳')",
    "li:has-text('低碳')",
    "a:has-text('低碳')",
    "span:has-text('低碳信息')",
    "div[class*='tab']:has-text('低碳信息')",
]
_BACK_SELECTORS = [
    ".el-tabs__item:has-text('定损')",
    "[role='tab']:has-text('定损')",
    "li:has-text('定损')",
    "a:has-text('定损')",
]


def enabled() -> bool:
    return bool(cfg.LOW_CARBON_COLLECT)


def _column_index(heads: list[str], kind: str) -> int:
    keys = _SEG_KEYS[kind]
    for i, h in enumerate(heads):
        if any(k in (h or "") for k in keys):
            return i
    return -1


async def _click_text(page: Page, selectors: list[str], label: str) -> bool:
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.scroll_into_view_if_needed(timeout=1500)
                await loc.click(timeout=3000)
                print(f"  [✓] 点击: {label}")
                log_audit(f"点击: {label}")
                return True
            except Exception:
                continue
    return False


def _parse(tables: list[dict]) -> list[dict]:
    best: list[dict] = []
    for table in tables:
        heads = [str(h or "") for h in (table.get("heads") or [])]
        rows = table.get("rows") or []
        if not rows:
            continue
        idx = {k: _column_index(heads, k) for k in _SEG_KEYS}
        if idx["code"] < 0 and idx["name"] < 0:
            continue
        parsed: list[dict] = []
        for cells in rows:
            def pick(key: str) -> str:
                i = idx[key]
                return str(cells[i]) if 0 <= i < len(cells) else ""
            code, name = pick("code"), pick("name")
            if not code and not name:
                continue
            parsed.append({
                "part_code": code,
                "part_name": name,
                "qty": pick("qty"),
                "price": pick("price"),
                "raw_json": json.dumps(cells, ensure_ascii=False),
            })
        if len(parsed) > len(best):
            best = parsed
    return best


def _store():
    from bot.pricing import get_price_service
    return get_price_service().store


async def collect(page: Page, claim_no: str = "") -> list[dict]:
    """读取低碳信息列表；读不到返回空列表（不抛错）"""
    if not enabled():
        return []

    # 低碳表已直接可见（无需切页签）时优先直读
    has_b_marker = False
    for frame in page.frames:
        try:
            if await frame.locator("#btSelectAllB-0").count() > 0:
                has_b_marker = True
                break
        except Exception:
            continue

    clicked = await _click_text(page, _TAB_SELECTORS, "低碳信息页签")
    if clicked:
        await page.wait_for_timeout(800)
    elif not has_b_marker:
        print("  [i] 未找到低碳信息页签/表格 → 视为无低碳配件")
        log_run(f"低碳信息：未找到入口 | 报案号={claim_no}")
        return []

    tables: list[dict] = []
    for frame in page.frames:
        try:
            tables.extend(await frame.evaluate(_READ_TABLES_JS) or [])
        except Exception:
            continue
    items = _parse(tables)

    if clicked:
        if not await _click_text(page, _BACK_SELECTORS, "定损信息页签"):
            print("  [!] 低碳信息页签已切换，但未找到返回定损页签入口")
            log_audit("低碳信息：未找到返回定损页签入口，请人工确认页面状态")

    if not items:
        print("  [i] 低碳信息列表为空 → 按普通配件流程")
        log_run(f"低碳信息：空 | 报案号={claim_no}")
        return []

    print(f"  [✓] 低碳信息列表 {len(items)} 个配件："
          f"{'、'.join((i['part_name'] or i['part_code']) for i in items[:5])}"
          f"{' …' if len(items) > 5 else ''}")
    log_audit(f"低碳信息 | {claim_no} | {len(items)} 个配件")
    try:
        _store().add_low_carbon_parts(claim_no, items)
    except Exception as exc:
        log_run(f"低碳信息落库失败: {exc}")
    return items


def stats() -> dict:
    try:
        return _store().low_carbon_stats()
    except Exception:
        return {"claims": 0, "parts": 0}


if __name__ == "__main__":
    s = stats()
    print(f"低碳信息：{s['claims']} 个案件 / {s['parts']} 个配件"
          f"（数据目录 {app_data_dir()}）")
