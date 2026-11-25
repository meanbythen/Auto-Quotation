"""空闲时段批量采集「报价处理意见」

路径：理赔作业中心 → 车险理赔 → 任务中心 → 我的任务
      → 流入时间「三月内」+ 状态「全部」→ 查询
      → 逐条点报案号打开报价处理页 → 读取意见/意见说明 → 落库 → 返回列表

只采集，不改动案件。任何一个案件失败只记录，继续下一个。
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from playwright.async_api import Page

from bot.intelligence import collector, reader
from bot.logging_system import log_audit, log_run

_MENU = ["车险理赔", "任务中心", "我的任务"]
_CLAIM_LINK = "td.el-table_1_column_2 span.bluetxt, td span.bluetxt"
_QUERY_BTN = ".footer-btns button:has-text('查询'), button:has-text('查询')"
_NEXT_BTN = ".el-pagination .btn-next, .btn-next"


def _log(msg: str) -> None:
    print(msg)
    log_run(msg)


def _stopped(stop_event) -> bool:
    try:
        return stop_event is not None and stop_event.is_set()
    except Exception:
        return False


async def _click_text(page: Page, text: str, timeout: float = 6.0) -> bool:
    """跨 frame 点击包含指定文字的第一个可见元素"""
    deadline = asyncio.get_event_loop().time() + timeout
    selectors = [
        f"li:has-text('{text}')", f"a:has-text('{text}')",
        f"span:has-text('{text}')", f"div[role='menuitem']:has-text('{text}')",
        f"span.el-tabs__item:has-text('{text}')",
    ]
    while asyncio.get_event_loop().time() < deadline:
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    await loc.scroll_into_view_if_needed(timeout=1500)
                    await loc.click(timeout=3000)
                    return True
                except Exception:
                    continue
        await asyncio.sleep(0.4)
    return False


async def _open_my_tasks(page: Page) -> bool:
    await page.bring_to_front()
    for name in _MENU:
        if not await _click_text(page, name):
            _log(f"采集意见 | 未找到菜单「{name}」")
            return False
        await page.wait_for_timeout(1200)
    # 等列表容器出现
    for _ in range(30):
        try:
            if await page.locator(_CLAIM_LINK).count() > 0:
                return True
            if await page.locator("input[placeholder*='报案号']").count() > 0:
                await page.wait_for_timeout(800)
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    _log("采集意见 | 我的任务列表未加载")
    return False


async def _ensure_filters(page: Page) -> None:
    for label, text in (("radio", "三月内"), ("checkbox", "全部")):
        sel = f"label.el-{label}:has-text('{text}')"
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            cls = (await loc.get_attribute("class") or "")
            if "is-checked" in cls:
                continue
            await loc.click(timeout=3000)
            await page.wait_for_timeout(300)
        except Exception:
            continue


async def _query(page: Page) -> bool:
    try:
        btn = page.locator(_QUERY_BTN).first
        await btn.wait_for(state="visible", timeout=5000)
        await btn.click(timeout=5000)
        await page.wait_for_timeout(1800)
        return True
    except Exception as exc:
        _log(f"采集意见 | 点击查询失败: {exc}")
        return False


async def _claim_numbers(page: Page, limit: int) -> list[str]:
    """读取当前查询结果里的报案号，自动翻页，最多 limit 个"""
    out: list[str] = []
    seen: set[str] = set()
    for _ in range(10):
        try:
            texts = await page.locator(_CLAIM_LINK).all_inner_texts()
        except Exception:
            texts = []
        for t in texts:
            no = (t or "").strip()
            if not no or no in seen:
                continue
            if not re.match(r"^RD[A-Z]{2,}", no):
                continue
            seen.add(no)
            out.append(no)
            if len(out) >= limit:
                return out
        # 翻页
        try:
            nxt = page.locator(_NEXT_BTN).first
            if await nxt.count() == 0:
                break
            cls = (await nxt.get_attribute("class") or "")
            disabled = ("is-disabled" in cls) or bool(await nxt.get_attribute("disabled"))
            if disabled:
                break
            before = out[-1] if out else ""
            await nxt.click(timeout=4000)
            await page.wait_for_timeout(1500)
            after = ""
            try:
                after = (await page.locator(_CLAIM_LINK).first.inner_text() or "").strip()
            except Exception:
                pass
            if before and after == before:
                break
        except Exception:
            break
    return out


async def _open_claim(page: Page, claim_no: str):
    """点击报案号，返回 (报价处理页, 是否新标签页)"""
    try:
        link = page.locator(_CLAIM_LINK).filter(has_text=claim_no).first
        if await link.count() == 0:
            link = page.locator(f"span.bluetxt:has-text('{claim_no}')").first
        await link.scroll_into_view_if_needed(timeout=3000)
        pages_before = len(page.context.pages)
        await link.click(timeout=5000)
        await page.wait_for_timeout(2000)
        if len(page.context.pages) > pages_before:
            quote = page.context.pages[-1]
            await quote.bring_to_front()
            try:
                await quote.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            return quote, True
        # 同页跳转
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return page, False
    except Exception as exc:
        _log(f"采集意见 | 打开案件失败 {claim_no}: {exc}")
        return None, False


async def _back_to_list(page: Page, quote, new_tab: bool) -> bool:
    """回到我的任务列表并可继续点下一条；失败返回 False（调用方应停止本轮）"""
    try:
        if new_tab:
            await quote.close()
            await page.bring_to_front()
        else:
            await page.go_back()
            await page.wait_for_timeout(1500)

        # 列表可能因页面回退而丢失筛选/隐藏，需确认真的可见
        visible = False
        try:
            loc = page.locator(_CLAIM_LINK)
            visible = await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            visible = False
        if visible:
            return True

        # 重新进入我的任务并查询
        if not await _open_my_tasks(page):
            return False
        await _ensure_filters(page)
        return await _query(page)
    except Exception as exc:
        _log(f"采集意见 | 返回列表失败: {exc}")
        return False


async def scan_my_tasks(page: Page, max_claims: int = 30,
                        stop_event=None, timeout: float = 8.0) -> dict:
    """批量采集我的任务里的报价处理意见；返回统计 dict"""
    result = {"claims": 0, "with_opinion": 0, "saved": 0, "failed": 0}

    _log("采集意见 | 打开我的任务（车险理赔 → 任务中心 → 我的任务）")
    if not await _open_my_tasks(page):
        return result
    await _ensure_filters(page)
    if not await _query(page):
        return result

    claims = await _claim_numbers(page, max(1, int(max_claims)))
    _log(f"采集意见 | 查询到 {len(claims)} 个案件（上限 {max_claims}）")
    if not claims:
        return result

    for idx, claim_no in enumerate(claims, 1):
        if _stopped(stop_event):
            _log(f"采集意见 | 收到停止指令，已采集 {idx - 1} 个")
            break
        result["claims"] += 1
        quote, new_tab = await _open_claim(page, claim_no)
        if quote is None:
            result["failed"] += 1
            continue
        try:
            opinion = await reader.read_latest_opinion(quote, timeout, claim_no=claim_no)
            if opinion:
                result["with_opinion"] += 1
                if collector.record_opinion(claim_no, opinion, source="scan"):
                    result["saved"] += 1
                _log(f"采集意见 | [{idx}/{len(claims)}] {claim_no} | "
                     f"{reader.format_opinion(opinion)}")
            else:
                _log(f"采集意见 | [{idx}/{len(claims)}] {claim_no} | 无意见")
        except Exception as exc:
            result["failed"] += 1
            log_run(f"采集意见 | {claim_no} 读取失败: {exc}")
        finally:
            ok = await _back_to_list(page, quote, new_tab)
            await page.wait_for_timeout(600)
            if not ok:
                _log("采集意见 | 无法返回我的任务列表，提前结束")
                break

    _log(f"采集意见 | 完成：案件 {result['claims']} | 有意见 {result['with_opinion']} | "
         f"入库 {result['saved']} | 失败 {result['failed']}")
    log_audit(f"采集意见完成 | 案件={result['claims']} 有意见={result['with_opinion']} "
              f"入库={result['saved']} 失败={result['failed']}")
    return result
