"""空闲时段批量采集「报价处理意见」

路径：理赔作业门户 → 左侧『任务中心』→『我的任务』
      → 流入时间「三月内」+ 状态「全部」→ 查询
      → 逐条点报案号打开报价处理页 → 读取意见/意见说明 → 落库 → 返回列表

『我的任务』与『平级移交』同属任务中心子菜单，平时折叠在悬浮弹层里（display:none），
按文字找可见元素永远找不到。因此和平级移交一样按三层方式打开：
  ① 已打开的『我的任务』标签页直接复用；
  ② 直连页面地址（生产实测 /claimcar/32180/myTask/index）；
  ③ 回门户读菜单 data-url 后新标签页直连；仍失败则派发菜单项点击事件。
只采集，不改动案件。任何一个案件失败只记录，继续下一个。
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import Page

import bot.config as cfg
from bot.intelligence import collector, reader
from bot.logging_system import log_audit, log_run

# 我的任务页面（生产 DOM，2026-11-25 F12 保存页）
_QUERY_BTN = ".footer-btns button:has-text('查询')"
_CLAIM_LINK = "td.el-table_1_column_2 span.bluetxt"
_CLAIM_ANY = "td.el-table_1_column_2 span.bluetxt, td span.bluetxt"
_NEXT_BTN = ".el-pagination .btn-next"
_PAGE_ACTIVE = ".el-pagination .el-pager li.number.active"
_TOTAL = ".el-pagination__total"
_RADIO_MONTH3 = "label.el-radio:has-text('三月内')"
_CHECK_ALL = "label.el-checkbox:has-text('全部')"
_LOADING = ".el-loading-mask"

# 门户侧边菜单项：<div class="menu-item-text" data-url="/claimcar/myTask/Index">
_MENU_MY_TASK = ".menu-item-text[data-url*='myTask']"
_JS_CLICK_MENU = """
() => {
  const items = Array.from(document.querySelectorAll('.menu-item-text'));
  const it = items.find(e => (e.getAttribute('data-url') || '').toLowerCase().includes('mytask'))
          || items.find(e => (e.innerText || '').trim().startsWith('我的任务'));
  if (!it) return false;
  it.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  return true;
}
"""


def _log(msg: str) -> None:
    print(msg)
    log_run(msg)


def _stopped(stop_event) -> bool:
    try:
        return stop_event is not None and stop_event.is_set()
    except Exception:
        return False


def _my_task_urls() -> list[str]:
    urls: list[str] = []
    for url in (getattr(cfg, "MY_TASK_URL", ""), getattr(cfg, "MY_TASK_URL_ALT", "")):
        if url and url not in urls:
            urls.append(url)
    for url in getattr(cfg, "MY_TASK_URL_ALTS", ()) or ():
        if url and url not in urls:
            urls.append(url)
    return urls


async def _live_page(page: Page) -> Page:
    """页面被关闭时从同一 context 取其它 tab"""
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass
    try:
        for p in page.context.pages:
            if not p.is_closed():
                return p
    except Exception:
        pass
    return page


async def _my_tasks_ready(page: Page) -> bool:
    """我的任务页渲染完成：出现查询按钮或结果表里的报案号链接"""
    try:
        if page.is_closed():
            return False
        if await page.locator(_QUERY_BTN).count() > 0:
            return True
        return await page.locator(_CLAIM_LINK).count() > 0
    except Exception:
        return False


async def _find_my_task_tab(page: Page) -> Optional[Page]:
    try:
        pages = [p for p in page.context.pages if not p.is_closed()]
    except Exception:
        pages = [page]
    for p in pages:
        try:
            if "mytask" in (p.url or "").lower() and await _my_tasks_ready(p):
                return p
        except Exception:
            continue
    return None


async def _goto_my_task(page: Page, url: str) -> bool:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        _log(f"采集意见 | 直连失败 {url}：{type(exc).__name__}")
        return False
    for _ in range(30):
        if await _my_tasks_ready(page):
            return True
        await page.wait_for_timeout(500)
    return False


async def _menu_data_url(page: Page) -> str:
    for _ in range(20):
        try:
            loc = page.locator(_MENU_MY_TASK).first
            if await loc.count() > 0:
                return (await loc.get_attribute("data-url") or "").strip()
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return ""


async def _open_my_tasks(page: Page) -> Optional[Page]:
    """打开我的任务页，返回承载页（可能是复用/新开的标签页）；失败返回 None"""
    page = await _live_page(page)

    existing = await _find_my_task_tab(page)
    if existing is not None:
        await existing.bring_to_front()
        _log("采集意见 | 复用已打开的『我的任务』标签页")
        return existing

    # ① 直连已知地址
    for url in _my_task_urls():
        if await _goto_my_task(page, url):
            print(f"  [✓] 已打开『我的任务』（直连 {url}）")
            log_audit(f"我的任务直连成功: {url}")
            return page

    # ② 回门户读菜单 data-url（菜单折叠也能读到），新标签页直连
    try:
        await page.goto(cfg.PORTAL_REAL_URL, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    menu_url = await _menu_data_url(page)
    if menu_url:
        full = urljoin(page.url or cfg.PORTAL_REAL_URL, menu_url)
        try:
            tab = await page.context.new_page()
        except Exception:
            tab = None
        if tab is not None:
            if await _goto_my_task(tab, full):
                print(f"  [✓] 已打开『我的任务』（菜单 data-url: {menu_url}）")
                log_audit(f"我的任务 data-url 打开成功: {menu_url}")
                return tab
            try:
                await tab.close()
            except Exception:
                pass

    # ③ 派发菜单项点击事件（菜单在弹层里不可见，但 Vue 的点击处理仍会响应）
    try:
        before = {id(p) for p in page.context.pages}
    except Exception:
        before = set()
    try:
        clicked = await page.evaluate(_JS_CLICK_MENU)
    except Exception:
        clicked = False
    if clicked:
        for _ in range(30):
            try:
                new_pages = [p for p in page.context.pages
                             if id(p) not in before and not p.is_closed()]
            except Exception:
                new_pages = []
            for np in new_pages:
                if await _my_tasks_ready(np):
                    await np.bring_to_front()
                    print("  [✓] 已打开『我的任务』（菜单点击）")
                    log_audit("我的任务页已打开（菜单点击）")
                    return np
            if await _my_tasks_ready(page):
                return page
            await page.wait_for_timeout(500)

    _log("采集意见 | 未找到『我的任务』页面（直连 / data-url / 菜单点击均失败）")
    return None


async def _ensure_filters(page: Page) -> None:
    """流入时间『三月内』+ 状态『全部』（已勾选则跳过，避免反选）"""
    for sel, label in ((_RADIO_MONTH3, "三月内"), (_CHECK_ALL, "全部")):
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            cls = (await loc.get_attribute("class") or "")
            if "is-checked" in cls:
                continue
            await loc.scroll_into_view_if_needed(timeout=2000)
            await loc.click(timeout=3000)
            await page.wait_for_timeout(300)
            print(f"  [✓] 查询条件 → {label}")
        except Exception:
            continue


async def _wait_loaded(page: Page, timeout: float = 12.0) -> None:
    """等 el-table 的『拼命加载中』遮罩消失并出现数据行"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            masks = page.locator(_LOADING)
            visible = False
            for i in range(min(await masks.count(), 3)):
                if await masks.nth(i).is_visible():
                    visible = True
                    break
            if not visible:
                break
        except Exception:
            break
        await page.wait_for_timeout(300)
    try:
        await page.wait_for_selector(".el-table__row", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(400)


async def _query(page: Page) -> bool:
    try:
        btn = page.locator(_QUERY_BTN).first
        await btn.wait_for(state="visible", timeout=8000)
        await btn.click(timeout=5000)
    except Exception as exc:
        _log(f"采集意见 | 点击查询失败: {exc}")
        return False
    await _wait_loaded(page)
    try:
        total = (await page.locator(_TOTAL).first.inner_text(timeout=2000) or "").strip()
        if total:
            _log(f"采集意见 | 查询结果 {total}")
    except Exception:
        pass
    return True


def _is_claim_no(text: str) -> bool:
    return bool(re.match(r"^RD[A-Z0-9]{6,}$", (text or "").strip()))


async def _page_claims(page: Page) -> list[str]:
    """读取当前页的报案号（第 2 列蓝字链接）"""
    out: list[str] = []
    seen: set[str] = set()
    try:
        texts = await page.locator(_CLAIM_LINK).all_inner_texts()
    except Exception:
        return out
    for t in texts:
        no = (t or "").strip()
        if not no or no in seen or not _is_claim_no(no):
            continue
        seen.add(no)
        out.append(no)
    return out


async def _active_page_no(page: Page) -> str:
    try:
        return (await page.locator(_PAGE_ACTIVE).first.inner_text(timeout=1500) or "").strip()
    except Exception:
        return ""


async def _next_page(page: Page) -> bool:
    """翻到下一页；已是最后一页返回 False"""
    try:
        nxt = page.locator(_NEXT_BTN).first
        if await nxt.count() == 0 or await nxt.is_disabled():
            return False
        before = await _active_page_no(page)
        await nxt.click(timeout=4000)
    except Exception:
        return False
    await _wait_loaded(page)
    for _ in range(20):
        after = await _active_page_no(page)
        if after and after != before:
            return True
        await page.wait_for_timeout(300)
    return False


async def _open_claim(page: Page, claim_no: str):
    """点击报案号，返回 (报价处理页, 是否新标签页)"""
    try:
        link = page.locator(_CLAIM_LINK).filter(has_text=claim_no).first
        if await link.count() == 0:
            link = page.locator(_CLAIM_ANY).filter(has_text=claim_no).first
        if await link.count() == 0:
            _log(f"采集意见 | 当前页找不到报案号 {claim_no}")
            return None, False
        await link.scroll_into_view_if_needed(timeout=3000)
        pages_before = len(page.context.pages)
        await link.click(timeout=6000)
        await page.wait_for_timeout(2500)
        if len(page.context.pages) > pages_before:
            quote = page.context.pages[-1]
            await quote.bring_to_front()
            try:
                await quote.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            return quote, True
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return page, False
    except Exception as exc:
        _log(f"采集意见 | 打开案件失败 {claim_no}: {exc}")
        return None, False


async def _list_visible(page: Page) -> bool:
    try:
        loc = page.locator(_CLAIM_LINK)
        return await loc.count() > 0 and await loc.first.is_visible()
    except Exception:
        return False


async def _back_to_list(list_page: Page, quote, new_tab: bool) -> tuple[Page, bool, bool]:
    """返回 (列表页, 是否可继续, 是否重新查询过列表)"""
    try:
        if new_tab:
            try:
                await quote.close()
            except Exception:
                pass
            await list_page.bring_to_front()
        else:
            await list_page.go_back()
            await list_page.wait_for_timeout(1500)
    except Exception as exc:
        _log(f"采集意见 | 返回列表失败: {exc}")

    # 列表可能因页面回退而丢失筛选/隐藏，需确认真的可见
    if await _list_visible(list_page):
        return list_page, True, False

    _log("采集意见 | 列表状态丢失，重新打开我的任务并查询")
    fresh = await _open_my_tasks(list_page)
    if fresh is None:
        return list_page, False, True
    await _ensure_filters(fresh)
    if not await _query(fresh):
        return fresh, False, True
    return fresh, True, True


async def scan_my_tasks(page: Page, max_claims: int = 30,
                        stop_event=None, timeout: float = 8.0) -> dict:
    """批量采集我的任务里的报价处理意见；返回统计 dict"""
    result = {"claims": 0, "with_opinion": 0, "saved": 0, "failed": 0}
    limit = max(1, int(max_claims))

    _log("采集意见 | 打开我的任务（任务中心 → 我的任务）")
    list_page = await _open_my_tasks(page)
    if list_page is None:
        return result
    await _ensure_filters(list_page)
    if not await _query(list_page):
        return result

    seen: set[str] = set()
    guard = 0
    while len(seen) < limit and not _stopped(stop_event):
        guard += 1
        if guard > 300:
            _log("采集意见 | 翻页次数过多，提前结束")
            break
        nums = await _page_claims(list_page)
        todo = [n for n in nums if n not in seen]
        if not todo:
            if await _next_page(list_page):
                continue
            break

        reset = False
        for claim_no in todo:
            if len(seen) >= limit or _stopped(stop_event):
                break
            seen.add(claim_no)
            result["claims"] += 1
            idx = len(seen)
            quote, new_tab = await _open_claim(list_page, claim_no)
            if quote is None:
                result["failed"] += 1
                if not await _list_visible(list_page):
                    fresh = await _open_my_tasks(list_page)
                    if fresh is None:
                        break
                    await _ensure_filters(fresh)
                    if not await _query(fresh):
                        break
                    list_page = fresh
                continue
            ok = True
            try:
                opinion = await reader.read_latest_opinion(quote, timeout, claim_no=claim_no)
                if opinion:
                    result["with_opinion"] += 1
                    if collector.record_opinion(claim_no, opinion, source="scan"):
                        result["saved"] += 1
                    _log(f"采集意见 | [{idx}/{limit}] {claim_no} | "
                         f"{reader.format_opinion(opinion)}")
                else:
                    _log(f"采集意见 | [{idx}/{limit}] {claim_no} | 无意见")
            except Exception as exc:
                result["failed"] += 1
                log_run(f"采集意见 | {claim_no} 读取失败: {exc}")
            finally:
                list_page, ok, reset = await _back_to_list(list_page, quote, new_tab)
                await list_page.wait_for_timeout(500)
            if not ok:
                _log("采集意见 | 无法返回我的任务列表，提前结束")
                return _finish(result)
            if reset:
                # 列表被重新查询（回到第 1 页），重新读当前页并按 seen 跳过
                break

    return _finish(result)


def _finish(result: dict) -> dict:
    _log(f"采集意见 | 完成：案件 {result['claims']} | 有意见 {result['with_opinion']} | "
         f"入库 {result['saved']} | 失败 {result['failed']}")
    log_audit(f"采集意见完成 | 案件={result['claims']} 有意见={result['with_opinion']} "
              f"入库={result['saved']} 失败={result['failed']}")
    return result
