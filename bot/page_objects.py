"""
页面交互封装（Page Object） —— DOM 优先策略
核心变化：一车一件拆成两页 —— 首页输 VIN → 查询 → 跳详情页 → 详情页输零件编码
"""
from __future__ import annotations
import asyncio
import random
import re
import threading as _th
import time
from typing import List, Optional, Sequence

from playwright.async_api import Page, Locator, expect
import bot.config as cfg
from bot.config import (
    DEFAULT_TIMEOUT, STEP_DELAY_MIN, STEP_DELAY_MAX,
    SELECTORS_PORTAL, get_market_selectors,
    MOCK_URLS, TASK_TRANSFER_URL, TRANSFER_TARGET_DEFAULT,
    TRANSFER_SUBMIT_PATH, TRANSFER_GROUP_CODE,
    TRANSFER_GROUP_OPTION, TRANSFER_GROUP_OPTION_KEY, TRANSFER_GROUP_OPTION_EXCLUDE,
    TRANSFER_SUBMIT_PATH_BRANCH, TRANSFER_GROUP_CODE_BRANCH,
    TRANSFER_GROUP_OPTION_BRANCH, TRANSFER_GROUP_OPTION_KEY_BRANCH,
)
from bot.logging_system import log_audit
from bot.models import PartItem, VehicleInfo


async def human_delay(min_ms: int = STEP_DELAY_MIN, max_ms: int = STEP_DELAY_MAX) -> None:
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000.0)


def _accept_dialog(d) -> None:
    """接受原生弹窗；同一弹窗可能被多处监听器处理，重复提交会报错 → 静默吞掉"""
    async def _run():
        try:
            await d.accept()
        except Exception:
            pass
    asyncio.create_task(_run())


async def safe_click(locator: Locator, label: str = "", timeout: int = DEFAULT_TIMEOUT) -> None:
    await locator.wait_for(state="visible", timeout=timeout)
    await locator.scroll_into_view_if_needed(timeout=timeout)
    await locator.click(timeout=timeout)
    if label:
        print(f"  [✓] 点击: {label}")
        log_audit(f"点击: {label}")


async def safe_fill(locator: Locator, value: str, label: str = "") -> None:
    await locator.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
    await locator.click()
    await locator.fill("")
    await locator.fill(value)
    if label:
        print(f"  [✓] 输入: {label}")
        log_audit(f"输入: {label} = {value}")


# ---------- 人工操作等效：状态检测辅助 ----------
async def click_if_present(locator: Locator, label: str = "", timeout: int = 3000) -> bool:
    """
    幂等点击：元素存在且可见才点击；否则视为「人工已操作 / 状态已达成」，跳过并返回 False。
    让单步调试时人工点击和程序点击等效——程序看的是页面状态，不是谁点的。
    """
    try:
        await locator.first.wait_for(state="visible", timeout=timeout)
    except Exception:
        print(f"  [i] 未找到「{label}」→ 判定已人工操作或状态已达成，跳过点击")
        log_audit(f"跳过点击（未找到，可能人工已操作）: {label}")
        return False
    await safe_click(locator.first, label, timeout=timeout)
    return True


async def is_visible_now(locator: Locator, timeout: int = 800) -> bool:
    """快速探测元素当前是否可见（不阻塞）"""
    try:
        await locator.first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


# ---------- 滚动 + 文字找按钮（真实门户按钮在页面下方、要滚动才出现，且没有稳定 id） ----------
_SCAN_CLICKABLE_JS = """
([kws, maxLen]) => {
  const k = kws.map(s => String(s).toLowerCase());
  // 含 Shadow DOM：真实门户的 Vue 组件可能把操作条挂在 shadowRoot 里
  const roots = [document];
  for (let i = 0; i < roots.length && i < 12; i++) {
    roots[i].querySelectorAll('*').forEach((el) => {
      if (el.shadowRoot) roots.push(el.shadowRoot);
    });
  }
  const all = [];
  roots.forEach((r) => {
    all.push(...r.querySelectorAll(
      'a,button,input[type=button],input[type=submit],span,div,li,td,label,img'));
  });
  const cands = [];
  for (const el of all) {
    let t = '';
    if (el.tagName === 'INPUT') t = el.value || el.getAttribute('title') || '';
    else if (el.tagName === 'IMG') t = el.getAttribute('alt') || el.getAttribute('title') || '';
    else t = el.innerText || el.textContent || '';
    t = (t || '').trim();
    if (!t || t.length > maxLen) continue;
    const tl = t.toLowerCase();
    if (!k.some(x => tl === x || tl.includes(x))) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    let score = 0;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' || tag === 'a' || tag === 'input') score += 30;
    if (st.cursor === 'pointer') score += 12;
    if (k.some(x => tl === x)) score += 40;
    if (el.children.length === 0) score += 10;
    if (el.getAttribute('onclick')) score += 6;
    score -= Math.min(15, Math.round((r.width * r.height) / 6000));
    cands.push({el, score, t, tag});
  }
  cands.sort((a, b) => b.score - a.score);
  if (!cands.length) return {found: false};
  document.querySelectorAll('[data-bot-pick]').forEach(e => e.removeAttribute('data-bot-pick'));
  const best = cands[0];
  best.el.setAttribute('data-bot-pick', '1');
  best.el.scrollIntoView({block: 'center', inline: 'center'});
  return {found: true, tag: best.tag, text: best.t,
          count: cands.length, top: cands.slice(0, 6).map(c => c.tag + ':' + c.t)};
}
"""


# 生产报价页实测：底部固定条 <div class="picc-bottom-bar buttonClass">
#   <div class="right-link-button"> 返回(F) / 暂存(S) / <button class="el-button--text el-button--primary">提交(D)</button>
QUOTE_SUBMIT_SELECTORS = [
    SELECTORS_PORTAL["quote_submit_btn"],
    ".picc-bottom-bar .right-link-button button:has-text('提交')",
    ".right-link-button button:has-text('提交')",
    "button:has-text('提交(D)')",
    "button:has-text('提交')", "input[value='提交']", "a:has-text('提交')",
]


async def find_clickable(page: Page, selectors: Sequence[str], keywords: Sequence[str],
                         label: str = "", timeout: int = 8000) -> tuple:
    """找可点击元素并滚动到可视区域，返回 (frame, locator)

    顺序：CSS 选择器（主页面 + 所有 iframe）→ 页面文字智能扫描（按按钮/链接/手型排序）
    真实门户的「编辑」「提交」都在页面下方，需要滚动才看得见，这里统一 scrollIntoView。
    """
    deadline = time.time() + timeout / 1000.0
    while True:
        # ① CSS 选择器（主页面 + 所有 iframe / 嵌套 iframe）
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    if label:
                        print(f"  [i] {label}: 命中选择器 {sel}")
                        log_audit(f"{label}选择器: {sel}")
                    return frame, loc
                except Exception:
                    continue
        # ② 页面文字智能扫描（按按钮/链接/手型排序，含 Shadow DOM）
        for frame in page.frames:
            try:
                info = await frame.evaluate(_SCAN_CLICKABLE_JS, [list(keywords), 14])
            except Exception:
                continue
            if info and info.get("found"):
                loc = frame.locator("[data-bot-pick='1']").first
                try:
                    await loc.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                print(f"  [i] {label}: 按文字命中 {info['tag']}「{info['text']}」"
                      f"（候选 {info['count']} 个: {', '.join(info.get('top') or [])}）")
                log_audit(f"{label}按文字命中: {info['tag']}「{info['text']}」")
                return frame, loc
        # ③ Playwright 定位兜底：跨域 iframe / JS 被拦 / 深层嵌套窗口时仍能找到
        #    门户的底部操作条常在 iframe 里，且文字是「提交(0)」这种，用包含匹配
        for frame in page.frames:
            for kw in keywords:
                for sel in (f"button:has-text('{kw}')", f"input[value*='{kw}']",
                            f"a:has-text('{kw}')", f"[role='button']:has-text('{kw}')",
                            f"[class*='btn']:has-text('{kw}')",
                            f"span:has-text('{kw}')", f"div:has-text('{kw}')"):
                    try:
                        loc = frame.locator(sel).first
                        if await loc.count() == 0 or not await loc.is_visible():
                            continue
                        await loc.scroll_into_view_if_needed(timeout=3000)
                        print(f"  [i] {label}: 兜底定位命中 {sel[:60]}（frame: {(frame.url or '')[:60]}）")
                        log_audit(f"{label}兜底定位: {sel[:60]}")
                        return frame, loc
                    except Exception:
                        continue
        if time.time() >= deadline:
            raise RuntimeError(
                f"未找到「{label}」：选择器 {list(selectors)} 与文字 {list(keywords)} 均未命中，"
                f"请用『📸 采集生产页面』补采该页结构")
        await asyncio.sleep(0.5)


def _market_sel(key: str) -> str:
    """运行时动态选 selector 集合（mock / real）"""
    return get_market_selectors()[key]


# 报价/移交「结果提示」里只认明确的报错词，避免把页面静态选项（如「转核价岗」）误判为失败
_RESULT_ERROR_KEYWORDS = (
    "权限不够", "权限不足", "报上级", "上报上级",
    "无权限", "超出权限", "需上级审核",
)


async def _read_tip_text(page: Page, key: str = "quote_result_tip") -> str:
    """读取结果提示条（toast / el-message）里当前可见的文字，读不到就返回空串"""
    try:
        loc = page.locator(SELECTORS_PORTAL[key])
        count = await loc.count()
    except Exception:
        return ""
    texts: list[str] = []
    for i in range(min(count, 5)):
        try:
            txt = (await loc.nth(i).inner_text(timeout=1000) or "").strip()
        except Exception:
            txt = ""
        if txt:
            texts.append(txt)
    return "\n".join(texts)


async def _page_text(page: Page) -> str:
    """整页文本（异常诊断用）"""
    try:
        return await page.locator("body").inner_text(timeout=2000)
    except Exception:
        try:
            return await page.content()
        except Exception:
            return ""


async def _dialog_text(page: Page) -> str:
    """读取当前可见弹窗文本（通用：Element UI / Bootstrap modal / 自定义遮罩），无则空串"""
    # 先试已知框架
    specific_selectors = [
        ".el-message-box__wrapper:visible", ".el-message-box:visible", ".el-message:visible",
        ".modal.in:visible", ".modal.show:visible", ".modal.fade.in:visible",
        ".modal:visible", ".modal-dialog:visible",
        ".ant-modal:visible", ".ant-message:visible",
        ".layui-layer:visible", ".ui-dialog:visible",
        "[role='dialog']:visible", "[role='alertdialog']:visible",
        ".dialog:visible", ".popup:visible",
    ]
    for fr in page.frames:
        for sel in specific_selectors:
            try:
                box = fr.locator(sel).last
                if await box.count() and await box.is_visible():
                    txt = (await box.inner_text() or "").strip()
                    if txt:
                        return txt[:500]
            except Exception:
                continue
    # 兜底：z-index 最高遮罩
    js = """() => {
      let best = null, bestZ = 0;
      for (const el of document.querySelectorAll('div, [role="dialog"]')) {
        const s = getComputedStyle(el);
        const isOverlay = (s.position === 'fixed' || s.position === 'absolute')
          && parseInt(s.zIndex || '0') >= 100 && s.display !== 'none' && s.visibility !== 'hidden';
        if (!isOverlay) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 100 || r.height < 60) continue;
        if (parseInt(s.zIndex || '0') > bestZ) { bestZ = parseInt(s.zIndex || '0'); best = el; }
      }
      return best ? (best.innerText || '').trim().slice(0, 500) : '';
    }"""
    for fr in page.frames:
        try:
            txt = await fr.evaluate(js)
            if txt:
                return txt
        except Exception:
            continue
    return ""


# 向后兼容别名
_el_messagebox_text = _dialog_text


async def _wait_page_result(
    page: Page,
    success_keywords: list[str],
    error_keywords: tuple = _RESULT_ERROR_KEYWORDS,
    tip_key: str = "quote_result_tip",
    timeout: int = DEFAULT_TIMEOUT,
    strict: bool = True,
    label: str = "提交",
) -> str:
    """
    等「提交成功 / 权限不足」提示：
      - 命中权限类关键词 → 抛 RuntimeError（带原文，供 main 判断是否需要补救）
      - 命中成功关键词 → 返回命中的词
      - 超时：strict=True 抛异常（不静默成功）；strict=False 只告警并返回 ""
    同时监听 alert/confirm 弹窗（真实内网可能用原生弹窗提示），
    以及 Element UI el-message-box（命中关键词时自动点「确定」关掉）
    """
    dialogs: list[str] = []

    def _on_dialog(d):
        try:
            dialogs.append(d.message or "")
        except Exception:
            pass
        _accept_dialog(d)

    page.on("dialog", _on_dialog)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout / 1000.0
    tip_text = ""
    try:
        while True:
            tip_text = await _read_tip_text(page, tip_key)
            box_text = await _el_messagebox_text(page)
            combined = tip_text + "\n" + "\n".join(dialogs) + "\n" + box_text
            for kw in error_keywords:
                if kw in combined:
                    if kw in box_text:
                        await confirm_el_messagebox(page, timeout=0.8)
                    raise RuntimeError(
                        f"{label}失败：页面提示「{kw}」 | 原文={tip_text or box_text or dialogs[-1]}"
                    )
            for kw in success_keywords:
                if kw in combined:
                    if kw in box_text:
                        await confirm_el_messagebox(page, timeout=0.8)
                        print(f"  [✓] 已点击「{kw}」提示框的「确定」")
                    log_audit(f"{label}成功提示: {kw}")
                    return kw
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.3)

        if strict:
            raise RuntimeError(
                f"{label}：等待成功提示超时（未出现 {success_keywords}），"
                f"页面提示={tip_text!r}；选择器待真机确认"
            )
        print(f"  [!] {label}：未检测到明确成功提示，按已提交处理（选择器待真机确认）")
        return ""
    finally:
        try:
            page.remove_listener("dialog", _on_dialog)
        except Exception:
            pass


async def _choose_dropdown_text(
    page: Page,
    input_locator: Locator,
    full_text: str,
    option_selector: str,
    label: str,
    exclude_text: str = "",
    fallback_query: str | None = None,
    fallback_match: str | None = None,
) -> None:
    """在「输入即过滤」的下拉组件里选中一项

    优先策略：直接填完整值（如 1100000000005175-市公司报价组）→ 点精确匹配项 / 回车确认。
    兜底策略：改填短代码（如 5175）→ 在下拉里点含 fallback_match 的项，并排除 exclude_text（如「废弃」）。
    两步都失败则抛异常，不静默通过。
    """
    async def _options() -> list[Locator]:
        try:
            await page.wait_for_timeout(800)
        except Exception:
            pass
        opts = page.locator(option_selector)
        n = await opts.count()
        return [opts.nth(i) for i in range(n)]

    async def _pick(match: str, exclude: str) -> bool:
        for opt in await _options():
            try:
                if not await opt.is_visible():
                    continue
                t = (await opt.inner_text()).strip()
            except Exception:
                continue
            if match and match not in t:
                continue
            if exclude and exclude in t:
                continue
            try:
                await opt.click()
                log_audit(f"{label}下拉 → {t}")
                print(f"  [✓] {label}下拉 → {t}")
                return True
            except Exception:
                continue
        return False

    async def _value() -> str:
        try:
            return (await input_locator.input_value()).strip()
        except Exception:
            return ""

    # 1) 填完整值 → 精确匹配项 / 回车生效
    await safe_click(input_locator, f"{label}输入框")
    try:
        await input_locator.fill(full_text)
    except Exception:
        await safe_fill(input_locator, full_text, f"{label} → {full_text}")
    match_key = full_text if exclude_text else full_text.split("-")[-1]
    picked = await _pick(match_key, exclude_text)
    if not picked:
        try:
            await input_locator.press("Enter")
            await page.wait_for_timeout(600)
        except Exception:
            pass
        val = await _value()
        picked = bool(val) and (match_key in val or full_text in val)
        if picked:
            print(f"  [i] {label} 回车生效: {val}")

    # 2) 兜底：短代码 + 匹配项
    if not picked and fallback_query:
        try:
            await safe_fill(input_locator, fallback_query, f"{label} → {fallback_query}")
            picked = await _pick(fallback_match or fallback_query, exclude_text)
        except Exception:
            picked = False

    if not picked:
        raise RuntimeError(
            f"{label}：未在下拉中选中『{full_text}』（选择器待真机确认：{option_selector}）"
        )

    # 3) 校验输入框已带上值
    val = await _value()
    if not val:
        raise RuntimeError(
            f"{label}：下拉已点选但输入框仍为空（选择器待真机确认：{option_selector}）"
        )
    print(f"  [i] {label} 已选: {val}")
    log_audit(f"{label} 已选: {val}")


def _quote_group_selected(value: str, group_key: str) -> bool:
    """组别是否已“选中”成完整选项

    生产实测：el-select 里只敲短号『5175』时 input 是有值的，
    但并没有选中选项，提交时平台会弹『组或人员请至少录入一项！』。
    因此只有值里带组名关键字（如『市公司报价组』）才算选中。
    """
    v = (value or "").strip()
    if not v:
        return False
    return (not group_key) or (group_key in v)


async def _quote_visible_input(scope, placeholder: str):
    """取 scope 内**可见**的 placeholder 输入框（没有可见的就退回最后一个）

    弹窗重渲染会留下隐藏的旧节点，`.last` 可能取到隐藏节点：
    读到旧值会让『已自动带出』判断误成立 → 跳过填写 → 平台弹『组或人员请至少录入一项』。
    """
    loc = scope.locator(f"input[placeholder='{placeholder}']")
    n = await loc.count()
    found = None
    for i in range(n):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                found = el
        except Exception:
            continue
    if found is not None:
        return found
    return loc.last if n > 0 else None


async def _quote_dismiss_group_required_popup(page: Page, timeout: float = 3.0) -> bool:
    """处理『组或人员请至少录入一项！』提示框：点『确定』关闭，返回是否出现过

    这个提示只是校验失败，点确定即可取消，不会提交任何东西；
    不清掉它会一直盖在页面上，后续等待提交结果/平级移交都会被挡住。

    生产实测（2026-11-18）弹框 DOM：
      <div class="el-message-box__wrapper" role="dialog">
        <div class="el-message-box">
          <div class="el-message-box__message"><p>组或人员请至少录入一项！</p></div>
          <div class="el-message-box__btns">
            <button class="el-button ... el-button--primary"><span>确定</span></button>
          </div>
    弹框是异步弹出的，所以这里轮询 timeout 秒，而不是只扫一次。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for fr in page.frames:
            try:
                box = fr.locator(".el-message-box:visible").last
                if await box.count() == 0 or not await box.is_visible():
                    continue
                txt = (await box.inner_text() or "").strip()
                if "请至少录入一项" not in txt:
                    continue
                print(f"  [!] 平台提示『{' '.join(txt.split())[:60]}』→ 点『确定』关闭")
                log_audit("提交上级：出现『组或人员请至少录入一项』提示 → 点确定关闭")
                for bt in ("确定", "确 定"):
                    btn = box.locator(
                        f"button:has-text('{bt}'), .el-button:has-text('{bt}')").first
                    if await btn.count() == 0 or not await btn.is_visible():
                        continue
                    try:
                        await btn.click(timeout=3000)
                    except Exception:
                        await btn.evaluate("el => el.click()")
                    break
                try:
                    await box.wait_for(state="hidden", timeout=3000)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _quote_clear_blocking_overlay(page: Page, label: str = "报价任务提交") -> int:
    """清掉盖在『报价任务提交』弹窗上的 message box

    生产实测（2026-11-18 RDAA2026130700N0068070 / RDAA2026130800N0039714）：
    选完提交路径『九级报价』后，上层还压着一个 el-message-box__wrapper，
    Playwright 点组别代码输入框时 15 秒全部被它拦截 → 卡死。

    处理规则（与保险金额提示框一致）：
      · 报价总额超过保险金额…是否重新报价 → 点『取消』
      · 请提交远程操作平台处理 → 中止（应走分中心报价分支，不能按九级报价提交）
      · 其它未知提示 → 记录文案后点『确定/确认/知道了』，仅用于解除遮挡
    """
    handled = 0
    for _ in range(3):
        box = None
        for fr in page.frames:
            try:
                b = fr.locator(
                    ".el-message-box__wrapper:visible, .el-message-box:visible").last
                if await b.count() > 0 and await b.is_visible():
                    box = b
                    break
            except Exception:
                continue
        if box is None:
            break

        try:
            txt = (await box.inner_text() or "").strip()
        except Exception:
            txt = ""
        flat = "".join(txt.split())
        print(f"  [!] {label}：发现遮挡弹窗 → {txt[:120] or '（无文本）'}")
        log_audit(f"{label}：发现遮挡弹窗『{txt[:300]}』")

        if "请提交远程操作平台处理" in flat:
            raise RuntimeError(
                f"{label}：弹出『请提交远程操作平台处理』→ 应改走分中心报价分支，"
                "不能按九级报价继续提交"
            )
        if "报价总额超过保险金额" in flat and "是否重新报价" in flat:
            buttons = ("取消",)
        else:
            buttons = ("确定", "确 定", "确认", "确 认", "提交下一节点",
                       "知道了", "已阅知", "关闭")

        clicked = False
        for bt in buttons:
            try:
                btn = box.locator(
                    f"button:has-text('{bt}'), .el-button:has-text('{bt}'), "
                    f"[role='button']:has-text('{bt}'), a:has-text('{bt}')").first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                try:
                    await btn.click(timeout=3000)
                except Exception:
                    # 弹窗本身也被浮层挡住时，用 JS 直接触发点击（绕过指针拦截）
                    await btn.evaluate("el => el.click()")
                clicked = True
                print(f"  [✓] {label}：遮挡弹窗 → 点『{bt}』")
                log_audit(f"{label}：遮挡弹窗点『{bt}』")
                break
            except Exception:
                continue
        if not clicked:
            try:
                x = box.locator(
                    ".el-message-box__headerbtn, .el-message-box__close").first
                if await x.count() > 0:
                    await x.click(timeout=2000)
                    clicked = True
                    print(f"  [✓] {label}：遮挡弹窗 → 点右上角关闭")
            except Exception:
                pass

        handled += 1
        try:
            await box.wait_for(state="hidden", timeout=3000)
        except Exception:
            await asyncio.sleep(0.5)
        if not clicked:
            break
    return handled


async def _quote_fill_group_remote(
    page: Page,
    group_input: Locator,
    short_query: str,
    match_keyword: str,
    full_value: str,
    label: str = "组别代码",
    timeout: float = 8.0,
    dialog: Locator | None = None,
) -> None:
    """填写「远程搜索」类型的 el-select（提交上级弹窗里的组别代码就是这种）

    生产实测 DOM 结构（从 F12 保存确认）：
      <div class="el-select">
        <div class="el-input el-input--suffix">
          <input type="text" placeholder="组号或组名" class="el-input__inner">
        </div>
        <!-- 没有 el-select-dropdown 子元素！下拉被 teleport 到 body 根节点 -->
      </div>
      <!-- body 根节点 -->
      <div class="el-select-dropdown el-popper" style="z-index: 2019;">
        <ul class="el-select-dropdown__list">
          <li class="el-select-dropdown__item hover"> 1100000000005175-市公司报价组 </li>
        </ul>
      </div>

    交互流程（用户实测必须按这个来）：
      1. 直接点 input → 聚焦（不会展开下拉）
      2. 清空 input → type 短号关键字（如 "5175"）→ 触发后端远程搜索
      3. 等 body 根节点下出现 visible 的 .el-select-dropdown.el-popper
      4. 点选项里含 match_keyword 的 li（如 "市公司报价组"）
      5. 校验 input 带上完整值

    不能用 fill(full_value) 的原因：后端可能只按短号/名称前缀匹配，完整值搜不到选项。
    """
    # Step 0: 先清掉盖住弹窗的 message box
    #（生产实测：选完『九级报价』后上层压着 el-message-box，点击被拦截 15s 直至超时）
    await _quote_clear_blocking_overlay(page, label)
    # 平台可能在选完提交路径后就异步弹『组或人员请至少录入一项』，
    # 它会盖住组别输入框；这里先廉价探测一次，避免下面的点击白等
    await _quote_dismiss_group_required_popup(page, timeout=0.1)

    # Step 0.1: 用稳定 placeholder 重新定位输入框
    #（弹窗重渲染后 data-bot-field 标记会丢，导致 locator 解析不到）
    try:
        scope = dialog if dialog is not None else page
        alt = await _quote_visible_input(scope, "组号或组名")
        if alt is not None and await alt.count() > 0:
            group_input = alt
    except Exception:
        pass

    # Step 0.2: 关掉可能残留的其它下拉（提交路径选完后弹层可能还开着，会挡住点击）
    # 用 document mousedown 触发 Element UI 的 click-outside，避免按 Esc 把整个弹窗关掉
    try:
        if await page.locator(".el-select-dropdown.el-popper:visible").count() > 0:
            await page.evaluate(
                "() => document.body.dispatchEvent("
                "new MouseEvent('mousedown', {bubbles: true}))")
            await asyncio.sleep(0.3)
    except Exception:
        pass

    # Step 1: 点击 input 聚焦
    # 点击超时压到 1.5s：提示框若在点击瞬间弹出，Playwright 会一直等可点击性，
    # 旧版 5s 才失败 → 看起来像“程序不处理提示框”。这里失败后立即清遮挡再试。
    try:
        await group_input.click(timeout=1500)
    except Exception as exc:
        await _quote_dismiss_group_required_popup(page, timeout=0.1)
        await _quote_clear_blocking_overlay(page, label)
        try:
            await group_input.click(timeout=1500)
        except Exception:
            try:
                await group_input.evaluate(
                    "el => { el.removeAttribute('readonly'); el.focus(); }")
            except Exception:
                print(f"  [!] {label} 点击聚焦失败，改用 JS 输入: {exc}")

    # 只读态（已选值/组件锁定）会吞掉键盘输入 → 先解除
    try:
        if await group_input.get_attribute("readonly") is not None:
            await group_input.evaluate("el => el.removeAttribute('readonly')")
    except Exception:
        pass

    # Step 2: 清空 input + 输入短号关键字（触发远程搜索）
    try:
        await group_input.fill("")
        await asyncio.sleep(0.2)
    except Exception:
        try:
            await group_input.evaluate(
                "el => { el.value='';"
                " el.dispatchEvent(new Event('input',{bubbles:true})); }")
        except Exception:
            pass
    # 用 type 逐字输入（比 fill 更像真实用户，remote-method 的防抖通常用 input 事件）
    try:
        await group_input.type(short_query, delay=30)
    except Exception:
        try:
            await group_input.fill(short_query)
        except Exception:
            try:
                await group_input.evaluate(
                    "(el, q) => { el.value=q;"
                    " el.dispatchEvent(new Event('input',{bubbles:true})); }",
                    short_query)
            except Exception as exc:
                raise RuntimeError(
                    f"{label} 无法输入关键字『{short_query}』: {exc}")
    print(f"  [i] {label} 输入关键字『{short_query}』，等待后端返回选项...")

    # Step 3+4: 轮询所有可见下拉，直到出现含 match_keyword 的选项
    # 不能用「第一个可见下拉」：提交路径下拉可能残留，会读到它的 8 个选项，
    # 旧实现据此兜底点第一项，既点不中还会误改提交路径，并空转近 4 分钟。
    picked_text = ""
    seen: list[str] = []
    retyped = False
    wait_deadline = time.monotonic() + max(timeout, 12.0)
    while time.monotonic() < wait_deadline and not picked_text:
        # 提示框可能在等待选项期间弹出并盖住下拉 → 顺手关掉（点确定只是取消校验）
        await _quote_dismiss_group_required_popup(page, timeout=0.01)
        seen = []
        try:
            drops = page.locator(".el-select-dropdown.el-popper:visible")
            for di in range(await drops.count()):
                items = drops.nth(di).locator(".el-select-dropdown__item")
                for ii in range(await items.count()):
                    it = items.nth(ii)
                    try:
                        if not await it.is_visible():
                            continue
                        text = (await it.inner_text()).strip()
                    except Exception:
                        continue
                    if not text:
                        continue
                    if text not in seen:
                        seen.append(text)
                    if match_keyword and match_keyword in text:
                        try:
                            await it.click(timeout=2500)
                        except Exception:
                            # 选项可能被其它浮层盖住 → JS 触发，确保真正选中
                            try:
                                await it.evaluate("el => el.click()")
                            except Exception:
                                break
                        picked_text = text
                        break
                if picked_text:
                    break
        except Exception:
            pass
        if picked_text:
            break

        # 只重新输入一次，避免反复清空页面状态
        if not retyped and time.monotonic() > wait_deadline - 8.0:
            retyped = True
            try:
                await group_input.click(timeout=3000)
                await group_input.press("Control+a")
                await group_input.press("Backspace")
                await group_input.type(short_query, delay=30)
                print(f"  [i] {label} 暂未见到目标选项，重新输入『{short_query}』再试...")
            except Exception:
                pass
        await asyncio.sleep(0.4)

    if picked_text:
        print(f"  [✓] {label} 下拉选中: {picked_text}")
        log_audit(f"{label}下拉选中(远程搜索): {picked_text}")
    else:
        # 兜底：只点「文本含短号」的选项，绝不点其它下拉（否则会改掉提交路径）
        try:
            items = page.locator(
                ".el-select-dropdown.el-popper:visible .el-select-dropdown__item")
            for i in range(await items.count()):
                it = items.nth(i)
                try:
                    text = (await it.inner_text()).strip()
                    if short_query and short_query in text:
                        try:
                            await it.click(timeout=2500)
                        except Exception:
                            await it.evaluate("el => el.click()")
                        picked_text = text
                        print(f"  [✓] {label} 兜底选中含『{short_query}』的选项: {text}")
                        log_audit(f"{label}下拉选中(兜底): {text}")
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if not picked_text:
        print(f"  [!] {label} 当前可见下拉选项({len(seen)}): {seen[:12]}")
        log_audit(f"{label} 未匹配到选项，可见下拉({len(seen)}): {seen[:12]}")
        raise RuntimeError(
            f"{label}：未找到含『{match_keyword}』的选项"
            f"（可见 {len(seen)} 项：{seen[:8]}）→ 无法自动填写"
        )

    # Step 5: 等待 input 带上完整值，校验
    await asyncio.sleep(0.5)
    val = ""
    for _ in range(5):
        try:
            val = (await group_input.input_value() or "").strip()
        except Exception:
            val = ""
        if val:
            break
        await asyncio.sleep(0.2)

    if not val:
        raise RuntimeError(
            f"{label}：下拉已点选但 input 仍为空 → 选择器可能不匹配"
        )
    print(f"  [✓] {label} 已带上值: {val}")
    log_audit(f"{label}已填写: {val}")

    # 平台可能在操作期间异步弹『组或人员请至少录入一项』（延迟 0.5~1s），
    # 留着会挡住后面『提交任务』的点击 → 收尾时再清一次
    await _quote_dismiss_group_required_popup(page, timeout=0.4)

    # 额外校验：如果 full_value 提供了，检查是不是匹配
    if full_value and full_value not in val and match_keyword not in val:
        print(f"  [!] {label} 值校验：期望含『{full_value}』或『{match_keyword}』"
              f"，实际『{val}』— 仍继续，但请人工留意")


async def _quote_read_group_value(group_input) -> str:
    """读取组别代码输入框当前值（兼容 input_value / Element UI 已选项文本）"""
    try:
        if group_input is not None and await group_input.count() > 0:
            val = (await group_input.input_value() or "").strip()
            if val:
                return val
    except Exception:
        pass
    try:
        if group_input is not None and await group_input.count() > 0:
            val = await group_input.evaluate(
                "el => (el.value || (el.closest('.el-select')"
                " && el.closest('.el-select').querySelector("
                "'.el-select__selected-item, .el-select__placeholder')"
                " && el.closest('.el-select').querySelector("
                "'.el-select__selected-item, .el-select__placeholder')"
                ".innerText) || '')")
            return (val or "").strip()
    except Exception:
        pass
    return ""


def _quote_group_ok(val: str, group_key: str) -> bool:
    """组别是否真的选中（不只是输入了短号）"""
    try:
        return bool(_quote_group_selected(val, group_key))
    except Exception:
        return bool(val and group_key and group_key in val)


async def _quote_locate_group_input(page: Page, dialog):
    """用稳定 placeholder 在当前弹窗里重新定位组别输入框"""
    try:
        scope = dialog if dialog is not None else page
        inp = await _quote_visible_input(scope, "组号或组名")
        if inp is not None and await inp.count() > 0:
            return inp
    except Exception:
        pass
    return None


async def _quote_wait_group_stable(page: Page, dialog, group_key: str,
                                   timeout: float = 2.5) -> str:
    """轮询组别输入框：连续 3 次读到“已选中且值不变”才返回最后的值"""
    deadline = time.monotonic() + max(0.5, timeout)
    last = ""
    stable = 0
    while time.monotonic() < deadline:
        inp = await _quote_locate_group_input(page, dialog)
        val = await _quote_read_group_value(inp) if inp is not None else ""
        if _quote_group_ok(val, group_key) and val == last:
            stable += 1
            if stable >= 3:
                return val
        else:
            stable = 0
        last = val
        await asyncio.sleep(0.25)
    return last


async def _quote_ensure_group_selected(page: Page, dialog, group_input,
                                       group_code: str, group_key: str,
                                       group_option: str):
    """确保组别已真正选中；返回 (最新输入框, 当前值)

    选提交路径后弹窗会异步重渲染/回填，可能在填完后把组别清空。
    这里先等自动带出稳定，未带出则远程填写，并做二次重填兜底。
    """
    # 先等系统自动带出（同案件走过时可能自动填好）
    auto_val = await _quote_wait_group_stable(page, dialog, group_key, timeout=4.0)
    if _quote_group_ok(auto_val, group_key):
        inp = await _quote_locate_group_input(page, dialog) or group_input
        return inp, auto_val

    inp, val = group_input, ""
    for attempt in (1, 2):
        inp = await _quote_locate_group_input(page, dialog) or group_input
        await _quote_fill_group_remote(
            page, inp,
            short_query=group_code,
            match_keyword=group_key or group_option,
            full_value=group_option,
            label="组别代码",
            dialog=dialog,
        )
        val = await _quote_wait_group_stable(page, dialog, group_key, timeout=2.5)
        if _quote_group_ok(val, group_key):
            return inp, val
        print(f"  [!] 组别填写后仍为空/未选中（第 {attempt} 次，当前『{val or '空'}』）"
              f"→ 重新填写")
        log_audit(f"组别填写后未稳定（第 {attempt} 次，当前『{val or '空'}』）→ 重填")
    return inp, val


# ============================================================
from bot.workflow.registry import register_step

# 报案号正则：RD + 2位字母 + 16~20位字母数字
# （mock: RDZA2026110000501913=RDZA+16 / 生产: RDAA2026110000N0519652=RDAA+18）
CLAIM_NO_RE = re.compile(r"\bRD[A-Za-z]{2}[0-9A-Z]{16,20}\b", re.IGNORECASE)


async def _loss_page_claim_state(p: Page, claim_no: str):
    """定损页是否属于本单：True=页内出现本单报案号，False=只出现别的报案号，None=读不到/未渲染"""
    if not claim_no:
        return None
    nums: set = set()
    try:
        for fr in p.frames:
            try:
                txt = await fr.evaluate(
                    "() => (document.body ? document.body.innerText : '')")
            except Exception:
                continue
            nums.update(m.group(0).upper() for m in CLAIM_NO_RE.finditer(txt or ""))
    except Exception:
        return None
    if not nums:
        return None
    return True if claim_no.upper() in nums else False


async def find_claim_rows(page: Page) -> list[str]:
    """从页面文本里提取所有报案号（不依赖具体 DOM，mock/真实门户通用）

    真实门户常把案件列表放在 iframe / frameset 的某个 frame 里，所以逐 frame 扫描。
    """
    seen: list[str] = []
    for frame in page.frames:
        try:
            text = await frame.inner_text("body")
        except Exception:
            continue
        for m in CLAIM_NO_RE.findall(text or ""):
            if m not in seen:
                seen.append(m)
    return seen


# JS 行级扫描：只在「表格行」里找报案号，并判断该行是否可点击
# —— 解决『页面文本兜底把历史/详情区的纯文本报案号也当成待处理案件』的问题
_JS_SCAN_CLAIM_ROWS = r"""
() => {
  const re = /RD[A-Za-z]{2}[0-9A-Z]{16,20}/i;
  const out = [];
  const seen = new Set();
  const vis = (el) => {
    try {
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
      const r = el.getBoundingClientRect();
      return r.width > 2 && r.height > 2;
    } catch (e) { return false; }
  };
  const isClickable = (el) => {
    let a = el;
    for (let k = 0; k < 6 && a; k++) {
      try {
        if (a.tagName === 'A' || a.hasAttribute('onclick')) return true;
        if (getComputedStyle(a).cursor === 'pointer') return true;
      } catch (e) {}
      a = a.parentElement;
    }
    return false;
  };
  Array.from(document.querySelectorAll('tbody tr, .el-table__row, ul li, .list-item')).forEach(tr => {
    const txt = (tr.innerText || '');
    const m = txt.match(re);
    if (!m) return;
    const claim = m[0].toUpperCase();
    if (seen.has(claim)) return;
    if (!vis(tr)) return;
    let target = null;
    // 优先: 元素 class 包含 claim-no / claimNo / claim_code（生产通用命名）
    const claimByCls = tr.querySelector('[class*="claim-no"],[class*="claimNo"],[class*="claim_code"],[class*="claimNo"]');
    if (claimByCls && vis(claimByCls)) { target = claimByCls; }
    // 其次: textContent 严格等于 claim
    if (!target) {
      for (const el of Array.from(tr.querySelectorAll('a, span, div, td, p'))) {
        if ((el.textContent || '').trim() === m[0]) { target = el; break; }
      }
    }
    // 再其次: 以 claim 开头，尾部只有短标签（允许 "RDAA...18420 省内"）
    if (!target) {
      for (const el of Array.from(tr.querySelectorAll('a, span, div, td, p'))) {
        const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (t && t.toUpperCase().startsWith(claim)) {
          const rest = t.substring(claim.length).trim();
          const restTags = rest.split(/\s+/).filter(Boolean);
          const allShort = restTags.every(x => x.length <= 6 && !/\d{4,}/.test(x));
          if (rest === '' || (restTags.length <= 2 && allShort)) { target = el; break; }
        }
      }
    }
    const clickable = target ? isClickable(target) : isClickable(tr);
    const hasLink = !!tr.querySelector("a[href], button, [onclick], [class*='link'], [class*='btn']");
    seen.add(claim);
    out.push({claim, clickable: clickable || hasLink, tag: target ? target.tagName : 'TR',
              sample: txt.replace(/\s+/g, ' ').trim().slice(0, 70)});
  });
  return out;
}
"""


async def _scan_claim_rows(page: Page) -> list[dict]:
    """逐 frame 扫描表格行里的报案号，返回 [{claim, clickable, tag, sample}]"""
    rows: list[dict] = []
    seen: set[str] = set()
    for fr in page.frames:
        try:
            data = await fr.evaluate(_JS_SCAN_CLAIM_ROWS)
        except Exception:
            continue
        for item in data or []:
            claim = (item.get("claim") or "").upper()
            if not claim or claim in seen:
                continue
            seen.add(claim)
            rows.append(item)
    return rows


# JS 精确扫描：全页找 textContent 包含报案号的可点击元素（允许尾部跟「省内/省间/集/超60天」等短标记）
# 评分: cursor:pointer +25, 表格里 +15, 蓝色/深色 +20, 宽度合适 +5；is-hidden 副本 -60, 查询面板 -40
_JS_FIND_CLAIM_EXACT = r"""
(claim) => {
  document.querySelectorAll('[data-bot-claim-pick]').forEach(e => e.removeAttribute('data-bot-claim-pick'));
  const claimStr = String(claim).trim();
  // 匹配：文本包含 claim，且 claim 前后不是字母数字（防止嵌套 claim 号误伤）
  const re = new RegExp('(?:^|\\W)' + claimStr.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?:$|\\W)', 'i');
  // 短标签白名单（出现在 claim 号之后允许通过，防止 textContent 严格等于失败）
  const SHORT_TAG = /^(省内|省间|集|超60天|超30天|超90天|直供|加急|本地|异地|跨区|跨省|跨市|跨县|省内通赔|通赔)$/;
  const TAGS = ['span','div','a','td','li','th','p','label'];
  const SKIP_TAGS = new Set(['INPUT','TEXTAREA','SELECT','BUTTON','OPTION','SCRIPT','STYLE','NOSCRIPT']);
  const cands = [];
  for (const tag of TAGS) {
    for (const el of document.querySelectorAll(tag)) {
      if (SKIP_TAGS.has(el.tagName)) continue;
      const t = String(el.textContent || '').replace(/\s+/g, ' ').trim();
      if (!t) continue;
      // === 文本匹配 ===
      let match = false;
      let matchMode = 'none';
      // 模式 1: 严格等于
      if (t === claimStr) { match = true; matchMode = 'exact'; }
      // 模式 2: 以 claim 开头，后面只跟短标签（如 "RDAA...18420 省内"）
      if (!match) {
        let rest = t;
        const i = rest.indexOf(claimStr);
        if (i >= 0) {
          const before = rest.substring(0, i).trim();
          const after = rest.substring(i + claimStr.length).trim();
          if (!before || !re.test(before.slice(-1))) {
            const afterTags = after.split(/\s+/).filter(Boolean);
            const allShort = afterTags.length > 0 && afterTags.every(x => x.length <= 6 && !/\d{4,}/.test(x));
            const mostlyClaim = afterTags.length <= 2 && allShort;
            if (i === 0 && (after === '' || mostlyClaim)) { match = true; matchMode = 'prefix-tags'; }
            else if (i > 0 && before === '' && (after === '' || mostlyClaim)) { match = true; matchMode = 'contains'; }
          }
        }
      }
      if (!match) continue;

      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;

      let inQueryPanel = false;
      let ancestor = el;
      for (let k = 0; k < 8 && ancestor; k++) {
        const cls = (ancestor.className || '').toString().toLowerCase();
        if (cls.match(/(search|query|form|filter|input|panel|toolbar|header)/)) { inQueryPanel = true; break; }
        ancestor = ancestor.parentElement;
      }
      let inTable = false;
      let anc2 = el;
      for (let k = 0; k < 8 && anc2; k++) {
        if (anc2.tagName === 'TR' || anc2.tagName === 'TD' || anc2.tagName === 'TABLE') { inTable = true; break; }
        const cls = (anc2.className || '').toString().toLowerCase();
        if (cls.match(/(table|row|list|cell|tbody|thead|body)/)) { inTable = true; break; }
        anc2 = anc2.parentElement;
      }
      let score = 0;
      const cursorPtr = st.cursor === 'pointer';
      let hiddenCopy = false;
      let anc3 = el;
      for (let k = 0; k < 5 && anc3; k++) {
        const c3 = (anc3.className || '').toString();
        if (/is-hidden|el-table__fixed-hidden/.test(c3)) { hiddenCopy = true; break; }
        anc3 = anc3.parentElement;
      }
      if (hiddenCopy) score -= 60;
      if (cursorPtr) score += 25;
      if (matchMode === 'exact') score += 15;      // 精确匹配加奖励
      else score += 8;                              // 带标签的宽松匹配
      const col = (st.color || '').toLowerCase();
      if (col.includes('28') && col.includes('97') && col.includes('252')) score += 20;
      else if (col.match(/rgba?\(0,\s*0,\s*252/)) score += 20;
      else if (col.includes('#1c61fc') || col.includes('#0000ff')) score += 20;
      else if (st.color && !col.includes('rgb(0, 0, 0') && !col.includes('rgb(51') && !col.includes('rgb(102')) score += 8;
      if (inTable) score += 15;
      if (inQueryPanel) score -= 40;
      if (r.width < 40) score -= 15;
      if (r.width > 200) score += 5;
      if (el.tagName === 'A') score += 5;
      if (el.children.length === 0) score += 3;
      // 如果元素有 claim-no / claimNo / claim 相关 class → 额外加分（生产通常这么命名）
      const myCls = (el.className || '').toString().toLowerCase();
      if (/claim[-_]?(no|num|code)/.test(myCls)) score += 20;
      cands.push({el, score, tag: el.tagName, cls: (el.className || '').toString().substring(0,60),
                  cursor: st.cursor, color: st.color || '', inTable, inQueryPanel, cursorPtr,
                  width: Math.round(r.width), height: Math.round(r.height),
                  children: el.children.length, matchMode});
    }
  }
  if (!cands.length) return {found: false, count: 0};
  // pointer 候选整体排前
  const ptrCands = cands.filter(c => c.cursorPtr);
  const nonPtrCands = cands.filter(c => !c.cursorPtr);
  let ordered;
  if (ptrCands.length > 0) {
    ptrCands.sort((a,b) => b.score - a.score);
    nonPtrCands.sort((a,b) => b.score - a.score);
    ordered = ptrCands.concat(nonPtrCands);
  } else {
    cands.sort((a,b) => b.score - a.score);
    ordered = cands;
  }
  const top = ordered.map((c,i) =>
    `#${i+1} ${c.tag}(${c.cls||''},cur=${c.cursor},w=${c.width},tbl=${c.inTable?'Y':'N'},q=${c.inQueryPanel?'Y':'N'},ptr=${c.cursorPtr?'Y':'N'},mode=${c.matchMode},score=${c.score})`);
  const best = ordered[0];
  if (best.score < 30) {
    return {found: true, count: ordered.length, lowScore: true, top};
  }
  best.el.setAttribute('data-bot-claim-pick', '1');
  best.el.scrollIntoView({block: 'center', inline: 'center'});
  return {found: true, count: ordered.length, lowScore: false,
          score: best.score, tag: best.tag, cls: best.cls, cursor: best.cursor,
          color: best.color, inTable: best.inTable, inQueryPanel: best.inQueryPanel,
          width: best.width, top, matchMode: best.matchMode};
}
"""


async def find_claim_link(page: Page, claim_no: str):
    """按报案号找可点击元素（mock vs 生产交互不同，多策略兜底）

    顺序：
      ⓪ JS 全页精确扫描 + 评分排序（首选，不依赖 DOM 结构是 table 还是 div）
      ① 表格行内、可见的精确文本（mock 里点纯文本就跳转；生产 <td> 文本也带点击事件）
      ② 任意可见的精确文本（跳过查询框/隐藏元素里的同名文本）
      ③ 表格行内的 <a> / [onclick] / [role=button]
      ④ 整行 <tr>（带 onclick 的行）
      ⑤ 单元格 <td> 兜底

    为什么要跳过不可见元素：生产门户的查询条件区 / 隐藏模板里也有报案号文本，
    get_by_text(...).first 可能命中它们 → 点了个不响应的元素（真实 bug）。
    """
    # ===== ⓪ 新版：JS 精确扫描（不依赖 tr 结构，Vue 动态渲染也能命中） =====
    try:
        info = await page.evaluate(_JS_FIND_CLAIM_EXACT, claim_no)
        if info and info.get("found"):
            top = info.get("top") or []
            # 打印所有候选，生产环境误选时能立刻定位原因
            for t in top:
                print(f"    [cand] {t}")
            if info.get("lowScore"):
                print(f"  [i] JS精确扫描：最高分仅 {info.get('score', '<40')}（全是弱候选），跳过 → 用 Playwright 后续策略")
            else:
                details = f"{info.get('tag','')}({info.get('cls','')[:40]},cur={info.get('cursor','')},w={info.get('width','')},score={info.get('score','')}"
                extras = []
                if info.get("inTable"): extras.append("inTable")
                if info.get("inQueryPanel"): extras.append("inQueryPanel")
                if info.get("cursor") == "pointer": extras.append("cursor:pointer")
                if extras: details += " [" + ",".join(extras) + "]"
                print(f"  [i] 定位报案号: JS精确扫描 → {details}")
                loc = page.locator("[data-bot-claim-pick='1']").first
                if await loc.count() > 0:
                    return loc
    except Exception as exc:
        print(f"  [i] JS精确扫描异常：{exc}")
        pass

    tr_prefix = f"tr:has-text('{claim_no}')"

    # ① 行内精确文本（最贴合你手工点击的位置）；优先 cursor:pointer 的那个
    visible_els = []
    try:
        cells = page.locator(tr_prefix).get_by_text(claim_no, exact=True)
        for i in range(min(await cells.count(), 5)):
            el = cells.nth(i)
            try:
                if not await el.is_visible():
                    continue
                # 排除 Element UI 固定列/隐藏副本（点它不会有任何反应）
                hidden = await el.evaluate(
                    "e => {let a=e; for(let k=0;k<5&&a;k++){"
                    " const c=(a.className||'').toString();"
                    " if(/is-hidden|el-table__fixed-hidden/.test(c)) return true;"
                    " a=a.parentElement;} return false;}")
                if hidden:
                    continue
                visible_els.append(el)
            except Exception:
                continue
    except Exception:
        pass
    if visible_els:
        pick = visible_els[0]
        for el in visible_els:
            try:
                if await el.evaluate("e => getComputedStyle(e).cursor === 'pointer'"):
                    pick = el
                    break
            except Exception:
                pass
        print(f"  [i] 定位报案号: 行内精确文本（行内可见候选 {len(visible_els)} 个）")
        return pick

    # ② 任意可见的精确文本（跳过查询框/隐藏元素里的同名文本）
    try:
        cands = page.get_by_text(claim_no, exact=True)
        for i in range(min(await cands.count(), 8)):
            el = cands.nth(i)
            try:
                if await el.is_visible():
                    print(f"  [i] 定位报案号: 页面可见文本（第 {i+1} 个候选）")
                    return el
            except Exception:
                continue
    except Exception:
        pass

    # ③ 行内可点击元素（<a> / [onclick]）
    for sel_template in [
        f"{tr_prefix} a",
        f"{tr_prefix} [onclick]",
        f"{tr_prefix} button",
        f"{tr_prefix} [role='button']",
        f"{tr_prefix} [class*='link']",
    ]:
        try:
            loc = page.locator(sel_template).first
            if await loc.count() > 0 and await loc.is_visible():
                print(f"  [i] 定位报案号: 行内元素 {sel_template[:40]}")
                return loc
        except Exception:
            continue

    # ④ 整行 <tr> 点击
    try:
        row = page.locator(tr_prefix).first
        if await row.count() > 0 and await row.is_visible():
            print(f"  [i] 定位报案号: 整行 tr")
            return row
    except Exception:
        pass

    # ⑤ 单元格 <td> 兜底
    cell = page.locator(f"td:has-text('{claim_no}')").first
    if await cell.count() > 0:
        print(f"  [i] 定位报案号: 单元格 td")
        return cell

    print(f"  [i] 定位报案号: 纯文字兜底")
    return page.locator(f"text={claim_no}").first


_JS_MARK_CLAIM_ROW = """
(claim) => {
  document.querySelectorAll('[data-bot-row]').forEach(e => e.removeAttribute('data-bot-row'));
  // 报案号 span 里常嵌一个空标记 span（截图实测：<span class="zi lflagtext"></span>），
  // 所以不能要求 children.length === 0，按「文字==报案号」取最深的一个
  const leaves = [];
  for (const el of document.querySelectorAll('span,div,a,td')) {
    const t = String(el.textContent || '').trim();
    if (t !== claim) continue;
    leaves.push(el);
  }
  if (!leaves.length) return false;
  leaves.sort((a, b) =>
    a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  let row = null, cur = leaves[0];
  for (let i = 0; i < 6 && cur; i++) {
    const tag = cur.tagName;
    const cls = String(cur.className || '').toLowerCase();
    if (tag === 'TR' || tag === 'LI' || /(row|item|record|line)/.test(cls)) { row = cur; break; }
    cur = cur.parentElement;
  }
  if (!row) return false;
  row.setAttribute('data-bot-row', '1');
  return true;
}
"""


async def _claim_row_scope(page: Page, claim_no: str):
    """定位报案号所在的那一行（tr / li / row 容器）→ (frame, locator)；找不到返回 (None, None)

    生产列表可能是 <table>，也可能是 Vue 的 div 列表；按 tr → 祖先元素 → JS 标记 三级兜底。
    """
    for frame in page.frames:
        for sel in (f"tr:has-text('{claim_no}')",
                    f"xpath=//*[contains(text(),'{claim_no}')]/ancestor::*[self::tr or self::li][1]"):
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return frame, loc
            except Exception:
                continue
        try:
            if await frame.evaluate(_JS_MARK_CLAIM_ROW, claim_no):
                loc = frame.locator("[data-bot-row='1']").first
                if await loc.count() > 0:
                    return frame, loc
        except Exception:
            continue
    return None, None


async def find_claim_row_actions(page: Page, claim_no: str) -> list:
    """同一行里除报案号之外的其它入口（生产门户『流程图』列）

    实测 DOM（理赔作业门户首页）：
      <td ... class="... blueTableText ..."><div class="cell">
        <span style="color: rgb(28,97,252); cursor: pointer;"> 一级报价 </span>
      </div></td>
    「一级报价」是 **span**（不是 <a>），所以必须按文字找；
    操作列的图标点开的是『留言页面』（message/index），不是报价页，不再作为候选。
    """
    frame, row = await _claim_row_scope(page, claim_no)
    if row is None:
        return []
    out = []
    seen_text = set()
    # ① 文字入口（span / div / td 都覆盖，取最内层）
    for txt, label in [("一级报价", "流程图·一级报价"), ("报价", "流程图·报价")]:
        try:
            loc = row.get_by_text(txt).first
            if await loc.count() > 0 and await loc.is_visible():
                t = (await loc.inner_text() or "").strip()
                if t in seen_text:      # 「报价」是「一级报价」的子串，会命中同一个元素
                    continue
                seen_text.add(t)
                out.append((label, loc))
        except Exception:
            pass
    # ② 单元格兜底（文字被遮住/被 icon 包着时点整格）
    for sel, label in [("td:has-text('一级报价')", "一级报价单元格"),
                       ("td:has-text('报价')", "报价单元格"),
                       ("a", "行内链接")]:
        try:
            loc = row.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                t = (await loc.inner_text() or "").strip()
                if t in seen_text:
                    continue
                title = ""
                try:
                    title = (await loc.get_attribute("title") or "").strip()
                except Exception:
                    pass
                if "编辑" in title:      # 备注编辑，绝不能点
                    continue
                seen_text.add(t)
                out.append((label, loc))
        except Exception:
            continue
    return out

# STEP 0: 首页 → 列出所有可处理的报案号
# ============================================================
async def _dump_empty_list_diag(page: Page) -> None:
    """列表读到 0 时打印诊断：当前停在哪个页面、是否掉登录 / 撞 404"""
    try:
        url = page.url or ""
        title = await page.title()
        frames = []
        for f in page.frames:
            try:
                frames.append((f.url or "")[:100])
            except Exception:
                pass
        body = ""
        try:
            body = (await page.inner_text("body"))[:160].replace("\n", " ")
        except Exception:
            pass
        login_hint = any(k in body for k in ("登录", "用户名", "密码", "404", "无权"))
        print(f"  [i] 列表为空诊断: url={url[:120]}")
        print(f"      title={title[:60]} | frame数={len(frames)} | 登录/404提示={login_hint}")
        print(f"      body前160字: {body}")
        for f in page.frames:
            if f == page.main_frame:
                continue
            try:
                ftxt = (await f.inner_text("body"))[:70].replace("\n", " ")
            except Exception:
                ftxt = "<读取失败>"
            print(f"      frame: {(f.url or '')[:90]} | {ftxt}")
        log_audit(f"列表为空 @ {url[:120]} | frame={len(frames)} | 登录提示={login_hint}")
    except Exception:
        pass


@register_step(desc="首页列出所有可处理报案号", outputs=["pending_claims"])
async def step_home_list_claims(page: Page, context: dict, **kwargs) -> list:
    """
    扫描首页报案号列表，返回所有"待定损·报价"状态的报案号

    mock 实现：从 #claim-table tbody 里读 tr
    真实内网：selector 可能变化，这里用通用策略
    """
    print(f"\n🔹 STEP 0: 首页 → 扫描可处理报案号")
    await page.wait_for_load_state("networkidle")

    # 策略 1：找带 claim-no class + 旁边 status-tag 是待处理状态的
    claims = []
    try:
        rows = await page.locator("#claim-table tbody tr").all()
    except Exception:
        rows = []

    for row in rows:
        try:
            # 报案号
            no_el = row.locator(".claim-no").first
            if await no_el.count() == 0:
                continue
            claim_no = (await no_el.text_content() or "").strip()
            if not claim_no:
                continue

            # 状态：是否是待定损·报价 / 定损中 / 核损中（均可处理）
            status_el = row.locator(".status-tag").first
            status_text = ""
            status_class = ""
            if await status_el.count() > 0:
                status_text = (await status_el.text_content() or "").strip()
                status_class = await status_el.get_attribute("class") or ""

            # 跳过已完成的
            if "已完成" in status_text or "status-done" in status_class or "查看" in status_text:
                continue

            claims.append({
                "claim_no": claim_no,
                "status": status_text,
                "el": row,  # 保留 row 引用，后面可以 click
            })
        except Exception as exc:
            print(f"  [!] 读行失败: {exc}")

    # 策略 2：真实门户 → 只在表格行里找报案号；可点击的行排前面
    if not claims:
        scanned = await _scan_claim_rows(page)
        if scanned:
            clickable_rows = [r for r in scanned if r.get("clickable")]
            others = [r for r in scanned if not r.get("clickable")]
            for r in others:
                print(f"  [i] 该行没有可点击标记（排到后面）: {r['claim']} ({r.get('tag')}) {r.get('sample','')[:50]}")
            usable = clickable_rows + others
            print(f"  [i] 表格行扫描提取报案号（{len(usable)} 个，其中可点击 {len(clickable_rows)} 个）")
            claims = [{"claim_no": r["claim"], "status": "", "el": None} for r in usable]

    # 策略 3：仍是 0 → 页面文本正则兜底（不依赖 DOM，可能是 iframe 里的列表）
    if not claims:
        found = await find_claim_rows(page)
        if found:
            print(f"  [i] 未匹配到表格结构，改用页面文本提取报案号（{len(found)} 个）")
            claims = [{"claim_no": c, "status": "", "el": None} for c in found]

    # 策略 4：还是 0 个 → 可能是列表靠『查询』按钮异步加载 / 页面停在别的 tab
    if not claims:
        await _dump_empty_list_diag(page)
        try:
            for sel in ["button:has-text('查询')", "input[value='查询']",
                        "a:has-text('查询')", "button:has-text('搜索')"]:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    print(f"  [i] 列表为空，尝试点击『查询』({sel}) 重新加载")
                    await loc.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    found = await find_claim_rows(page)
                    if found:
                        print(f"  [i] 点击『查询』后读取到 {len(found)} 个报案号")
                        claims = [{"claim_no": c, "status": "", "el": None} for c in found]
                    break
        except Exception as exc:
            print(f"  [!] 点击『查询』失败: {exc}")

    # 策略 4：真实门户常把案件放在『我的案件 / 待办案件』菜单里，首页只显示待办小组件
    if not claims:
        for label in ("我的案件", "待办案件", "待处理", "案件列表", "待办事宜"):
            try:
                loc = page.get_by_text(label, exact=True).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                print(f"  [i] 列表为空，尝试进入『{label}』")
                await loc.click()
                await asyncio.sleep(2)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                found = await find_claim_rows(page)
                if found:
                    print(f"  [i] 进入『{label}』后读取到 {len(found)} 个报案号")
                    claims = [{"claim_no": c, "status": "", "el": None} for c in found]
                    break
            except Exception:
                continue

    print(f"  [✓] 找到 {len(claims)} 个可处理报案号")
    for c in claims:
        print(f"      → {c['claim_no']}  ({c['status']})")

    context["pending_claims"] = [c["claim_no"] for c in claims]
    return [c["claim_no"] for c in claims]


# STEP 1: 理赔作业门户首页 → 点击报案号
# ============================================================
def _is_quote_url(u: str) -> bool:
    u = (u or "").lower()
    return "quote/index" in u or "quote_process" in u


# JS: 给一个已标记 data-bot-claim-pick 的元素发完整合成点击序列
# 绕开视觉叠层（z-index 更高的标签气泡可能挡在 claim-no span 上面挡住浏览器级点击）
_JS_SYNTH_CLICK = r"""
() => {
  const el = document.querySelector('[data-bot-claim-pick="1"]')
         || document.querySelector('[class*="claim-no"],[class*="claimNo"]');
  if (!el) return {ok: false, reason: 'no-el'};
  el.scrollIntoView({block: 'center', inline: 'center'});
  // 完整事件序列：mouseover → mousedown → mouseup → click
  const opts = { bubbles: true, cancelable: true, view: window, button: 0 };
  try { el.dispatchEvent(new MouseEvent('mouseover', opts)); } catch (e) {}
  try { el.dispatchEvent(new MouseEvent('mousedown', opts)); } catch (e) {}
  try { el.dispatchEvent(new MouseEvent('mouseup', opts)); } catch (e) {}
  const c = new MouseEvent('click', opts);
  c.synthetic = true;
  el.dispatchEvent(c);
  return {ok: true, tag: el.tagName, cls: (el.className||'').toString().substring(0,50)};
}
"""


async def _dispatch_claim_click(page: Page) -> bool:
    """给 claim-no 元素发 JS 合成点击（绕开视觉叠层的标签气泡）"""
    try:
        res = await page.evaluate(_JS_SYNTH_CLICK)
        if res and res.get("ok"):
            print(f"  [i] JS合成点击命中 {res.get('tag')}({res.get('cls','')[:40]})")
            return True
    except Exception:
        pass
    return False


async def _wait_quote_opens(page: Page, claim_no: str, before: set,
                            wait_s: float = 8.0) -> Optional[Page]:
    """等报价处理页出现（新标签页 或 同页路由）。独立函数供多种点击策略复用。"""
    wrong_pages = []
    start = time.monotonic()
    while time.monotonic() - start < wait_s:
        await asyncio.sleep(0.5)
        for p in page.context.pages:
            if p in before: continue
            u = p.url or ""
            if not u.startswith("http"): continue
            if _is_quote_url(u):
                print(f"  [i] 报价处理在新标签页打开: {u[:100]}")
                return p
            if p not in wrong_pages:
                wrong_pages.append(p)
                print(f"  [!] 该项打开的不是报价页: {u[:90]} → 稍后关闭")
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if _is_quote_url(cur) or (claim_no and claim_no in cur):
            return page
    for p in wrong_pages:
        try:
            if p in page.context.pages:
                await p.close()
                print(f"  [i] 已关闭点错打开的标签: {(p.url or '')[:80]}")
        except Exception:
            continue
    return None


async def _click_and_wait_quote(page: Page, target, label: str, claim_no: str,
                                wait_s: float = 8.0,
                                double: bool = False,
                                press_enter: bool = False) -> Optional[Page]:
    """点一个入口，等报价处理页出现（新标签页 或 同页路由）；失败返回 None

    double=True → 双击（部分门户的 el-table 行是双击打开）
    press_enter=True → 点击后补一个回车（单元格可聚焦但单击不生效的场景）
    """
    before = set(page.context.pages)
    try:
        await target.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    clicked = False
    try:
        if double:
            await target.dblclick(timeout=6000)
        else:
            await target.click(timeout=9000)
        clicked = True
        print(f"  [✓] 点击: {label}")
        log_audit(f"点击: {label}")
    except Exception as exc:
        print(f"  [!] 直接点击失败({label}): {exc}")
        try:
            await target.dispatch_event("click")
            clicked = True
            print(f"  [i] 已改用 JS dispatch click 触发: {label}")
            log_audit(f"点击(JS派发): {label}")
        except Exception:
            pass
    if not clicked:
        return None

    if press_enter:
        try:
            await page.keyboard.press("Enter")
            print(f"  [i] 已补按回车: {label}")
        except Exception:
            pass

    return await _wait_quote_opens(page, claim_no, before, wait_s)


_JS_ROW_HTML = r"""
(claim) => {
  const re = new RegExp(claim, 'i');
  for (const tr of Array.from(document.querySelectorAll('tbody tr, .el-table__row'))) {
    if (!re.test(tr.innerText || '')) continue;
    return {
      html: (tr.outerHTML || '').replace(/\s+/g, ' ').slice(0, 1500),
      clickables: Array.from(tr.querySelectorAll('a, [onclick], [class*=link], span[style*=pointer]'))
        .slice(0, 6)
        .map(e => e.tagName + '.' + (e.className || '').toString().slice(0, 40) +
                  '|' + (e.textContent || '').trim().slice(0, 20)),
    };
  }
  return null;
}
"""


async def _dump_claim_row_html(page: Page, claim_no: str) -> None:
    """所有点击策略都失败时，把该行的 DOM 片段打进日志，方便下次精确对齐"""
    for fr in page.frames:
        try:
            info = await fr.evaluate(_JS_ROW_HTML, claim_no)
        except Exception:
            continue
        if not info:
            continue
        print(f"  [diag] 该行可点击元素: {info.get('clickables')}")
        print(f"  [diag] 该行 HTML: {info.get('html')}")
        log_audit(f"报案号行诊断 {claim_no}: {info.get('clickables')}")
        return
    print(f"  [diag] 页面上没找到含 {claim_no} 的表格行（可能是 iframe 或纯文本节点）")


async def _try_force_row_click(page: Page, claim_no: str) -> Optional[Page]:
    """『扫得到但点不开』的强点击兜底：单元格双击 / 行双击 / 点击+回车 / 行内链接"""
    row = page.locator(f"tr:has-text('{claim_no}')").first
    try:
        if await row.count() == 0 or not await row.is_visible():
            return None
    except Exception:
        return None

    targets: list[tuple[str, object]] = []
    try:
        cell = row.locator("td", has_text=claim_no).first
        if await cell.count() > 0:
            targets.append(("报案号所在单元格", cell))
    except Exception:
        pass
    # 流程图列的『一级报价 / 报价』文字入口（人手工点的就是这个）
    for txt, label in [("一级报价", "流程图·一级报价"), ("报价", "流程图·报价")]:
        try:
            loc = row.get_by_text(txt).first
            if await loc.count() > 0 and await loc.is_visible():
                targets.append((label, loc))
        except Exception:
            continue
    try:
        loc = row.locator("a").first
        if await loc.count() > 0 and await loc.is_visible():
            targets.append(("行内链接", loc))
    except Exception:
        pass
    targets.append(("整行", row))

    # 只保留前 4 个目标，避免在死行上耗时过久
    for label, t in targets[:4]:
        for mode in ("dblclick", "click+enter"):
            tag = f"{label}({mode})"
            print(f"  [i] 强点击尝试: {tag}")
            if mode == "dblclick":
                qp = await _click_and_wait_quote(page, t, tag, claim_no, wait_s=4, double=True)
            else:
                qp = await _click_and_wait_quote(page, t, tag, claim_no, wait_s=4, press_enter=True)
            if qp is not None:
                return qp
    return None


@register_step(desc="首页点击报案号", inputs=["claim_no"])
async def step_home_click_claim(page: Page, context: dict, **kwargs) -> Page:
    """点击报案号，返回『报价处理』所在的页面对象

    mock 是同页跳转；生产门户是在**新标签页**打开报价处理页
    （/claimcar/32160/quote/index?...&registNo=报案号），必须跟进新标签，
    否则后续 STEP 会错在首页标签上操作。
    """
    claim_no = kwargs.get("claim_no") or context.get("claim_no", "")
    print(f"\n🔹 STEP 1: 首页 → 点击报案号 {claim_no}")
    await page.wait_for_load_state("networkidle")
    await human_delay()

    # 人工等效：已人工点进该报案号的处理页（URL 带报案号）→ 跳过点击
    if claim_no and claim_no in (page.url or ""):
        print(f"  [i] 页面已在 {claim_no} 的处理页（可能人工已点击）→ 跳过")
        log_audit(f"跳过点击报案号（已在处理页）: {claim_no}")
        return page

    link = await find_claim_link(page, claim_no)
    if link is None:
        if claim_no and claim_no in (page.url or ""):
            print(f"  [i] 确认已在 {claim_no} 处理页，继续")
            return page
        raise RuntimeError(f"首页未找到报案号链接 {claim_no}，且当前不在其处理页")

    # ① 点报案号
    quote_page = await _click_and_wait_quote(page, link, f"报案号 {claim_no}", claim_no, wait_s=8)

    # ② 重试：先按 Esc 收起可能残留的浮动筛选面板，再重新定位点击
    if quote_page is None:
        print("  [i] 未检测到报价页 → 收起浮层后重新定位点击")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.wait_for_timeout(500)
        except Exception:
            pass
        link2 = await find_claim_link(page, claim_no)
        if link2 is not None:
            quote_page = await _click_and_wait_quote(
                page, link2, f"报案号 {claim_no}(重试)", claim_no, wait_s=8)

    # ③ 再兜底：点同一行其它入口（流程图「一级报价」/ 操作图标）
    if quote_page is None:
        for label, loc in await find_claim_row_actions(page, claim_no):
            print(f"  [i] 尝试同一行入口: {label}")
            quote_page = await _click_and_wait_quote(page, loc, label, claim_no, wait_s=6)
            if quote_page is not None:
                break

    # ③.5 强点击兜底：单元格双击 / 整行双击 / 点击+回车
    #      —— 针对「列表里扫得到报案号、但单击没反应」的门户行
    if quote_page is None:
        quote_page = await _try_force_row_click(page, claim_no)
        if quote_page is not None:
            print(f"  [✓] 强点击成功打开报价页")

    # ③.6 JS 合成点击：绕开视觉叠层（z-index 更高的「省内/省间/集/超60天」等标签气泡挡在 claim-no span 上面）
    #      浏览器级 .click() 被标签气泡截获 → 用 dispatchEvent 直接发给 claim-no span 元素
    if quote_page is None:
        for fr in page.frames:
            await fr.evaluate("document.querySelectorAll('[data-bot-claim-pick]').forEach(e=>e.removeAttribute('data-bot-claim-pick'))")
            info = await fr.evaluate(_JS_FIND_CLAIM_EXACT, claim_no)
            if info and info.get("found") and not info.get("lowScore"):
                before = set(page.context.pages)
                await _dispatch_claim_click(fr)
                quote_page = await _wait_quote_opens(page, claim_no, before, wait_s=10)
                if quote_page is not None:
                    print(f"  [✓] JS合成点击成功打开报价页（绕开标签气泡叠层）")
                    break

    if quote_page is not None:
        # 同页路由 / 新标签都已经拿到 → 直接收尾
        try:
            await quote_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            await quote_page.bring_to_front()
        except Exception:
            pass
        context["quote_page"] = quote_page
        await human_delay()
        return quote_page

    # ④ 多次点击都没跳转 → 再等一会儿同页路由（生产 SPA 偶发慢跳转）
    print(f"  [i] 多次尝试仍未打开报价页，继续等同页路由跳转（最多 12s）...")
    before_final = set(page.context.pages)
    start = time.monotonic()
    while time.monotonic() - start < 12:
        await asyncio.sleep(0.6)
        cur = page.url or ""
        if _is_quote_url(cur) or (claim_no and claim_no in cur):
            quote_page = page
            print(f"  [✓] 同页路由跳转成功: {cur[:100]}")
            break
        for p in page.context.pages:
            if p not in before_final and _is_quote_url(p.url):
                quote_page = p
                break
        if quote_page is not None:
            break

    if quote_page is None:
        print(f"  [i] 未检测到报价处理页跳转/新标签，继续在当前页: {(page.url or '')[:100]}")
        print(f"  [i] 若门户其实弹了提示（如『案件已被占用』），以下是当前可见弹层:")
        await dump_visible_overlays(page)
        await _dump_claim_row_html(page, claim_no)
        quote_page = page
    else:
        try:
            await quote_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            await quote_page.bring_to_front()
        except Exception:
            pass
    context["quote_page"] = quote_page
    await human_delay()
    return quote_page


def _is_loss_url(u: str) -> bool:
    u = (u or "").lower()
    return ("loss" in u or "vehicle/" in u or "damageid" in u
            or "loss_assessment" in u)


# ============================================================
# STEP 2: 报价处理 → 点"编辑" → 新标签页打开定损平台
# ============================================================
@register_step(desc="报价处理页面点击编辑，打开定损平台 popup", outputs=["loss_page"])
async def step_quote_open_loss_edit(page: Page, context: dict, **kwargs) -> Page:
    print("\n🔹 STEP 2: 报价处理 → 点击「编辑」进入定损平台")
    print(f"  [i] 当前操作页面: {(page.url or '')[:110]}")

    # ===== URL 守门：必须真的在报价页才能点编辑 =====
    # 生产门户首页（/claimcar/32160/ 或 /claimOperate/）表格里也有 "编辑" 按钮
    # （用来编辑案件备注/一级报价），如果误在首页上找「编辑」就会点到错误按钮
    # → 必须先确认当前页 URL 是 quote/index 或类似报价页路径
    cur_url = (page.url or "").lower()
    is_quote = _is_quote_url(page.url)
    # 额外宽松检查：URL 里带 claimcar + quote 说明在某个报价详情页
    if not is_quote:
        try:
            title = await page.title()
        except Exception:
            title = ""
        # 如果 URL 不含 quote，但页面里有 #editButtons（报价页 DOM 特征），也算过
        try:
            has_edit_buttons = await page.locator("#editButtons").count() > 0
        except Exception:
            has_edit_buttons = False
        if not has_edit_buttons:
            raise RuntimeError(
                f"当前不在报价处理页（URL={cur_url[:100]}，标题={title[:50]}），"
                f"找不到报价页的 #editButtons，拒绝点击『编辑』按钮（避免误点门户首页的备注编辑）"
            )
        print("  [warn] URL 不是 quote/index，但检测到 #editButtons DOM → 继续")

    # 生产报价页是 Vue SPA，渲染慢：先等 #editButtons 挂载再点，避免误点别的『编辑』
    if _is_quote_url(page.url):
        try:
            await page.wait_for_selector("#editButtons", state="attached", timeout=20000)
            print("  [i] 报价页 #editButtons 已就绪")
        except Exception:
            print("  [i] 等待 #editButtons 超时，转用候选选择器兜底")

    # 人工等效：定损平台标签页已被人工打开 → 直接复用，不再点「编辑」
    # （生产定损平台 URL 形如 http://10.28.15.151:8090/vehicle/?damageId=...）
    # ⚠️ 必须核对是不是「本单」的定损页：上一单（尤其是直供价跳过的那单）标签页不会自动关，
    #    无脑复用会把上一单的零件表/直供价当成本单数据 → 后续所有报案号全被误判跳过
    _cur_claim = (context.get("claim_no") or "").strip()
    try:
        for p in list(page.context.pages):
            if p is page or not _is_loss_url(p.url):
                continue
            _state = await _loss_page_claim_state(p, _cur_claim)
            if _state is True:
                print(f"  [i] 定损平台已是本单（{_cur_claim}）→ 复用现有页面")
                log_audit("跳过点击编辑（定损平台已是本单）")
                await p.bring_to_front()
                return p
            print(f"  [i] 残留的定损页不属于本单（{_cur_claim or '无上下文'}，"
                  f"{'读到别的报案号' if _state is False else '未渲染出报案号'}）→ 关掉它，重新点编辑")
            log_audit("关闭上一单残留的定损页，重新打开本单")
            try:
                await p.close()
            except Exception:
                pass
    except Exception:
        pass

    # ==== 候选选择器收紧：优先 #editButtons 容器内的编辑按钮，通用兜底仅作最后手段 ====
    # 生产报价页实测：<div id="editButtons"><button class="el-button--primary el-button--mini"><span> 编辑 </span></button></div>
    # 注意：不能把 "button:has-text('编辑')" 这种通用选择器放前面 — 门户首页表格也有「编辑」按钮
    candidates = [
        "#editButtons button",
        "#editButtons .el-button",
        "#editButtons span",
        SELECTORS_PORTAL["edit_parts"],  # "#open-loss-edit"
        "a:has-text('编辑')",
        # 通用兜底（加 container 限制，尽量限定在表单/卡片内，避免首页表格的「编辑」）
        ".quote-card button:has-text('编辑')",
        ".quote-panel button:has-text('编辑')",
        "[class*='quote'] button:has-text('编辑')",
        "[class*='detail'] button:has-text('编辑')",
    ]
    frame, edit_btn = await find_clickable(page, candidates, ["编辑"], "报价页编辑按钮", timeout=12000)

    ctx = page.context
    before = {p: (p.url or "") for p in ctx.pages}
    new_page = None
    try:
        async with page.expect_popup(timeout=DEFAULT_TIMEOUT) as popup_info:
            await edit_btn.click()
        new_page = await popup_info.value
    except Exception:
        new_page = None
    try:
        await frame.evaluate(
            "() => document.querySelectorAll('[data-bot-pick]')"
            ".forEach(e => e.removeAttribute('data-bot-pick'))")
    except Exception:
        pass

    if new_page is None:
        # 生产门户有时用 window.open(url, 固定窗口名) 或新开标签页，
        # Playwright 收不到 popup 事件 —— 轮询找回：
        #   ① 新出现的标签页（按对象身份，不限 URL 模式；等它从 about:blank 导航完）
        #   ② 已有标签页被导航到定损平台（URL 特征 / 标题含「定损」）
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and new_page is None:
            for p in ctx.pages:
                if p is page or p in before:
                    continue
                if (p.url or "").startswith("http"):
                    new_page = p
                    break
            if new_page is None:
                for p, old in before.items():
                    if p is page:
                        continue
                    u = p.url or ""
                    if u != old and _is_loss_url(u):
                        new_page = p
                        break
            if new_page is None:
                await asyncio.sleep(0.8)
        if new_page is None:
            # 标题兜底：定损平台标签标题通常含「定损」
            for p in ctx.pages:
                if p is page:
                    continue
                try:
                    if "定损" in (await p.title()):
                        new_page = p
                        break
                except Exception:
                    pass
        if new_page is not None:
            print(f"  [i] 未见 popup，但发现新打开/已切换的定损标签页: {(new_page.url or '')[:110]}")
            log_audit(f"编辑复用已有窗口: {new_page.url}")
        else:
            print("  [i] 点击编辑后没有新窗口 → 判定在当前页打开了定损平台，继续使用当前页")
            print(f"     当前所有标签页: {[(p.url or '')[:110] for p in ctx.pages]}")
            log_audit("编辑未产生新窗口，继续使用当前页")
            try:
                await page.bring_to_front()
            except Exception:
                pass
            new_page = page

    try:
        await new_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    print(f"  [✓] 已打开定损平台: {new_page.url}")
    # 定损平台进入即弹『提示』（已阅知）→ 先关掉，否则会挡住复选框和同步定损
    await dismiss_entry_notice(new_page)
    return new_page


# ---------- 通用弹窗检测：覆盖 3 种常见 UI 框架 + 兜底 ----------
# 弹窗容器选择器（从最知名到最通用）
_DIALOG_CONTAINER_SELECTORS = [
    # Element UI
    ".el-message-box__wrapper:visible", ".el-message-box:visible", ".el-message:visible",
    # Bootstrap / AdminLTE modal（定损平台实测就是这种，class = "modal fade in"）
    ".modal.in:visible", ".modal.show:visible", ".modal.fade.in:visible",
    ".modal:visible", ".modal-dialog:visible", ".modal-content:visible",
    # Ant Design
    ".ant-modal:visible", ".ant-message:visible",
    # Layui（国产后台框架）
    ".layui-layer:visible",
    # jQuery UI
    ".ui-dialog:visible",
    # 通用 role 属性
    "[role='dialog']:visible", "[role='alertdialog']:visible",
    # 通用 class 关键字
    ".dialog:visible", ".popup:visible", ".alert-dialog:visible",
    ".confirm-dialog:visible", ".message-box:visible", ".messagebox:visible",
]

# 确定/关闭按钮选择器（在弹窗容器里找）
# 含"关闭当前窗口"这类收尾按钮（STEP 9 提交成功后系统提示弹窗用这个）
_CONFIRM_BTN_SELECTORS = [
    "button:has-text('确定')",
    "button:has-text('确 定')",
    "button:has-text('确认')",
    "button:has-text('确 认')",
    "button:has-text('OK')",
    "button:has-text('Ok')",
    "button:has-text('是')",
    "button:has-text('关闭当前窗口')",
    "button:has-text('关闭')",
    "button:has-text('知道了')",
    "button:has-text('我知道了')",
    "button:has-text('已阅知')",
    "button:has-text('好的')",
    "button:has-text('完成')",
    "[role='button']:has-text('确定')",
    "[role='button']:has-text('已阅知')",
    "[role='button']:has-text('关闭当前窗口')",
    ".btn:has-text('确定')",
    ".btn:has-text('关闭')",
    ".el-button:has-text('确定')",
    ".el-button:has-text('关闭当前窗口')",
    ".ant-btn:has-text('确定')",
    "a:has-text('确定')",
    "a:has-text('关闭')",
    "input[value='确定']",
    "input[value='关闭']",
]

# 通用兜底 JS：在整个 frame 里找 z-index 最高、包含「确定/关闭/完成」按钮的可见遮罩
_FIND_CONFIRM_JS = """() => {
  const candidates = document.querySelectorAll(
    'div, [role="dialog"], [role="alertdialog"], section, aside'
  );
  let best = null, bestZ = 0;
  for (const el of candidates) {
    const s = getComputedStyle(el);
    const isOverlay =
      (s.position === 'fixed' || s.position === 'absolute') &&
      parseInt(s.zIndex || '0') >= 100 &&
      s.display !== 'none' && s.visibility !== 'hidden';
    if (!isOverlay) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 100 || r.height < 60) continue;
    const hasConfirm = el.querySelector(
      "button, a, [role='button'], input[type='button']"
    );
    if (!hasConfirm) continue;
    const txt = (hasConfirm.innerText || hasConfirm.value || '').trim();
    if (!txt || !/确定|确 定|确认|关闭当前窗口|关闭|知道了|已阅知|好的|完成|OK|Ok|是/i.test(txt)) continue;
    if (parseInt(s.zIndex || '0') > bestZ) { bestZ = parseInt(s.zIndex || '0'); best = el; }
  }
  if (!best) return null;
  best.setAttribute('data-bot-dialog', '1');
  const btn = best.querySelector(
    "button, a, [role='button'], input[type='button']"
  );
  btn.setAttribute('data-bot-confirm', '1');
  return {
    tag: best.tagName, cls: best.className, z: bestZ,
    btn: (btn.innerText || btn.value || '').trim()
  };
}
"""


async def dismiss_entry_notice(page: Page) -> bool:
    """定损平台一打开就自动弹出的『提示』窗 → 点『已阅知』关掉

    这个窗是纯阅知提示（不是业务确认），不关会盖住零件表，
    导致后面的「全选复选框 / 同步定损」点不到。只认阅知类按钮，不碰『确定』。
    """
    for _ in range(3):
        for fr in page.frames:
            for sel in _DIALOG_CONTAINER_SELECTORS:
                try:
                    box = fr.locator(sel).last
                    if await box.count() == 0 or not await box.is_visible():
                        continue
                    for txt in ("已阅知", "我已知晓", "已知晓", "知道了", "我知道了"):
                        btn = box.locator(
                            f"button:has-text('{txt}'), .btn:has-text('{txt}'), "
                            f"[role='button']:has-text('{txt}'), a:has-text('{txt}')").first
                        if await btn.count() == 0 or not await btn.is_visible():
                            continue
                        await btn.click(timeout=2500)
                        print(f"  [✓] 已关闭定损平台提示窗（点『{txt}』）")
                        log_audit(f"关闭定损平台提示窗：{txt}")
                        await asyncio.sleep(0.3)
                        return True
                except Exception:
                    continue
        await asyncio.sleep(0.4)
    return False


async def confirm_dialog(page: Page, timeout: float = 10.0) -> bool:
    """通用弹窗确认：轮询所有标签页/所有 frame，用三层策略找弹窗并点『确定』

    覆盖范围（按优先级尝试）：
      ① 知名框架专用选择器：Element UI / Bootstrap modal / Ant Design / Layui / jQuery UI
      ② 通用 role / class 关键字匹配
      ③ JS 兜底：找 z-index>=100 的最大遮罩，里面必须含「确定」按钮
    原生 dialog 已由全局监听器自动处理，这里只管 DOM 弹窗。

    生产实测：定损平台同步定损弹窗是 Bootstrap modal（class="modal fade in"），
    不是 Element UI 的 el-message-box；报价页也可能弹 Element UI / 原生 alert / 自定义 popup，
    这里统一处理。
    """
    deadline = time.monotonic() + timeout
    seen = set()  # 去重：同一个弹窗不要反复点

    while time.monotonic() < deadline:
        for pg in list(dict.fromkeys([page, *page.context.pages])):
            for fr in pg.frames:
                frame_key = (pg.url or "", fr.url or "")
                try:
                    # --- 层 ①②：按选择器找 ---
                    for sel in _DIALOG_CONTAINER_SELECTORS:
                        try:
                            box = fr.locator(sel).last
                            if await box.count() == 0 or not await box.is_visible():
                                continue
                            box_text = await box.inner_text() or ""
                            flat = "".join(box_text.split())

                            # 黑名单：以下业务提示框应由专用 handler 按正确按钮处理，
                            # 通用 confirm_dialog 一律跳过不碰，否则会点错"确定"
                            _SKIP_KEYWORDS = (
                                # ① 报价总额超过保险金额 + 是否重新报价 → 必须点"取消"
                                ("报价总额超过保险金额", "是否重新报价"),
                                # ② 已本地化换件超 5 万 → 专用 handler 会走分中心报价分支
                                ("已本地化换件", "5万元"),
                                # ③ 标的车报价合计金额大于保险金额 → 专用 handler 链路更完整
                                ("标的车报价", "保险金额", "提交下一节点"),
                                # ④ 直供拟采购提示 → 必须点"否"（专用 handler 处理）
                                ("直供平台询价状态已接受", "拟采购"),
                            )
                            if any(all(k in flat for k in kw_pair) for kw_pair in _SKIP_KEYWORDS):
                                continue

                            sig = (frame_key, sel, box_text)
                            if sig in seen:
                                continue
                            for btn_sel in _CONFIRM_BTN_SELECTORS:
                                try:
                                    btn = box.locator(btn_sel).first
                                    if await btn.count() == 0:
                                        continue
                                    if not await btn.is_visible():
                                        continue
                                    await btn.scroll_into_view_if_needed(timeout=2000)
                                    await btn.click(timeout=3000)
                                    seen.add(sig)
                                    print(f"  [✓] 已点弹窗『确定』（选择器 {sel[:40]}，按钮 {btn_sel}）")
                                    log_audit(f"弹窗确认: {sel[:40]} → {btn_sel}")
                                    return True
                                except Exception:
                                    continue
                        except Exception:
                            continue

                    # --- 层 ③：JS 兜底扫遮罩 ---
                    try:
                        info = await fr.evaluate(_FIND_CONFIRM_JS)
                    except Exception:
                        continue
                    if info:
                        sig = (frame_key, "js-fallback", info.get("cls", ""))
                        if sig in seen:
                            continue
                        try:
                            btn = fr.locator("[data-bot-confirm='1']").first
                            if await btn.count():
                                await btn.scroll_into_view_if_needed(timeout=2000)
                                await btn.click(timeout=3000)
                                seen.add(sig)
                                print(f"  [✓] 已点弹窗『确定』（JS 兜底：class={info.get('cls','')[:40]} btn={info.get('btn','')}）")
                                log_audit(f"弹窗确认(JS兜底): {info.get('cls','')[:40]} → {info.get('btn','')}")
                                return True
                        except Exception:
                            continue
                except Exception:
                    pass
        await asyncio.sleep(0.4)
    return False


# 向后兼容别名（3.6.0 / 3.6.1 里的名字）
confirm_el_messagebox = confirm_dialog


async def dismiss_all_modals(page: Page, max_rounds: int = 3, per_timeout: float = 3.0) -> int:
    """扫残留弹窗：连点最多 max_rounds 次『确定/关闭/关闭当前窗口/完成』等收尾按钮，
    返回实际点掉的个数。
    用于：STEP 9 提交成功后清掉系统提示弹窗、每轮循环收尾兜底清理。
    """
    clicked = 0
    for _ in range(max_rounds):
        ok = await confirm_dialog(page, timeout=per_timeout)
        if not ok:
            break
        clicked += 1
        await asyncio.sleep(0.4)
    return clicked


async def dump_visible_overlays(page: Page, limit: int = 6) -> None:
    """诊断：列出所有标签页/所有 frame 里可见的顶层遮罩/弹层，便于识别未知弹窗结构"""
    js = """() => [...document.querySelectorAll('body > div')].filter(d => {
        const s = getComputedStyle(d);
        return (s.position === 'fixed' || s.position === 'absolute')
            && parseInt(s.zIndex || '0') >= 100
            && s.display !== 'none' && s.visibility !== 'hidden';
    }).map(d => (d.className || d.id || d.tagName) + ' | ' + (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 100))"""
    for pg in list(dict.fromkeys([page, *page.context.pages])):
        for fr in pg.frames:
            try:
                items = await fr.evaluate(js)
            except Exception:
                continue
            for it in (items or [])[:limit]:
                print(f"     弹层[{(fr.url or '').split('/')[-1][:40] or 'main'}]: {it}")


# ============================================================
# STEP 3: 定损平台 → 同步定损（打开定损平台后的第一个动作）
#   分支 1：有换件 → 全选复选框 → 点「同步定损」
#   分支 2：无换件（列表为空，复选框点不了）→ 直接点「同步定损」
# ============================================================
async def _frame_locator(page: Page, selector: str):
    """在页面及其所有 iframe（含嵌套）里找第一个匹配元素 → (frame, locator)

    生产门户是 Vue SPA，定损/报价表单常整体嵌在 iframe 里，
    直接 page.locator 只能看到主文档，会读不到 VIN / 换件表。

    Playwright 的 page.frames 已递归包含所有嵌套 iframe，
    但这里额外加诊断：把遍历过的 frame 数打印出来，方便排查。
    """
    frames_seen = 0
    for frame in page.frames:
        frames_seen += 1
        try:
            loc = frame.locator(selector).first
            if await loc.count() > 0:
                print(f"  [diag] _frame_locator 命中: frame={frame.url[:60] if frame.url else 'main'} sel={selector} (共遍历 {frames_seen} 个 frame)")
                return frame, loc
        except Exception as exc:
            print(f"  [diag] _frame_locator frame={frame.url[:60] if frame.url else 'main'} sel={selector} 异常: {type(exc).__name__}: {exc}")
            continue
    return page.main_frame, page.locator(selector).first


# ---------- VIN 提取：生产定损平台实测结构 ----------
# 截图确认：<span class="...read_item_lable...">定损VIN：</span>
#           <span class="...read_item_lable..."><span>"LFV3A24K5L3143247"</span></span>
# 值被 <script type="jsv#90~"></script> 包住（这是 JSV/RichTextBox 的文本标记），
# 元素上没有任何 id/class 含 vin，所以按选择器永远找不到，必须按「标签邻域」取文本。
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def _clean_vin(text: str) -> str:
    """从任意文本里切出一个合法 VIN（17 位、去掉引号/空格/中文）"""
    up = (text or "").upper()
    for tok in re.split(r"[^A-Z0-9]+", up):
        if not _VIN_RE.fullmatch(tok):
            continue
        if tok.isdigit():          # 17 位纯数字更像单号/业务号
            continue
        if tok.startswith("RD"):   # 报案号
            continue
        return tok
    return ""


# 页内 JS 兜底：按「定损VIN / 车架号」标签的邻域取 VIN，最后全页扫
_JS_FIND_VIN = """
() => {
  const VIN_RE = /^[A-HJ-NPR-Z0-9]{17}$/;
  const LABELS = ['定损vin', '车架号', 'vin码', 'vin'];
  const tokens = (s) => String(s || '').toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean);
  const pick = (s) => {
    for (const t of tokens(s)) {
      if (!VIN_RE.test(t)) continue;
      if (/^[0-9]{17}$/.test(t)) continue;
      if (t.indexOf('RD') === 0) continue;
      return t;
    }
    return '';
  };
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  // ① data-label 带 vin 的元素（部分版本用 car-info-value + data-label）
  for (const el of document.querySelectorAll('[data-label]')) {
    const lb = String(el.getAttribute('data-label') || '').toLowerCase();
    if (!lb.includes('vin') && !lb.includes('车架')) continue;
    const v = pick(el.textContent);
    if (v) return {vin: v, how: 'data-label=' + el.getAttribute('data-label')};
  }
  // ② 含「定损VIN/车架号」标签的最短元素 → 自身 / 下一个兄弟 / 父级 三层
  const labels = [];
  for (const el of document.querySelectorAll('span,div,td,th,li,label,p,dd,em,strong,b')) {
    const own = String(el.textContent || '').trim();
    if (!own || own.length > 40) continue;
    const low = own.toLowerCase().replace(/\\s+/g, '');
    if (!LABELS.some(k => low.includes(k))) continue;
    if (!visible(el)) continue;
    labels.push(el);
  }
  for (const el of labels) {
    if (pick(el.textContent)) return {vin: pick(el.textContent), how: '标签自身'};
    const nb = [el.nextElementSibling,
                el.parentElement,
                el.parentElement && el.parentElement.nextElementSibling,
                el.parentElement && el.parentElement.parentElement];
    for (const n of nb) {
      if (!n) continue;
      const v = pick(n.textContent);
      if (v) return {vin: v, how: '标签邻域'};
    }
  }
  // ③ 全页兜底
  const v = pick(document.body ? document.body.textContent : '');
  return v ? {vin: v, how: '全页扫描'} : null;
}
"""


# JS 方案：在所有 frame（含嵌套 iframe）里查找换件表行数（首选，绕开 Playwright locator 限制）
_JS_COUNT_LOSS_PARTS = """
() => {
  let maxRows = 0;
  let how = '';
  let frameInfo = [];

  // 换件表候选选择器（tbody 层面）
  const TBODY_SELS = [
    '#partListTable tbody',
    '.bootstrap-table .fixed-table-body .table tbody',
    '.bootstrap-table .table tbody',
    '#evalPartDiv table tbody',
    '.parts-table tbody',
    '#parts-tbody',
  ];
  // 数据行候选（排除 thead/tfoot）
  const ROW_SELS = [
    '#partListTable tbody tr',
    '.bootstrap-table .fixed-table-body .table tbody tr',
    '.bootstrap-table .table tbody tr',
    '#evalPartDiv table tbody tr',
    '.parts-table tbody tr',
    '#parts-tbody tr',
  ];

  // ① 主文档
  for (const sel of ROW_SELS) {
    const trs = document.querySelectorAll(sel);
    frameInfo.push({doc: 'main', sel, count: trs.length});
    if (trs.length > maxRows) {
      // 排除"没有数据"占位行
      if (trs.length === 1) {
        const txt = (trs[0].textContent || '').trim();
        if (txt.includes('没有数据') || txt.includes('暂无') || txt.includes('没有记录') || txt.includes('无换件')) continue;
      }
      maxRows = trs.length;
      how = '主文档 ' + sel;
    }
  }

  // ② 所有 iframe（含嵌套，递归）
  function scanIframes(doc, depth) {
    const iframes = doc ? doc.querySelectorAll('iframe') : [];
    for (const iframe of iframes) {
      try {
        const subDoc = iframe.contentDocument;
        if (!subDoc) continue; // 跨域或未加载
        for (const sel of ROW_SELS) {
          const trs = subDoc.querySelectorAll(sel);
          frameInfo.push({doc: 'iframe(depth=' + depth + ',name=' + (iframe.name || '') + ')', sel, count: trs.length});
          if (trs.length > maxRows) {
            if (trs.length === 1) {
              const txt = (trs[0].textContent || '').trim();
              if (txt.includes('没有数据') || txt.includes('暂无') || txt.includes('没有记录') || txt.includes('无换件')) continue;
            }
            maxRows = trs.length;
            how = 'iframe(depth=' + depth + ') ' + sel;
          }
        }
        // 递归扫嵌套 iframe
        scanIframes(subDoc, depth + 1);
      } catch(e) {
        // 跨域 iframe：忽略
      }
    }
  }
  scanIframes(document, 1);

  return {count: maxRows, how, frameInfo: frameInfo.slice(0, 20)}; // 最多返回20条诊断
}
"""


async def _count_loss_parts(page: Page, retries: int = 3, interval: float = 2.0) -> int:
    """统计换件列表行数（0 表示该案件无需换件）

    定损平台有两种表格：
      - 生产：bootstrap-table → 选择器 `#partListTable tbody` / `.bootstrap-table .table tbody`
      - mock：AdminLTE → `#parts-tbody`

    策略：
      ① 首选：JS 执行（递归扫所有 iframe，最可靠）
      ② 兜底：Playwright locator（多选择器循环 + 重试等待异步加载）
    """
    tbody_selectors = [
        "#partListTable tbody",
        ".bootstrap-table .fixed-table-body .table tbody",
        ".bootstrap-table .table tbody",
        "#evalPartDiv table tbody",
        ".parts-table tbody",
        "#parts-tbody",
    ]

    for attempt in range(1, max(1, retries) + 1):
        print(f"  [i] 换件表统计（尝试 {attempt}/{retries}）...")

        # ── ① 首选：JS 执行方案 ──
        try:
            js_result = await page.evaluate(_JS_COUNT_LOSS_PARTS)
            js_count = js_result.get("count", 0)
            js_how = js_result.get("how", "")
            print(f"  [diag] JS 方案 → 命中 {js_how} → {js_count} 行")
            # 打印 frame 诊断（前10条）
            for fi in (js_result.get("frameInfo") or [])[:10]:
                print(f"  [diag]   frame={fi['doc']} sel={fi['sel']} count={fi['count']}")
            if js_count > 0:
                print(f"  [i] 换件表命中(JS) → {js_count} 行（尝试 {attempt}/{retries}）")
                return js_count
        except Exception as exc:
            print(f"  [!] JS 方案异常: {type(exc).__name__}: {exc}")

        # ── ② 兜底：Playwright locator 方案 ──
        locator_hit = False
        for sel in tbody_selectors:
            try:
                _, tbody = await _frame_locator(page, sel)
                tbody_count = await tbody.count()
                print(f"  [diag] locator 检查 sel={sel} tbody.count={tbody_count}")
                if tbody_count == 0:
                    continue
                rows = tbody.locator("tr")
                n = await rows.count()
                print(f"  [diag] locator sel={sel} rows.count={n}")
                if n == 0:
                    continue
                # 排除 bootstrap-table 的"没有数据..."占位行
                txt = (await rows.first.text_content() or "").strip()
                if n == 1 and any(k in txt for k in ["没有数据", "暂无", "无换件", "没有记录"]):
                    print(f"  [diag] locator sel={sel} 仅1行占位: '{txt[:50]}' → 跳过")
                    continue
                print(f"  [i] 换件表命中(locator) {sel} → {n} 行（尝试 {attempt}/{retries}）")
                return n
            except Exception as exc:
                print(f"  [diag] locator sel={sel} 异常: {type(exc).__name__}: {exc}")
                continue

        # 没查到，等一下重试（最后一次不等）
        if attempt < retries:
            print(f"  [i] 换件表暂未加载到（尝试 {attempt}/{retries}），{interval}s 后重试...")
            await asyncio.sleep(interval)

    print(f"  [i] 换件表：{retries} 次尝试均未命中，判定 0 行")
    return 0


@register_step(desc="同步定损（有换件先全选 / 无换件直接点）", outputs=["has_parts"])
async def step_loss_sync(page: Page, context: dict, **kwargs) -> bool:
    """
    打开定损平台后的第一步：同步定损
      - 有换件：点序号左侧全选复选框 → 点「同步定损」
      - 无换件：复选框不可点，直接点「同步定损」
    """
    print(f"\n🔹 STEP 3: 定损平台 → 同步定损")

    # 拦截同步定损的 alert 弹窗
    dialog_handler = asyncio.Event()
    def _on_dialog(d):
        dialog_handler.set()
        _accept_dialog(d)

    page.on("dialog", _on_dialog)

    try:
        await page.wait_for_load_state("networkidle")
        # 双保险：提示窗偶尔晚一点渲染出来，再关一次
        await dismiss_entry_notice(page)
        # 直供案件进入定损平台会弹「是否对询价完成配件进行拟采购?」→ 必须点『否』
        await _answer_direct_supply_ask_no(page, timeout=4.0)

        # 先判断有没有换件
        part_count = await _count_loss_parts(page)
        has_parts = part_count > 0
        context["has_parts"] = has_parts

        if has_parts:
            print(f"  [i] 检出 {part_count} 个换件 → 先全选复选框")
            # 生产 bootstrap-table 的全选框 id 是 btSelectAllD-0（D=定损）/ btSelectAllB-0（B=低碳）
            select_candidates = [
                "#btSelectAllD-0", "#btSelectAll", "#btSelectAllB-0",
                "thead input[type='checkbox']",
                SELECTORS_PORTAL["select_all_parts"],
                "input[name='btSelectAll']",
                "th.bs-checkbox input[type='checkbox']",
            ]
            select_all = None
            for sel in select_candidates:
                try:
                    _, select_all = await _frame_locator(page, sel)
                    if await select_all.count() > 0:
                        print(f"  [i] 全选复选框命中 {sel}")
                        break
                except Exception:
                    continue
            if select_all is None:
                select_all = page.get_by_role("checkbox", name="序号").first

            try:
                await select_all.wait_for(state="visible", timeout=5000)
                if not await select_all.is_checked():
                    await safe_click(select_all, "全选复选框（序号左侧）")
                else:
                    print("  [i] 零件已被全选，跳过点击")
                await human_delay()
            except Exception as exc:
                print(f"  [!] 全选复选框操作失败（继续直接同步）: {exc}")
        else:
            print(f"  [i] 该案件无需换件（换件列表为空）→ 跳过全选，直接点同步定损")

        # 点工具栏「同步定损」
        # 生产定损平台的同步按钮 id 是 #tbdsBtn（HTML 里看到的）
        sync_candidates = [
            "#tbdsBtn", "#sync-loss-btn",
            SELECTORS_PORTAL["sync_loss_btn"],
            "button:has-text('同步定损')", "a:has-text('同步定损')",
        ]
        sync_btn = page.locator("#tbdsBtn").first
        try:
            _, sync_btn = await find_clickable(
                page, sync_candidates,
                ["同步定损"], "同步定损按钮", timeout=6000)
        except RuntimeError:
            sync_btn = page.locator("#tbdsBtn").first
        await click_if_present(sync_btn, "同步定损按钮")

        # 等同步结果弹窗：
        #   mock 用浏览器原生 alert（监听器自动点确定）；
        #   生产用 Element UI el-message-box（「确认同步定损？」→ 点确定才真正同步）
        try:
            await asyncio.wait_for(dialog_handler.wait(), timeout=3.0)
            print("  [✓] 同步定损完成（原生弹窗已确认）")
        except asyncio.TimeoutError:
            if await confirm_el_messagebox(page, timeout=10.0):
                print("  [✓] 已点击提示框「确定」，同步定损继续")
                log_audit("同步定损确认弹窗：已点确定")
                # 点确定后可能还有成功提示（原生 alert / el-message-box），顺手再处理一轮
                try:
                    await asyncio.wait_for(dialog_handler.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    await confirm_el_messagebox(page, timeout=3.0)
            else:
                print("  [i] 同步定损无弹窗（真实内网可能不弹/人工已确认），继续")
                print("  [i] 若页面上其实有弹窗未处理，以下是当前可见弹层（反馈给我补规则）:")
                await dump_visible_overlays(page)

        await human_delay()
        return has_parts

    except Exception as exc:
        print(f"  [!] 同步定损失败（非致命）: {exc}")
        return False
    finally:
        page.remove_listener("dialog", _on_dialog)


# ============================================================
# STEP 4: 定损平台 → 提取 VIN + 换件列表（同步定损之后）
# ============================================================
# 直供判定：只认零件表里「与编码/名称/本地价同一行」的直供价单元格。
# 历史 bug 1（2026-11-12 上午，7 单全被跳过）：按字段名关键词匹配，
#   platDirectPrice（平台直供价）/ nonDirectPrice（非直供价）被误当成直供价。
# 历史 bug 2（2026-11-12 下午，首单之后全被跳过）：整页跨 frame 扫描直供价列，
#   读到了上一单残留的旧表格 → 150/80/100/30/100 重复出现。现在改成同表同行取值。
def _mark_direct_supply_skip(context: dict, has_direct_supply: bool) -> None:
    """直供价只要有值 → 标记（由 claim_flow 决定：先取消直供价再按正常案件处理）"""
    if not has_direct_supply:
        return
    context["skip_direct_supply"] = True
    print("\n  ⚠️ 检测到『直供价』列存在数值 → 进入『切换价格类型取消直供价』分支")


# ============================================================
# 直供价非 0 案件：切换「价格类型」取消直供价（2026-11-17 新增）
# ------------------------------------------------------------
# 业务顺序（生产截图确认）：
#   ① 进定损平台弹「直供平台询价状态已接受,是否对询价完成配件进行拟采购?」→ 点『否』
#   ② 全选零件（复用同步定损的全选）
#   ③ 点表头可点击文字『价格类型』→ 弹窗里点『R』→『确定』
#   ④ 再点『价格类型』→ 点回原价格类型（原 A 就点 A，原 S 就点 S）→『确定』
#   ⑤ 点零件列表『保存』→ 直供价被清空（可回读校验）
#   ⑥ 提交按钮同一行的输入框填『接受系统点选』
# ============================================================
_PRICE_TYPE_LABEL = {
    "MS_RETAIL_PRICE": "S",
    "MARKET_PRICE": "O",
    "MARKET_BRANK_PRICE": "A",
    "MARKET_RECYCLE_PRICE": "R",
}


async def _answer_direct_supply_ask_no(page: Page, timeout: float = 6.0) -> bool:
    """进入定损平台的『直供平台询价状态已接受,是否对询价完成配件进行拟采购?』→ 点『否』

    注意：这个框也有『确定/是』按钮，但业务要求必须点『否』（不拟采购），
    所以要在通用 confirm_dialog 之前抢先用专用 handler 处理。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in page.frames:
            for sel in _DIALOG_CONTAINER_SELECTORS:
                try:
                    box = fr.locator(sel).last
                    if await box.count() == 0 or not await box.is_visible():
                        continue
                    flat = "".join(((await box.inner_text()) or "").split())
                except Exception:
                    continue
                if "直供平台询价状态已接受" not in flat and "拟采购" not in flat:
                    continue
                try:
                    btns = await box.locator(
                        "button:visible, .btn:visible, [role='button']:visible").all()
                except Exception:
                    btns = []
                for btn in btns:
                    try:
                        if ((await btn.inner_text()) or "").strip() != "否":
                            continue
                        await btn.click(timeout=3000)
                        print("  [✓] 直供拟采购提示 → 选『否』（不拟采购）")
                        log_audit("直供拟采购提示：选『否』")
                        await asyncio.sleep(0.5)
                        return True
                    except Exception:
                        continue
        await asyncio.sleep(0.4)
    return False


async def _loss_table_frame(page: Page):
    """定位零件表所在 frame（bootstrap-table 可能在 iframe 里）"""
    for sel in ("#btSelectAllD-0", "tr[data-lossobj]", "table tbody tr"):
        for fr in page.frames:
            try:
                if await fr.locator(sel).count() > 0:
                    return fr
            except Exception:
                continue
    return page.main_frame


async def _set_all_parts_checked(page: Page, want: bool = True) -> bool:
    """勾选/取消零件表全选（bootstrap-table 全选框 id: btSelectAllD-0 / btSelectAllB-0）"""
    for sel in ("#btSelectAllD-0", "#btSelectAllB-0", "#btSelectAll",
                "thead input[type='checkbox']", "th.bs-checkbox input[type='checkbox']"):
        for fr in page.frames:
            try:
                cb = fr.locator(sel).first
                if await cb.count() == 0 or not await cb.is_visible():
                    continue
                cur = await cb.is_checked()
                if cur != want:
                    try:
                        await cb.click(timeout=3000)
                    except Exception:
                        await cb.click(timeout=2500, force=True)
                    await human_delay()
                print(f"  [i] 全选复选框 → {'勾选' if want else '取消'}（{sel}）")
                return True
            except Exception:
                continue
    return False


async def _price_type_modal(page: Page):
    """找『价格类型』弹窗（part_jglx）→ (frame, modal)；找不到返回 (None, None)

    生产 DOM（part_jglx.js 已确认）：
      · 弹窗内容渲染进 #jglxDiv，外层是 dialog.js 生成的 .modal，
        弹窗 id 记录在隐藏域 #jglxDialogId
      · 价格类型按钮：#partPriceType button[data-id='MS_RETAIL_PRICE']（选中态 class 含 active/btn-danger）
      · 确定按钮：#btn-submit　取消按钮：#btn-close
    """
    # ① 优先用 #jglxDialogId 精确拿弹窗
    for fr in page.frames:
        try:
            hid = fr.locator("#jglxDialogId")
            if await hid.count() == 0:
                continue
            did = (await hid.input_value() or "").strip()
            if not did:
                continue
            box = fr.locator(f"#{did}")
            if await box.count() > 0 and await box.is_visible():
                return fr, box
        except Exception:
            continue

    # ② 兜底：#jglxDiv 可见 → 取外层的 .modal
    for fr in page.frames:
        try:
            div = fr.locator("#jglxDiv:visible").first
            if await div.count() == 0:
                continue
            wrapper = fr.locator(".modal:visible").filter(has=fr.locator("#jglxDiv")).first
            if await wrapper.count() > 0:
                return fr, wrapper
            return fr, div
        except Exception:
            continue

    # ③ 最后兜底：按可见弹窗正文特征匹配
    sel = ".modal:visible, .el-dialog:visible, [role='dialog']:visible, .bootbox:visible"
    for fr in page.frames:
        try:
            boxes = fr.locator(sel)
            n = await boxes.count()
        except Exception:
            continue
        for i in range(n):
            box = boxes.nth(i)
            try:
                if not await box.is_visible():
                    continue
                txt = await box.inner_text() or ""
            except Exception:
                continue
            if "换件价格类型" in txt or ("价格类型" in txt and "#btn-submit"):
                return fr, box
    return None, None


async def _open_price_type_modal(page: Page) -> bool:
    """点零件表表头可点击文字『价格类型』→ 打开价格类型对话框

    生产实际表头：#partListTable th.view-priceSchemeCode > span.jglxColumn（文本 价格类型）
    点击后弹窗内容由 AJAX(/fixedloss/loss/part/part_jglx) 渲染，需要轮询等待。
    """
    sels = (
        "th .jglxColumn",
        ".jglxColumn",
        "#partListTable th span:has-text('价格类型')",
        "#evalPartDiv span:has-text('价格类型')",
        "span:has-text('价格类型')",
    )
    for sel in sels:
        for fr in page.frames:
            try:
                loc = fr.locator(sel)
                n = await loc.count()
            except Exception:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    if ((await el.inner_text()) or "").strip() != "价格类型":
                        continue      # 排除『价格方案』等相邻表头
                    try:
                        await el.click(timeout=4000)
                    except Exception:
                        await el.click(timeout=3000, force=True)
                except Exception:
                    continue
                # AJAX 渲染弹窗 → 最多等 8 秒
                for _ in range(16):
                    await asyncio.sleep(0.5)
                    mfr, modal = await _price_type_modal(page)
                    if modal is not None:
                        print(f"  [✓] 已打开『价格类型』对话框")
                        return True
    print("  [!] 未能打开『价格类型』对话框")
    return False


async def _pick_price_type(page: Page, code: str) -> bool:
    """在『价格类型』对话框里点选价格类型按钮

    生产实测（part_jglx.js）：
      按钮 = #partPriceType button[data-id='MS_RETAIL_PRICE']，
      点击后由 partPriceTypeSelect 加 class "btn-danger active"；
      确定 = #btn-submit（submitJglx 读 #partPriceTypeSelect .active 的 data-id 提交）。
    因此这里必须校验 active，点不上就不能点确定（否则等于没改）。
    """
    label = _PRICE_TYPE_LABEL.get(code, "")
    fr, modal = await _price_type_modal(page)
    if modal is None:
        print("  [!] 价格类型对话框不存在")
        return False

    def _scope_locators():
        out = []
        for scope in ("#partPriceType", "#partPriceTypeSelect", ".modal:visible"):
            try:
                out.append(fr.locator(f"{scope} button[data-id='{code}']").first)
            except Exception:
                pass
        try:
            out.append(modal.locator(f"button[data-id='{code}']").first)
        except Exception:
            pass
        return out

    # ① data-id 精确匹配（生产真实结构）
    for btn in _scope_locators():
        try:
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            try:
                await btn.click(timeout=3000)
            except Exception:
                await btn.click(timeout=2000, force=True)
            await asyncio.sleep(0.3)
            cls = (await btn.get_attribute("class")) or ""
            if "active" in cls:
                print(f"  [✓] 价格类型 → {label}（{code}，data-id 命中）")
                return True
            print(f"  [!] 点了 {label} 但按钮未变为选中态（class={cls[:60]}）")
        except Exception:
            continue

    # ② 兜底：按钮文本完全等于 S/O/A/R
    if label:
        for scope in ("#partPriceType", "#partPriceTypeSelect", "#jglxDiv"):
            try:
                btns = await fr.locator(f"{scope} button:visible").all()
            except Exception:
                btns = []
            for btn in btns:
                try:
                    if ((await btn.inner_text()) or "").strip() != label:
                        continue
                    await btn.click(timeout=3000)
                    await asyncio.sleep(0.3)
                    cls = (await btn.get_attribute("class")) or ""
                    if "active" in cls:
                        print(f"  [✓] 价格类型 → {label}（按文字命中）")
                        return True
                except Exception:
                    continue
    print(f"  [!] 价格类型对话框里没能选中『{label or code}』")
    return False


async def _confirm_price_type_modal(page: Page) -> bool:
    """价格类型对话框 → 点『确定』（生产 id: #btn-submit）并等弹窗关闭"""
    fr, modal = await _price_type_modal(page)
    if modal is None:
        return False
    clicked = False
    for getter in (lambda: modal.locator("#btn-submit").first,
                   lambda: fr.locator("#jglxDiv #btn-submit").first,
                   lambda: modal.locator("button:has-text('确定')").first):
        try:
            btn = getter()
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            await safe_click(btn, "价格类型对话框 → 确定")
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        print("  [!] 价格类型对话框点『确定』失败")
        return False
    # 等弹窗消失（提交接口 part_change_schemeCodeList 回调后 closeJglx）
    for _ in range(16):
        await asyncio.sleep(0.5)
        mfr, still = await _price_type_modal(page)
        if still is None:
            print("  [✓] 价格类型对话框已关闭")
            return True
    print("  [!] 价格类型对话框未关闭（可能提交失败）")
    return False


async def _switch_price_type(page: Page, code: str) -> bool:
    """完整一轮：打开价格类型对话框 → 选中 code → 确定"""
    if not await _open_price_type_modal(page):
        return False
    if not await _pick_price_type(page, code):
        print("  [!] 未能选中目标价格类型，弹窗保持原样，本轮作废")
        return False
    ok = await _confirm_price_type_modal(page)
    await asyncio.sleep(0.5)
    return ok


async def _row_price_type_labels(frame) -> list:
    """逐行回读**原始**价格类型（oriSchemeCode）

    关键：必须用 oriSchemeCode（原始类型），不能用 estiSchemeCode（当前类型）！
    否则先切到 R 之后再读就全是 R，会把 S/A 全部还原错。
    来源优先级：行内隐藏域 input[name='oriSchemeCode'] → data-lossobj.oriSchemeCode
    """
    rows = frame.locator("#partListTable tbody tr[data-lossobj]")
    try:
        n = await rows.count()
    except Exception:
        n = 0
    if n == 0:
        rows = frame.locator("tr[data-lossobj]")
        try:
            n = await rows.count()
        except Exception:
            return []
    labels = []
    for i in range(n):
        code = ""
        try:
            hid = rows.nth(i).locator("input[name='oriSchemeCode']").first
            if await hid.count() > 0:
                code = (await hid.input_value() or "").strip()
        except Exception:
            code = ""
        if not code:
            try:
                raw = await rows.nth(i).get_attribute("data-lossobj") or ""
            except Exception:
                raw = ""
            m = (re.search(r'"oriSchemeCode"\s*:\s*"([^"]+)"', raw)
                 or re.search(r'"estiSchemeCode"\s*:\s*"([^"]+)"', raw)
                 or re.search(r'"priceSchemeCode"\s*:\s*"([^"]+)"', raw))
            code = m.group(1) if m else ""
        labels.append(_PRICE_TYPE_LABEL.get(code, ""))
    return labels


async def _row_price_type_codes(frame) -> list:
    """逐行回读原始价格类型**代码**（oriSchemeCode，原样返回）"""
    rows = frame.locator("#partListTable tbody tr[data-lossobj]")
    try:
        n = await rows.count()
    except Exception:
        n = 0
    if n == 0:
        rows = frame.locator("tr[data-lossobj]")
        try:
            n = await rows.count()
        except Exception:
            return []
    codes = []
    for i in range(n):
        code = ""
        try:
            hid = rows.nth(i).locator("input[name='oriSchemeCode']").first
            if await hid.count() > 0:
                code = (await hid.input_value() or "").strip()
        except Exception:
            code = ""
        if not code:
            try:
                raw = await rows.nth(i).get_attribute("data-lossobj") or ""
            except Exception:
                raw = ""
            m = (re.search(r'"oriSchemeCode"\s*:\s*"([^"]+)"', raw)
                 or re.search(r'"estiSchemeCode"\s*:\s*"([^"]+)"', raw)
                 or re.search(r'"priceSchemeCode"\s*:\s*"([^"]+)"', raw))
            code = m.group(1) if m else ""
        codes.append(code)
    return codes


async def _check_rows_by_label(frame, labels: list, label: str) -> int:
    """只勾选「行值 == label」的行（返回勾选数量），用于混合类型分组还原"""
    rows = frame.locator("#partListTable tbody tr[data-lossobj]")
    try:
        n = await rows.count()
    except Exception:
        n = 0
    if n == 0:
        rows = frame.locator("tr[data-lossobj]")
    checked = 0
    for i, lab in enumerate(labels):
        try:
            row = rows.nth(i)
            cb = row.locator("input[type='checkbox']").first
            if await cb.count() == 0:
                continue
            want = (lab == label)
            if want:
                checked += 1
            cur = await cb.is_checked()
            if cur != want:
                try:
                    await cb.click(timeout=2500)
                except Exception:
                    await cb.click(timeout=2000, force=True)
            await asyncio.sleep(0.08)
        except Exception:
            continue
    return checked


async def _click_save_parts(page: Page) -> bool:
    """零件列表工具栏『保存』

    生产结构：<div class="table_screen table_screen_btn" id="partSaveBtn">保存</div>
    注意它是 div 而不是 button，所以按 id/class 定位。
    """
    frame = await _loss_table_frame(page)
    sels = (
        "#partSaveBtn",
        ".table_screen_btn:has-text('保存')",
        "button:has-text('保存')",
        "a:has-text('保存')",
        "input[value='保存']",
        ".btn:has-text('保存')",
    )
    for sel in sels:
        try:
            loc = frame.locator(sel)
            n = await loc.count()
        except Exception:
            continue
        for i in range(n):
            btn = loc.nth(i)
            try:
                if not await btn.is_visible():
                    continue
                txt = "".join(((await btn.inner_text()) or "").split())
            except Exception:
                continue
            if txt and txt != "保存":
                continue
            try:
                await btn.click(timeout=4000)
            except Exception:
                try:
                    await btn.click(timeout=3000, force=True)
                except Exception:
                    continue
            print(f"  [✓] 点击零件列表『保存』（{sel}）")
            log_audit("直供价取消 → 点击零件列表保存")
            await asyncio.sleep(2.0)
            return True
    print("  [!] 未找到零件列表『保存』按钮")
    return False


async def _fill_accept_system_pick(page: Page, text: str = "接受系统点选") -> bool:
    """往『提交』同一行的备注输入框填『接受系统点选』

    生产确认（index_init.js 的 #carSubmit 提交校验）：
      · 备注框选择器 = .manageRemark（input 或 textarea，提交前若零件存在
        [name=nonDirectType] 而该框为空 → 会弹「…请填写处理意见」类告警）
      · blur/change 会触发 manageRemarkSave 落库，所以填完必须失焦
    """
    # 生产实测结构（空白输入框，无 placeholder）：
    #   <input class="form-control width-200 display-inline-block manageRemark"
    #          id="estiRemarks" type="text" data-link="data.info.carVo.estiRemarks">
    for sel in ("input#estiRemarks",
                "input.manageRemark", "textarea.manageRemark"):
        for fr in page.frames:
            try:
                inp = fr.locator(sel).first
                if await inp.count() == 0 or not await inp.is_visible():
                    continue
                cur = ""
                try:
                    cur = (await inp.input_value() or "").strip()
                except Exception:
                    cur = ""
                if cur == text:
                    print(f"  [i] 处理意见已是『{text}』，跳过")
                    return True
                if cur:
                    print(f"  [i] 处理意见已有内容『{cur[:30]}』→ 追加『{text}』")
                    await inp.fill(f"{cur} {text}")
                else:
                    await inp.fill(text)
                # 该框用 data-link 绑定 Vue 模型，fill 后补发 input/change 确保落值
                try:
                    await inp.evaluate(
                        "el => { el.dispatchEvent(new Event('input', {bubbles: true}));"
                        " el.dispatchEvent(new Event('change', {bubbles: true})); }")
                except Exception:
                    pass
                try:
                    await inp.blur()          # 触发 .manageRemark 的 blur → manageRemarkSave
                except Exception:
                    pass
                await asyncio.sleep(0.6)
                print(f"  [✓] 提交行处理意见输入框 → {text}（{sel}）")
                log_audit(f"定损提交处理意见填写：{text}")
                return True
            except Exception:
                continue

    # 兜底：按 placeholder 找（个别环境标题文案不同）
    for sel in ("input[placeholder*='接受系统']", "textarea[placeholder*='接受系统']",
                "input[placeholder*='系统点选']", "textarea[placeholder*='系统点选']"):
        for fr in page.frames:
            try:
                inp = fr.locator(sel).first
                if await inp.count() == 0 or not await inp.is_visible():
                    continue
                await inp.fill(text)
                try:
                    await inp.blur()
                except Exception:
                    pass
                print(f"  [✓] 提交行输入框 → {text}（{sel}）")
                log_audit(f"定损提交备注填写：{text}")
                return True
            except Exception:
                continue

    # 最后兜底：提交按钮同行容器里的可编辑文本框
    kw = ("备注", "意见", "说明", "remark", "memo", "opinion")
    for fr in page.frames:
        try:
            btns = await fr.locator("button:visible, .btn:visible, div.save_btn:visible").all()
        except Exception:
            btns = []
        for btn in btns:
            try:
                t = "".join(((await btn.inner_text()) or "").split())
            except Exception:
                continue
            if t not in ("提交", "提交任务", "提交定损单"):
                continue
            for depth in (1, 2, 3):
                try:
                    box = btn.locator("xpath=" + "/".join([".."] * depth))
                    inputs = await box.locator("input:visible, textarea:visible").all()
                except Exception:
                    break
                cands = []
                for inp in inputs:
                    try:
                        typ = (await inp.get_attribute("type")) or "text"
                        if typ.lower() in ("checkbox", "radio", "hidden", "button",
                                           "submit", "file", "image"):
                            continue
                        if await inp.get_attribute("readonly") is not None:
                            continue
                        if await inp.is_disabled():
                            continue
                        meta = " ".join(str(await inp.get_attribute(k) or "")
                                        for k in ("id", "name", "placeholder", "class"))
                        cands.append((inp, meta))
                    except Exception:
                        continue
                if not cands:
                    continue
                hit = next((c for c in cands if any(k in c[1].lower() for k in kw)), None)
                if hit is None:
                    if len(cands) != 1:
                        print(f"  [!] 提交行有 {len(cands)} 个文本框，无法确定哪一个 → 跳过填写"
                              f"（候选: {[c[1].strip()[:60] for c in cands]}）")
                        return False
                    hit = cands[0]
                inp, meta = hit
                try:
                    await inp.fill(text)
                    try:
                        await inp.blur()
                    except Exception:
                        pass
                    print(f"  [✓] 提交行输入框 → {text}（命中: {meta.strip()[:80]}）")
                    log_audit(f"定损提交备注填写：{text} | 命中元素: {meta.strip()[:120]}")
                    return True
                except Exception:
                    continue
    print("  [!] 未找到提交行的处理意见输入框，请人工确认是否需要填写『接受系统点选』")
    return False


@register_step(desc="直供价非 0 → 切换价格类型取消直供价 + 保存 + 接受系统点选",
               outputs=["vehicle", "parts"])
async def step_loss_cancel_direct_supply(page: Page, context: dict, **kwargs) -> tuple:
    """把『有直供价』的案件转成正常案件

    流程：全选 → 价格类型→R→确定 → 价格类型→原类型(A/S)→确定 → 保存 →
          回读直供价确认已清空 → 提交行填『接受系统点选』→ 重新提取零件（价格已刷新）
    返回：重新提取后的 (vehicle, parts)
    """
    print(f"\n🔹 直供价取消流程（切换价格类型）")
    parts = kwargs.get("parts") or context.get("parts") or []
    frame = await _loss_table_frame(page)

    # ① 全选零件（同步定损时已全选，这里再确认一次）
    await _set_all_parts_checked(page, True)

    # ② 统计**原始**价格类型（oriSchemeCode，A/S/O/R），决定最后切回哪一种
    codes = await _row_price_type_codes(frame)
    if not codes:
        codes = [((getattr(p, "price_type_code", "") or getattr(p, "price_type", "")) or "").strip()
                 for p in parts]
    codes = [(c or "").strip() for c in codes]
    if not codes:
        raise RuntimeError("无法读取零件原始价格类型（oriSchemeCode 为空）→ 放弃取消直供价，避免把 S 误切成 A")
    missing = [c for c in codes if not c]
    if missing:
        raise RuntimeError(
            f"有 {len(missing)} 个零件读不到原始价格类型 → 放弃取消直供价，避免把 S 误切成 A")
    restore_codes = sorted(set(codes))
    labels_of = {c: _PRICE_TYPE_LABEL.get(c, c) for c in restore_codes}
    print(f"  [i] 原始价格类型分布: "
          + " ".join(f"{labels_of[c]}={codes.count(c)}" for c in restore_codes)
          + f" → 还原类型 {[labels_of[c] for c in restore_codes]}")

    # ③ 先全部切成 R（回收件价 → 直供价被清掉）
    if not await _switch_price_type(page, "MARKET_RECYCLE_PRICE"):
        raise RuntimeError("直供价取消失败：未能把价格类型切到『R』")
    print("  [✓] 第一轮切换完成：全部 → R")

    # ④ 切回原价格类型（原 A→A；原 S→S；混合时按原始代码分组分别切回）
    #    绝对不能用「当前类型」判断：切到 R 之后当前类型全是 R，
    #    必须用 oriSchemeCode（原始类型）还原，原 S 就必须还原成 S，绝不能变 A。
    if len(restore_codes) == 1:
        await _set_all_parts_checked(page, True)      # 确认选中状态还在
        code = restore_codes[0]
        if not await _switch_price_type(page, code):
            raise RuntimeError(f"直供价取消失败：未能把价格类型切回『{labels_of[code]}』")
        print(f"  [✓] 第二轮切换完成：全部 → {labels_of[code]}（{code}）")
    else:
        # 混合类型：按行分别勾选后再切（弹窗会按所选行的原始类型限制可选项）
        for code in restore_codes:
            await _set_all_parts_checked(page, False)
            cnt = await _check_rows_by_label(frame, codes, code)
            if cnt == 0:
                print(f"  [!] 未勾选到 {labels_of[code]} 类零件 → 跳过该组还原")
                continue
            print(f"  [i] 勾选 {labels_of[code]} 类零件 {cnt} 个 → 切回 {labels_of[code]}")
            if not await _switch_price_type(page, code):
                raise RuntimeError(
                    f"直供价取消失败：未能把 {labels_of[code]} 类零件切回原价格类型")
        await _set_all_parts_checked(page, True)
        print(f"  [✓] 混合价格类型已分别还原：{[labels_of[c] for c in restore_codes]}")

    # ⑤ 保存
    await _click_save_parts(page)
    await human_delay()

    # ⑥ 重新提取，检查直供价是否已清空
    profile_check = None
    try:
        profile_check = await step_loss_extract_data(page, context)
    except Exception as exc:
        print(f"  [!] 取消直供价后重新提取零件失败: {type(exc).__name__}: "
              f"{str(exc).splitlines()[0][:120]}")
    vehicle, parts = profile_check if profile_check else (None, parts)
    if parts:
        left = [p for p in parts if (p.direct_supply_price or 0) > 0]
        if left:
            print(f"  [!] 仍有 {len(left)} 个零件直供价非空: "
                  f"{[(p.code, p.direct_supply_price) for p in left][:5]}")
            log_audit(f"取消直供价后仍有直供价：{[(p.code, p.direct_supply_price) for p in left][:8]}")
        else:
            print("  [✓] 直供价已全部清空")
            log_audit("取消直供价成功：直供价已全部清空")
            context["skip_direct_supply"] = False
            # ★ 取消过直供价 → 换件金额与定损汇总不一致，报价完成时提交路径必须回『车辆定损』
            context["direct_supply_cancelled"] = True
            log_audit("已标记『取消过直供价』→ 报价完成提交路径选『车辆定损』")

    # ⑦ 提交行输入框填『接受系统点选』
    await _fill_accept_system_pick(page)

    if vehicle is None:
        vehicle = context.get("vehicle")
    context["vehicle"], context["parts"] = vehicle, parts
    return vehicle, parts


_JS_PART_NAME_COLORS = r"""
() => {
  const out = [];
  document.querySelectorAll('span.partName').forEach((el) => {
    const txt = (el.innerText || el.textContent || '').trim();
    if (!txt) return;
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (st.display === 'none' || st.visibility === 'hidden' ||
        parseFloat(st.opacity || '1') === 0 || r.width < 1 || r.height < 1) return;
    const deco = (st.textDecorationLine || st.textDecoration || '').toLowerCase();
    const underline = deco.includes('underline') ||
        (!!st.borderBottomStyle && st.borderBottomStyle !== 'none' &&
         parseFloat(st.borderBottomWidth || '0') > 0);
    out.push({name: txt, color: (st.color || '').trim(),
              partcolor: el.getAttribute('data-partcolor') || '',
              underline: underline});
  });
  return out;
}
"""


def _parse_rgb(color: str):
    """把 rgb/rgba/#hex 颜色解析为 (r,g,b)，失败返回 None"""
    c = (color or "").strip().lower()
    m = re.match(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", c)
    if m:
        return tuple(int(x) for x in m.groups())
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})([0-9a-f]{2})?", c)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return None


def _norm_color(color: str) -> str:
    rgb = _parse_rgb(color)
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})" if rgb else (color or "").strip().lower()


def _is_black_color(color: str) -> bool:
    """getComputedStyle 的颜色是否黑色（rgb(0,0,0) / rgba(0,0,0,x) / #000）"""
    rgb = _parse_rgb(color)
    return bool(rgb and all(x <= 16 for x in rgb))


def _is_blue_color(color: str) -> bool:
    """蓝色系：蓝色通道明显高于红/绿（如 rgb(28,97,252)）"""
    rgb = _parse_rgb(color)
    if not rgb:
        return False
    r, g, b = rgb
    return b >= 120 and b - max(r, g) >= 40


def _is_orange_color(color: str) -> bool:
    """橙色系：红 > 绿 > 蓝，且红/绿都较高（如 rgb(255,153,0)）"""
    rgb = _parse_rgb(color)
    if not rgb:
        return False
    r, g, b = rgb
    return r >= 180 and 70 <= g <= 210 and b <= 130 and r >= g and g >= b


def _part_name_should_skip(row: dict) -> tuple[bool, str]:
    """判断一个零件名称颜色是否需要人工处理

    规则：
      1) 橙色带下划线 → 视同黑色，正常处理
      2) 配置了精确跳过颜色 → 只按该清单跳过
      3) 未配置 → 内置启发式：蓝色、无下划线的橙色
    """
    color = str(row.get("color") or "")
    underline = bool(row.get("underline"))
    if underline and getattr(cfg, "PART_NAME_UNDERLINE_AS_BLACK", True):
        return False, "下划线视同黑色"
    if _is_black_color(color):
        return False, "黑色"

    configured = tuple(getattr(cfg, "PART_NAME_SKIP_COLORS", ()) or ())
    if configured:
        skip_set = {_norm_color(c) for c in configured}
        if _norm_color(color) in skip_set:
            return True, "配置颜色"
        return False, "未列入跳过颜色"

    if _is_blue_color(color):
        return True, "蓝色"
    if _is_orange_color(color):
        return True, "橙色无下划线"
    return False, "其他颜色"


async def _detect_non_black_part_names(scope, context: dict, prefer=None) -> None:
    """定损平台换件列表：任一『零件名称』字体颜色不是黑色 → 标记该报案号跳过自动处理

    scope 传 Page 时自动挑 frame：优先含本单报案号的，其次 prefer（已定位到的零件表 frame），
    避免残留的上一单文档误触发跳过。
    """
    frames = getattr(scope, "frames", None) or [scope]
    cur = (context.get("claim_no") or "").strip().upper()
    # 曾因蓝色/纯橙色退回过的报案号：二次进入时默认资料已补齐，颜色这一项视同正常
    color_returned_before = False
    if cur:
        try:
            from bot.pricing import get_price_service
            color_returned_before = get_price_service().claim_color_returned_before(cur)
        except Exception:
            color_returned_before = False
    cands = []
    for fr in frames:
        try:
            rows = await fr.evaluate(_JS_PART_NAME_COLORS)
        except Exception:
            continue
        if not rows:
            continue
        hit = False
        if cur:
            try:
                txt = (await fr.evaluate(
                    "() => document.body ? document.body.innerText : ''")) or ""
                hit = cur in txt.upper()
            except Exception:
                hit = False
        cands.append((rows, hit, fr is prefer))
    if not cands:
        return
    rows = next((r for r, hit, _ in cands if hit),
                next((r for r, _, pf in cands if pf), cands[0][0]))
    if not rows:
        return
    decisions = []
    for r in rows:
        skip, why = _part_name_should_skip(r)
        color = str(r.get("color") or "")
        if (skip and color_returned_before
                and (_is_blue_color(color) or _is_orange_color(color))):
            skip, why = False, "上次已退回，本次视同正常"
        decisions.append((r, skip, why))
    bad_rows = [(r, why) for r, skip, why in decisions if skip]
    bad = [r for r, _ in bad_rows]

    # 蓝色 / 纯橙色 → STEP 9 选业务动作『退回』；其他颜色（如配置的特殊颜色）仍按跳过处理
    blue_rows: list = []
    orange_rows: list = []
    other_rows: list = []
    for r in bad:
        color = str(r.get("color") or "")
        if _is_blue_color(color):
            blue_rows.append(r)
        elif _is_orange_color(color):
            orange_rows.append(r)
        else:
            other_rows.append(r)

    for key in ("skip_part_color", "skip_part_color_parts",
                "return_part_color", "return_blue_parts", "return_orange_parts"):
        context.pop(key, None)

    # 无论是否跳过都打印颜色分布，便于现场收集需要跳过的字体颜色代码
    dist: dict = {}
    for r, skip, why in decisions:
        key = f"{r.get('color') or '未知'}{'（下划线）' if r.get('underline') else ''}"
        item = dist.setdefault(key, {"n": 0, "label": "跳过" if skip else "正常",
                                     "why": why})
        item["n"] += 1
    summary = "；".join(
        f"{k}×{v['n']}→{v['label']}({v['why']})"
        for k, v in sorted(dist.items(), key=lambda kv: -kv[1]["n"]))
    print(f"  [i] 零件名称颜色分布（{len(rows)} 个）：{summary}")
    log_audit(f"零件名称颜色分布：{summary}")

    if color_returned_before:
        print("  [i] 该报案号曾因蓝色/纯橙色退回 → 本次零件名称颜色视同正常，"
              "按一般案件处理")
        log_audit(f"报案号={cur} 曾因颜色退回 → 二次进入视同正常")

    if not bad:
        print(f"  [i] 零件名称颜色检查：{len(rows)} 个均无需跳过 → 正常自动处理")
        return
    if other_rows:
        context["skip_part_color"] = True
    if blue_rows or orange_rows:
        context["return_part_color"] = True
        context["return_blue_parts"] = blue_rows[:10]
        context["return_orange_parts"] = orange_rows[:10]
    context["skip_part_color_parts"] = bad[:10]
    sample = "、".join(
        f"{r.get('name')}（{r.get('color')}"
        f"{'，下划线' if r.get('underline') else ''}，{why}）"
        for r, why in bad_rows[:5])
    action = ("STEP 9 选业务动作『退回』"
              if context.get("return_part_color") else "STEP 9 前跳过『报价完成』")
    print(f"\n  ⏭ 检测到 {len(bad)}/{len(rows)} 个零件名称颜色需人工处理"
          f" → 正常执行到 STEP 8，{action}")
    if context.get("return_part_color"):
        print(f"     蓝色 {len(blue_rows)} 个 / 纯橙色 {len(orange_rows)} 个 → 退回并填写意见")
    if other_rows:
        print(f"     其他颜色 {len(other_rows)} 个 → STEP 9 前跳过")
    print(f"      {sample}")
    log_audit(f"零件名称颜色需人工处理 {len(bad)}/{len(rows)} → {action}：{sample}")


@register_step(desc="定损平台提取 VIN + 所有换件信息", outputs=["vehicle", "parts"])
async def step_loss_extract_data(page: Page, context: dict, **kwargs) -> tuple:
    print(f"\n🔹 STEP 4: 定损平台 → 提取 VIN 和换件列表")
    # 同步定损 confirm 之后定损平台可能 SPA 重渲染：等 networkidle + 额外稳定期
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(1.5)  # SPA 渲染缓冲
    await human_delay()

    # ---- 逐个尝试 VIN 选择器（不用逗号组合，避免第一个失败时 Playwright 报错） ----
    # 生产 VIN 可能用 #loss-vin（mock 用的）、[id*=vin]、或 .vin-value 等不同命名；
    # 生产实测 VIN 元素没有任何 vin 相关 id/class，只靠「定损VIN」标签的邻域 → 走 JS 兜底
    VIN_CANDIDATES = [
        "#loss-vin",
        "[data-label='vin']", "[data-label='VIN']", "[data-label*='vin' i]",
        "[data-label*='车架']",
        "[data-field='vin']", "[data-field='VIN']",
        "[id*='vin']", "[id*='Vin']",
        "[class*='vin']", "[class*='Vin']",
        ".vin-value", "#tb-vin", "span.vin", "div.vin",
        # 通用兜底：包含 VIN 字样的 label 旁边的值
        "xpath=//*[contains(text(),'定损VIN')]/following-sibling::*",
        "xpath=//*[contains(text(),'定损VIN')]/parent::*/following-sibling::*",
    ]
    vin = ""
    tried = []
    for sel in VIN_CANDIDATES:
        try:
            fr, loc = await _frame_locator(page, sel)
            if await loc.count() > 0 and await loc.is_visible():
                txt = (await loc.text_content() or "").strip()
                vin = _clean_vin(txt)
                if vin:
                    print(f"  [✓] VIN 命中选择器 {sel}（frame={(fr.url or 'main')[:50]}）: {vin}")
                    break
                tried.append(f"{sel}→'{txt[:30]}'")
            else:
                tried.append(f"{sel}→(不可见)")
        except Exception:
            tried.append(f"{sel}→(异常)")

    # ---- 选择器全部未命中 → JS 按「定损VIN」标签邻域取（生产实测路径） ----
    if not vin:
        for fr in page.frames:
            try:
                info = await fr.evaluate(_JS_FIND_VIN)
            except Exception:
                continue
            if info and info.get("vin"):
                vin = _clean_vin(info["vin"]) or info["vin"]
                if vin:
                    print(f"  [✓] VIN JS 兜底命中（{info.get('how')}，"
                          f"frame={(fr.url or 'main')[:50]}）: {vin}")
                    log_audit(f"VIN JS 兜底命中: {vin} ({info.get('how')})")
                    break

    if not vin:
        # 打印诊断：所有 frame 的 URL + 页面文本片段
        print(f"  [!] 所有 VIN 选择器与 JS 兜底均未命中，以下是诊断信息：")
        for fr in page.frames:
            try:
                ftxt = (await fr.inner_text("body") or "")[:300].replace("\n", " ")
            except Exception:
                ftxt = "<读取失败>"
            print(f"     frame: {(fr.url or 'main')[:80]} | body前300字: {ftxt}")
        raise RuntimeError(
            f"定损平台提取 VIN 失败，所有候选选择器与 JS 兜底均未命中。"
            f"已尝试: {'; '.join(tried)}。"
            f"请把上面的 frame 诊断信息发给我补规则。"
        )

    # ---- 报案号：mock 用 #tb-claim，生产没有 → 回退上下文 / 页面文本 ----
    claim_no = ""
    try:
        _, claim_el = await _frame_locator(page, "#tb-claim")
        if await claim_el.count() > 0:
            claim_no = (await claim_el.text_content() or "").strip()
    except Exception:
        claim_no = ""
    if not claim_no:
        claim_no = (context.get("claim_no") or "").strip()
    if not claim_no:
        for fr in page.frames:
            try:
                ftxt = await fr.inner_text("body")
            except Exception:
                continue
            m = CLAIM_NO_RE.search(ftxt or "")
            if m:
                claim_no = m.group(0).strip()
                break
    if not claim_no:
        raise RuntimeError("定损平台提取报案号失败（#tb-claim / 上下文 / 页面文本均未命中）")
    if claim_no != (context.get("claim_no") or "").strip() and context.get("claim_no"):
        print(f"  [i] 页面报案号 {claim_no} 与上下文 {context.get('claim_no')} 不一致，以页面为准")

    vehicle = VehicleInfo(vin=vin, claim_no=claim_no)
    print(f"  [✓] 提取 VIN = {vin}")
    print(f"  [✓] 提取报案号 = {claim_no}")

    parts: List[PartItem] = []

    # 换件列表可能完全不存在（无换件案件）
    if await _count_loss_parts(page) == 0:
        print(f"  [i] 换件列表为空 → 该案件无需换件，跳过零件提取")
        context["has_parts"] = False
        return vehicle, parts

    context["has_parts"] = True

    # ===== 方案 A：从 data-lossobj JSON 属性提取（首选，最可靠） =====
    # 生产定损平台的每个 <tr> 上都有 data-lossobj="{...}" 属性，
    # 是完整的零件 JSON，绕开 bootstrap-table td 里 tooltip 文本污染的问题
    _JS_EXTRACT_LOSSOBJ = r"""
    () => {
      const results = [];
      // HTML decode 函数（&quot; -> " 等）
      const decode = (s) => {
        if (!s) return s;
        const txt = document.createElement('textarea');
        txt.innerHTML = s;
        return txt.value;
      };
      const scanRows = (doc, depth) => {
        // 找所有带 data-lossobj 的 tr（主文档和所有 iframe 递归）
        const iframes = doc ? doc.querySelectorAll('iframe') : [];
        for (const iframe of iframes) {
          try { scanRows(iframe.contentDocument, depth + 1); } catch(e) {}
        }
        const norm = (s) => (s || '').replace(/\s+/g, '');
        // 同一张表的「直供价」列下标（只认独立的直供价列，排除「非直供价」）
        const dsIdxOf = (table) => {
          if (table.__botDsIdx !== undefined) return table.__botDsIdx;
          let ths = Array.from(table.querySelectorAll('thead th'));
          if (!ths.length) {
            const box = table.closest('.bootstrap-table') || table.parentElement;
            if (box) ths = Array.from(box.querySelectorAll('thead th, .fixed-table-header th'));
          }
          let idx = -1;
          for (let i = 0; i < ths.length; i++) {
            const t = norm(ths[i].innerText || ths[i].textContent);
            if (t.indexOf('非直供') >= 0) continue;
            if (t === '直供价' || t === '直供单价' || t === '直供价格') { idx = i; break; }
          }
          table.__botDsIdx = idx;
          return idx;
        };
        const trs = doc ? doc.querySelectorAll('tr[data-lossobj]') : [];
        for (const tr of trs) {
          const raw = tr.getAttribute('data-lossobj');
          if (!raw) continue;
          try {
            const jsonStr = decode(raw);
            const obj = JSON.parse(jsonStr);
            // 直供价：和 编码/名称/本地价 同一行、同一张表的那个单元格
            let dsRaw = '', dsCol = false;
            try {
              const table = tr.closest('table');
              if (table) {
                const i2 = dsIdxOf(table);
                dsCol = i2 >= 0;
                if (dsCol && tr.children && tr.children.length > i2) {
                  const td = tr.children[i2];
                  dsRaw = (td.innerText || td.textContent || '').trim();
                }
              }
            } catch(e) {}
            obj.__directSupplyCol = dsCol;
            obj.__directSupplyRaw = dsRaw;
            results.push(obj);
          } catch(e) {
            results.push({__parseError: String(e.message || e)});
          }
        }
      };
      scanRows(document, 0);
      return results;
    }
    """
    data_lossobj_parts = []
    _cands: list = []
    for fr in page.frames:
        try:
            rows = await fr.evaluate(_JS_EXTRACT_LOSSOBJ)
            if rows and len(rows) > 0:
                _cands.append((fr, rows))
        except Exception as exc:
            print(f"  [diag] data-lossobj frame={(fr.url or 'main')[:50]} 异常: {type(exc).__name__}")

    # 只在「本单」的 frame 里取零件表：残留的上一单文档里同样有 tr[data-lossobj]，
    # 按 frame 顺序取第一个会把上一单的零件/直供价当成本单 → 后续整批报案号误跳过
    _cur_claim = (context.get("claim_no") or "").strip().upper()
    _picked = None
    _stale: list = []
    for _idx, (_fr, _rows) in enumerate(_cands):
        _nums: set = set()
        try:
            _txt = await _fr.evaluate(
                "() => (document.body ? document.body.innerText : '')")
            _nums = {m.group(0).upper() for m in CLAIM_NO_RE.finditer(_txt or "")}
        except Exception:
            pass
        if _cur_claim and _cur_claim in _nums:
            _picked = (_fr, _rows)
            print(f"  [i] 采用 frame#{_idx}（文档含本单报案号 {_cur_claim}，{len(_rows)} 条零件）")
            break
        if _nums:
            _stale.append((_idx, sorted(_nums)[:2]))
            continue
        if _picked is None:      # 读不到报案号 → 先记下，继续找更确定的
            _picked = (_fr, _rows)

    if _picked is None and _stale:
        raise RuntimeError(
            f"定损平台只找到其它报案号的零件表 {_stale}，本单 {_cur_claim} 的零件表未加载"
            f"（疑似复用/残留了上一单定损页）→ 拒绝按残留数据判断直供价")
    if _picked is not None:
        data_lossobj_parts = _picked[1]
        print(f"  [diag] data-lossobj 方案 → frame={(_picked[0].url or 'main')[:50]} "
              f"→ 拿到 {len(data_lossobj_parts)} 条 JSON")

    # ★ 换件列表『零件名称』字体颜色非黑色 → 标记跳过
    #   （独立于 data-lossobj 是否解析成功，直接看 span.partName 的 computed color）
    await _detect_non_black_part_names(page, context,
                                       prefer=(_picked[0] if _picked else None))

    if data_lossobj_parts and len(data_lossobj_parts) > 0 and not data_lossobj_parts[0].get("__parseError"):
        print(f"  [i] 使用 data-lossobj JSON 方案提取零件 ({len(data_lossobj_parts)} 条)")
        print(f"  [diag] data-lossobj 字段: {list(data_lossobj_parts[0].keys())}")
        has_direct_supply = False
        has_ds_col = False
        # 延迟导入避免循环依赖
        from bot.models import SCHEME_DISPLAY

        for raw_obj in data_lossobj_parts:
            try:
                # data-lossobj 字段 → PartItem 字段映射
                idx = int(raw_obj.get("serialNo") or raw_obj.get("row_num") or 0)
                code = str(raw_obj.get("factPartCode") or raw_obj.get("factPartShortCode") or "").strip()
                name = str(raw_obj.get("partName") or raw_obj.get("piccPartName") or "").strip()
                qty = int(raw_obj.get("evalPartAmount") or raw_obj.get("estiPartAmount") or 1)
                # 价格方案代码：优先取 estiSchemeCode（报价场景用的），兜底 priceSchemeCode
                scheme_code = str(raw_obj.get("estiSchemeCode")
                                  or raw_obj.get("apprSchemeCode")
                                  or raw_obj.get("priceSchemeCode") or "").strip()
                # 本地价：estiLocalPrice（定损平台给出，A类方案下是打折后的价格）
                local_price = float(raw_obj.get("estiLocalPrice") or raw_obj.get("evalLocalPrice") or 0)
                # 定损价 = 平台「报价」列当前值 estiPartPrice
                list_price = float(raw_obj.get("estiPartPrice") or raw_obj.get("evalPartPrice")
                                   or raw_obj.get("guidePrice") or raw_obj.get("msRetailPrice") or 0)
                # 市场指导价（仅记录备查）
                guide_price = float(raw_obj.get("guidePrice") or raw_obj.get("msRetailPrice") or 0)
                # ===== S价格值：msRetailPrice（4S零售价，比价统一基准） =====
                # 优先 msRetailPrice，兜底 fourLocalPrice 管道符分隔的第一个值
                ms_retail = raw_obj.get("msRetailPrice") or 0
                if not ms_retail:
                    four_str = str(raw_obj.get("fourLocalPrice") or "")
                    if four_str:
                        try:
                            ms_retail = float(four_str.split("|")[0])
                        except (ValueError, IndexError):
                            ms_retail = 0
                s_price_value = float(ms_retail) if ms_retail else None
                # 人保零件编码
                picc_code = str(raw_obj.get("piccPartCode") or "").strip()
                # 直供价
                ds_raw = str(raw_obj.get("__directSupplyRaw") or "").strip()
                ds_price = _parse_market_price_cell(ds_raw) if ds_raw else 0.0
                ds_col_found = bool(raw_obj.get("__directSupplyCol"))

                # 排除"没有数据..."占位行
                if code == "" and name in ("没有数据...", "暂无数据", ""):
                    continue

                parts.append(PartItem(
                    index=idx, code=code, name=name, qty=qty,
                    price_type=scheme_code, local_price=local_price,
                    list_price=list_price if list_price > 0 else None,
                    guide_price=guide_price if guide_price > 0 else None,
                    direct_supply_price=ds_price if (ds_price or 0) > 0 else None,
                    # ===== 新增字段 =====
                    price_scheme=SCHEME_DISPLAY.get(scheme_code, scheme_code),
                    price_type_code=scheme_code,
                    s_price_value=s_price_value,
                ))
                if ds_col_found:
                    has_ds_col = True
                # 人保零件编码塞到 context（后续一车一件查询可能用到）
                if picc_code:
                    context.setdefault("_picc_codes", {})[code] = picc_code
            except Exception as exc:
                print(f"  [!] data-lossobj 解析零件失败: {exc} | raw keys={list(raw_obj.keys())[:8]}")
                continue

        # 直供判定诊断（真实运行时可据此核对字段含义）
        _f0 = data_lossobj_parts[0]
        print(f"  [diag] 直供判定: directSupplyFlag={_f0.get('directSupplyFlag')!r} "
              f"platDirectPrice={_f0.get('platDirectPrice')} pzhigongPrice={_f0.get('pzhigongPrice')} "
              f"nonDirectPrice={_f0.get('nonDirectPrice')}")
        print(f"  [✓] data-lossobj 提取换件 {len(parts)} 个:")
        for p in parts:
            scheme_tag = f"{p.price_scheme}({p.price_type_code})" if p.price_type_code else "-"
            s_tag = f"¥{p.s_price_value}" if p.s_price_value else "—"
            print(f"      #{p.index:02d} {p.code[:20]:<20} {p.name[:16]:<16} "
                  f"方案={scheme_tag:<22} 本地价=¥{p.local_price} "
                  f"S价={s_tag} 定损价(报价)=¥{p.list_price} 指导价=¥{p.guide_price} "
                  f"直供价={('¥%s' % p.direct_supply_price) if p.direct_supply_price else '—'}")
        # 直供价只认「本单零件表」里同一行的那一列，不再跨 frame 扫（避免读到上一单残留的表格）
        ds_vals = [p.direct_supply_price for p in parts if (p.direct_supply_price or 0) > 0]
        has_direct_supply = bool(ds_vals)
        if has_ds_col:
            print(f"  [i] 「直供价」列(同行): "
                  + (f"有数值 {ds_vals[:5]} → 本单跳过"
                     if ds_vals else "整列为空 → 本单正常比价"))
        else:
            print("  [i] 零件表无「直供价」列 → 本单正常比价")
        _mark_direct_supply_skip(context, has_direct_supply)
        return vehicle, parts

    # ===== 方案 B：按表头列名 + td 文本（兜底，mock 环境用） =====
    print(f"  [diag] data-lossobj 方案未取到数据 → 回退到表头列映射方案")

    tbody_selectors = [
        "#partListTable tbody",
        ".bootstrap-table .fixed-table-body .table tbody",
        ".bootstrap-table .table tbody",
        "#evalPartDiv table tbody",
        ".parts-table tbody",
        "#parts-tbody",
    ]
    tbody = None
    for sel in tbody_selectors:
        try:
            fr, loc = await _frame_locator(page, sel)
            if await loc.count() > 0 and await loc.locator("tr").count() > 0:
                tbody = loc
                print(f"  [i] 换件表读取命中 {sel}")
                break
        except Exception:
            continue
    if tbody is None:
        print(f"  [!] 换件表：读取时所有选择器均未命中，跳过零件提取")
        return vehicle, parts

    # 一次性读回「表头 + 所有数据行所有单元格」（纯 JS）
    # 生产实测：bootstrap-table 里逐格 Playwright 读取会在打印完表头后长时间卡住
    # （locator.get_attribute / text_content 对不存在的元素各等 30s，10 行就是几分钟）
    _JS_READ_PARTS_TABLE = r"""
    (tbody) => {
      const clean = (s) => (s || '')
        .replace(/\u00a0/g, ' ')
        .replace(/[\r\n\t]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      const table = tbody.closest('table');
      let headers = [];
      if (table) {
        const thead = table.querySelector('thead tr');
        if (thead) headers = Array.from(thead.querySelectorAll('th,td')).map(th => clean(th.textContent));
      }
      // 生产 td 里混有 tooltip/提示类后代（如「配件【后转向节（右）】可以重复使用…」），
      // 取文本前先剔除这些节点，避免污染零件名称/编码
      const TIP_SEL = '[class*="tooltip"],[class*="popover"],[class*="tip-"],[class*="-tip"],[class*="tips"],[class*="help-"],[class*="hint"],[class*="notice"]';
      const rows = Array.from(tbody.querySelectorAll('tr')).map(tr => {
        const codeEl = tr.querySelector('.part-code');
        const codeAttr = codeEl ? (codeEl.getAttribute('data-code') || '') : '';
        const cells = Array.from(tr.querySelectorAll('td')).map(td => {
          const clone = td.cloneNode(true);
          try { clone.querySelectorAll(TIP_SEL).forEach(n => n.remove()); } catch (e) {}
          return clean(clone.textContent);
        });
        return {cells, codeAttr};
      });
      return {headers, rows};
    }
    """

    table_data = {"headers": [], "rows": []}
    try:
        table_data = await tbody.first.evaluate(_JS_READ_PARTS_TABLE) or table_data
    except Exception as exc:
        print(f"  [!] 换件表整表读取失败：{exc}")

    headers = [str(h or "") for h in (table_data.get("headers") or [])]
    raw_rows = table_data.get("rows") or []
    if headers:
        print(f"  [i] 换件表表头 ({len(headers)} 列): {headers}")
    print(f"  [i] 换件表数据行：{len(raw_rows)} 条")

    col_map: dict = {}
    for i, h_norm in enumerate(headers):
        if not col_map.get("idx") and ("序号" in h_norm or h_norm.isdigit()):
            col_map["idx"] = i
        if not col_map.get("code") and ("编码" in h_norm or "OE" in h_norm.upper()):
            col_map["code"] = i
        if not col_map.get("name") and ("名称" in h_norm or
              ("零件" in h_norm and "编码" not in h_norm and "价格" not in h_norm and "方案" not in h_norm)):
            col_map["name"] = i
        if not col_map.get("qty") and ("数量" in h_norm):
            col_map["qty"] = i
        if not col_map.get("price_type") and ("价格类型" in h_norm): col_map["price_type"] = i
        if not col_map.get("price_type") and ("价格" in h_norm and "方案" in h_norm): col_map["price_type"] = i
        if not col_map.get("local_price") and "本地价" in h_norm:
            col_map["local_price"] = i
        if not col_map.get("direct_supply") and "直供" in h_norm and "非直供" not in h_norm:
            # 只认独立的「直供价」列，避免「直供状态 / 直供商」误命中
            _h = re.sub(r"[（(].*?[)）]", "", h_norm).replace("¥", "").replace("￥", "").replace("元", "").strip()
            if _h in ("直供价", "直供单价", "直供价格"):
                col_map["direct_supply"] = i
        if not col_map.get("list_price") and any(k in h_norm for k in ("市场价", "参考价", "列表价")):
            col_map["list_price"] = i
    if headers:
        print(f"  [i] 换件表列映射: {col_map}")

    if not col_map:
        col_map = {"idx": 1, "code": 2, "name": 3, "qty": 4,
                   "price_type": 5, "local_price": 6, "list_price": 7}
        print(f"  [i] 使用默认列映射（mock 结构）: {col_map}")

    def _num(text: str) -> float:
        m = re.search(r"([\d.]+)", (text or "").replace(",", "").replace("¥", "").replace("￥", ""))
        return float(m.group(1)) if m else 0.0

    has_direct_supply = False
    for i, raw_row in enumerate(raw_rows):
        try:
            cells = [str(c or "") for c in (raw_row.get("cells") or [])]
            if not cells:
                continue

            def _cell(key: str, default: str = "") -> str:
                pos = col_map.get(key, -1)
                if pos < 0 or pos >= len(cells):
                    return default
                return cells[pos]

            idx_match = re.search(r"(\d+)", _cell("idx", "0") or "0")
            idx = int(idx_match.group(1)) if idx_match else 0

            name = _cell("name")
            code = (raw_row.get("codeAttr") or "").strip() or _cell("code")

            qty_match = re.search(r"(\d+)", _cell("qty", "1") or "1")
            qty = int(qty_match.group(1)) if qty_match else 1

            price_type = _cell("price_type")
            local_price = _num(_cell("local_price", "0"))
            list_price = _num(_cell("list_price", "0"))

            # 直供价：与编码/名称/本地价同一行的「直供价」列
            ds_price = 0.0
            if "direct_supply" in col_map:
                ds_price = _num(_cell("direct_supply", "0"))
                if ds_price > 0:
                    has_direct_supply = True
                    print(f"      💰 直供价有值: #{idx:02d} {code} {name} 直供价=¥{ds_price}")

            if idx == 0 and code == "" and name in ("没有数据...", "暂无数据", ""):
                continue

            parts.append(PartItem(
                index=idx, code=code, name=name, qty=qty,
                price_type=price_type, local_price=local_price,
                list_price=list_price if list_price > 0 else None,
                guide_price=None,   # 方案 B 无 guidePrice 数据（mock 环境）
                direct_supply_price=ds_price if ds_price > 0 else None,
            ))
        except Exception as exc:
            print(f"  [!] 解析第 {i} 行零件失败：{exc}")
            continue

    print(f"  [✓] 提取换件 {len(parts)} 个:")
    for p in parts:
        print(f"      #{p.index:02d} {p.code[:20]:<20} {p.name[:16]:<16} 本地价=¥{p.local_price} "
              f"直供价={('¥%s' % p.direct_supply_price) if p.direct_supply_price else '—'}")

    _mark_direct_supply_skip(context, has_direct_supply)
    return vehicle, parts


# ============================================================
# 一车一件（外网）反爬保护 —— 速率限制 + 封禁检测 + 人类化随机化
# ============================================================
# 全局时间戳（线程安全）
_last_market_vin_time = 0.0
_last_market_part_time = 0.0
_market_lock = _th.Lock()
# 封禁冷却（until 时间戳，0 = 无封禁）
_market_ban_until = 0.0
_market_ban_lock = _th.Lock()
# 滑动窗口配额：记录每次查询（VIN / 零件）的时间戳，防止"单次间隔够但累计过密"
_market_query_times: list = []
# 连续 VIN 查询无结果的次数（连续 2 次视为疑似封禁）
_market_vin_fail_streak = 0


def _market_quota_wait() -> float:
    """滑动窗口配额：返回需要等待的秒数（0 = 立即可查），并登记本次查询时间"""
    global _market_query_times
    now = time.monotonic()
    with _market_lock:
        _market_query_times = [t for t in _market_query_times
                               if now - t < cfg.MARKET_QUOTA_WINDOW_LONG]
        recent10 = [t for t in _market_query_times
                    if now - t < cfg.MARKET_QUOTA_WINDOW_SHORT]
        wait = 0.0
        if len(recent10) >= cfg.MARKET_QUOTA_MAX_SHORT:
            # 等到最早那次滑出 10 分钟窗口
            wait = max(wait, recent10[0] + cfg.MARKET_QUOTA_WINDOW_SHORT - now + 0.5)
        if len(_market_query_times) >= cfg.MARKET_QUOTA_MAX_LONG:
            wait = max(wait, _market_query_times[0] + cfg.MARKET_QUOTA_WINDOW_LONG - now + 0.5)
        return max(0.0, wait)


def market_ban_remaining() -> float:
    """当前封禁冷却剩余秒数（0 = 未被封）"""
    with _market_ban_lock:
        return max(0.0, _market_ban_until - time.monotonic())


def market_set_ban(seconds: float, reason: str = "") -> None:
    """手动置入封禁冷却（连续失败自保用）"""
    global _market_ban_until
    with _market_ban_lock:
        _market_ban_until = time.monotonic() + seconds
    if reason:
        print(f"\n  🛑 {reason} → 一车一件暂停 {seconds/60:.0f} 分钟")
        log_audit(f"{reason} → 暂停一车一件 {int(seconds)}s")


def _market_quota_mark() -> None:
    with _market_lock:
        _market_query_times.append(time.monotonic())
        del _market_query_times[:-200]


async def _market_rate_wait(kind: str, rate_enabled: bool = True,
                            breaker_enabled: bool = True) -> None:
    """在一车一件上做动作前强制等够最小间隔，防反爬。

    rate_enabled    —— 速率限流开关（VIN/零件间隔 + 人类抖动）
    breaker_enabled —— 封禁熔断开关（封禁冷却等待）
    """
    global _last_market_vin_time, _last_market_part_time, _market_ban_until

    # ① 先检查封禁冷却（熔断开关控制）
    #    封禁期绝不发请求，也不原地睡 30 分钟把整轮挂机阻塞 → 抛给上层，本单降级为定损价
    if breaker_enabled:
        remain = market_ban_remaining()
        if remain > 0:
            raise RuntimeError(
                f"一车一件触发反爬封禁（冷却剩余 {int(remain/60)} 分钟），已暂停一车一件查询")

    if not rate_enabled:
        return

    # ② 滑动窗口配额（累计量控制）——站点风控看的是单位时间总次数
    quota_wait = _market_quota_wait()
    if quota_wait > 0:
        print(f"\n  ⏳ 一车一件查询配额已满（10分钟≤{cfg.MARKET_QUOTA_MAX_SHORT}次 / "
              f"60分钟≤{cfg.MARKET_QUOTA_MAX_LONG}次）→ 等 {quota_wait/60:.1f} 分钟再查")
        log_audit(f"一车一件滑动窗口配额已满 → 等待 {int(quota_wait)}s")
        await asyncio.sleep(quota_wait)
    _market_quota_mark()

    # ③ 速率限制
    if kind == "vin":
        mn = cfg.MARKET_VIN_DELAY_MIN
        mx = cfg.MARKET_VIN_DELAY_MAX
        with _market_lock:
            elapsed = time.monotonic() - _last_market_vin_time
        if elapsed < mn:
            wait = random.uniform(mn - elapsed, mx - elapsed)
            wait = max(0.5, wait)
            print(f"  🕒 一车一件 VIN 限流 → 等 {wait:.1f}s（距上次 {elapsed:.1f}s）")
            await asyncio.sleep(wait)
        with _market_lock:
            _last_market_vin_time = time.monotonic()

    elif kind == "part":
        mn = cfg.MARKET_PART_DELAY_MIN
        mx = cfg.MARKET_PART_DELAY_MAX
        with _market_lock:
            elapsed = time.monotonic() - _last_market_part_time
        if elapsed < mn:
            wait = random.uniform(mn - elapsed, mx - elapsed)
            wait = max(0.3, wait)
            print(f"  🕒 一车一件零件限流 → 等 {wait:.1f}s（距上次 {elapsed:.1f}s）")
            await asyncio.sleep(wait)
        with _market_lock:
            _last_market_part_time = time.monotonic()

    # ③ 动作前再补一段小抖动（200~900ms），模拟人类停顿
    await asyncio.sleep(random.uniform(0.2, 0.9))


_BAN_STRICT_KWS = ("429", "too many requests", "rate limit")
# 数字/英文指纹必须带上下文才算数，否则页面里随便一个 "429" 都会误触发 16 分钟熔断
_BAN_STRICT_CONTEXT = (
    "请求", "频繁", "错误", "禁止", "访问", "超出", "限制", "异常",
    "too many", "limited", "forbidden", "error", "blocked", "exceeded",
)


# ---------- 网络层封禁指纹：真实的 HTTP 429 ----------
# 页面文字可能是"正常 DOM"，但接口已经返回 429 —— 这是最准确的限流信号
_market_http_bans: list = []          # [(monotonic_ts, status, url)]
_market_http_lock = _th.Lock()
_HTTP_BAN_STATUS = (429,)
_SKIP_RES_TYPES = {"image", "stylesheet", "font", "media", "script"}


def market_note_response(status: int, url: str, resource_type: str = "") -> None:
    """记录一次 HTTP 429（跳过图片/样式/脚本等静态资源）"""
    try:
        if int(status) not in _HTTP_BAN_STATUS:
            return
        if (resource_type or "").lower() in _SKIP_RES_TYPES:
            return
        with _market_http_lock:
            _market_http_bans.append((time.monotonic(), int(status), url or ""))
            del _market_http_bans[:-20]
    except Exception:
        pass


def market_recent_http_ban(window: float = 60.0) -> tuple:
    """最近 window 秒内是否出现过 429，返回 (status, url)，没有则 (0, '')"""
    now = time.monotonic()
    with _market_http_lock:
        for ts, status, url in reversed(_market_http_bans):
            if now - ts <= window:
                return status, url
    return 0, ""


def market_clear_http_ban() -> None:
    with _market_http_lock:
        _market_http_bans.clear()


def _on_market_response(resp) -> None:
    """Playwright response 回调（同步）：只做记录，不做 IO"""
    try:
        try:
            rtype = resp.request.resource_type
        except Exception:
            rtype = ""
        market_note_response(resp.status, resp.url, rtype)
    except Exception:
        pass


def install_market_ban_listener(page: Page) -> None:
    """给一车一件页面挂上网络响应监听（真实的 429 判定）"""
    try:
        page.on("response", _on_market_response)
        print("  🌐 已挂载一车一件网络监听（HTTP 429 → 熔断）")
    except Exception as exc:
        print(f"  [!] 网络监听挂载失败：{type(exc).__name__}")


def _market_ban_hit(body_text: str) -> tuple:
    """在本页文本里找封禁/反爬关键词，返回 (命中词, 命中上下文)，空串=没命中

    中文关键词（防爬/禁用/验证码…）出现即命中；
    '429' / 'rate limit' 这类指纹必须带“请求频繁/错误/限制”等上下文才算，
    避免页面里任意一个 429 数字造成误熔断。
    """
    if not body_text:
        return "", ""
    low = body_text.lower()
    for kw in cfg.MARKET_BAN_KEYWORDS:
        kl = kw.lower()
        idx = low.find(kl)
        if idx < 0:
            continue
        ctx = low[max(0, idx - 40): idx + len(kl) + 40]
        if kl in _BAN_STRICT_KWS and not any(c in ctx for c in _BAN_STRICT_CONTEXT):
            continue
        return kw, body_text[max(0, idx - 40): idx + len(kl) + 60].replace("\n", " ⏎ ")
    # 四位错误码（实测 [9016] 账号状态异常）：单独出现也认，避免文案改版后漏判
    m = re.search(getattr(cfg, "MARKET_BAN_CODE_PATTERN", r"\[\s*9\d{3}\s*\]"), body_text)
    if m:
        idx = m.start()
        return m.group(0), body_text[max(0, idx - 10): idx + 80].replace("\n", " ⏎ ")
    return "", ""


async def _market_check_ban(page: Page, enabled: bool = True) -> str:
    """扫页面有没有反爬/封禁/验证码关键词。命中返回原文，空串=没命中"""
    global _market_ban_until
    if not enabled:
        return ""
    try:
        body_text = await page.locator("body").inner_text(timeout=2000) or ""
    except Exception:
        try:
            body_text = await page.content() or ""
        except Exception:
            return ""
    kw, ctx = _market_ban_hit(body_text)
    if kw:
        msg = f"🚨 一车一件触发反爬/封禁机制！关键词『{kw}』上下文: …{ctx}…"
        print(f"\n{msg}")
        log_audit(msg)
        # 进入封禁冷却
        with _market_ban_lock:
            _market_ban_until = time.monotonic() + cfg.MARKET_BAN_COOLDOWN
        return msg

    # 网络层：接口真的返回过 429（页面文字可能还是正常的）
    status, url = market_recent_http_ban(60.0)
    if status:
        market_clear_http_ban()
        msg = f"🚨 一车一件网络层限流：HTTP {status} → {url[:120]}"
        print(f"\n{msg}")
        log_audit(msg)
        with _market_ban_lock:
            _market_ban_until = time.monotonic() + cfg.MARKET_BAN_COOLDOWN
        return msg
    return ""


async def _market_humanize(page: Page) -> None:
    """人类化小动作：鼠标微移 + 滚轮微滚。Playwright 的 locator.click 本身就会滚动元素、
    触发 mouseover 等事件，但加上这些小动作更像真人"""
    try:
        await page.mouse.move(
            random.randint(100, 800), random.randint(200, 600), steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.1, 0.3))
        if random.random() < 0.4:
            await page.mouse.wheel(0, random.randint(-80, 80))
            await asyncio.sleep(random.uniform(0.1, 0.25))
    except Exception:
        pass


def market_reset_claim_rate() -> None:
    """一个报案号跑完后调，让下一个 VIN 查询有个干净的起点"""
    global _last_market_part_time
    with _market_lock:
        _last_market_part_time = 0.0


# ============================================================
# 一车一件弹窗（车辆信息 / 配件信息）→ 自动点『确定』
#   实测：查 VIN 后会弹「车辆信息」框（品牌/车系/年份… + 橙色确定按钮），
#   不点掉页面就卡在弹窗上，后续查询全部无法进行
# ============================================================
_MARKET_MODAL_JS = r"""
() => {
  const LABELS = ['确定', '确认', '知道了', '关闭', '好的', '完成', '关闭当前窗口'];
  const BOX_SELS = ['.el-dialog', '.el-dialog__wrapper', '.el-message-box', '.ant-modal',
                    '.modal-dialog', '.modal', '[role="dialog"]', '.layui-layer',
                    '.dialog', '.dialog-vehicle-select', '.popup'];
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // 清掉上一轮打的标记（否则会点到已隐藏的旧弹窗按钮）
  document.querySelectorAll('[data-bot-market-confirm]')
    .forEach(e => e.removeAttribute('data-bot-market-confirm'));
  const shown = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const boxShown = (el) => shown(el) && el.getBoundingClientRect().width > 80
                              && el.getBoundingClientRect().height > 40;
  const all = [];
  for (const sel of BOX_SELS) {
    document.querySelectorAll(sel).forEach(n => { if (boxShown(n)) all.push(n); });
  }
  const boxes = Array.from(new Set(all));
  // 后出现的通常在最上层 → 从后往前找
  for (let i = boxes.length - 1; i >= 0; i--) {
    const box = boxes[i];
    const cand = Array.from(box.querySelectorAll(
        'button, .el-button, input[type=button], a, div, span, p'))
      .filter(b => {
        if (!shown(b)) return false;
        const t = clean(b.innerText || b.value || '');
        if (!t || t.length > 10) return false;
        if (!LABELS.some(l => t === l || t.indexOf(l) >= 0)) return false;
        const cls = String(b.className || '');
        return b.tagName === 'BUTTON' || b.tagName === 'A' ||
               /btn|button|el-button|confirm|primary|sure|ok/i.test(cls);
      });
    if (!cand.length) continue;
    const btn = cand[cand.length - 1];
    btn.setAttribute('data-bot-market-confirm', '1');
    return {
      ok: true,
      cls: String(box.className || box.id || '').slice(0, 60),
      title: clean(box.innerText).slice(0, 40),
      label: clean(btn.innerText || btn.value)
    };
  }
  return {ok: false};
}
"""


# 弹窗右上角 X 关闭按钮
_MARKET_CLOSE_X_JS = r"""
() => {
  const SELS = ['.el-dialog__headerbtn', '.el-dialog__close', '.el-message-box__headerbtn',
                '.ant-modal-close', '.close', '.dialog-close'];
  for (const s of SELS) {
    for (const el of document.querySelectorAll(s)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && getComputedStyle(el).display !== 'none') {
        el.setAttribute('data-bot-market-x', '1');
        return true;
      }
    }
  }
  return false;
}
"""


# VIN 输入框是否被遮挡（用于判断弹窗是否真的清干净了）
_MARKET_BLOCKED_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!top) return false;
  if (top === el || el.contains(top) || top.contains(el)) return false;
  const tag = (top.tagName || '').toUpperCase();
  if (tag === 'INPUT' || tag === 'TEXTAREA') return false;
  return true;
}
"""


async def dismiss_market_modal(page: Page, wait: float = 0.0, max_rounds: int = 3,
                               text_filter: Optional[list] = None) -> int:
    """一车一件弹窗自动确认

    wait 秒内轮询等弹窗出现；wait=0 只扫一遍（逐零件查询时几乎不弹窗，避免白等）。
    text_filter：只点“弹窗文本包含任一关键词”的弹窗（防误点结果面板上的『确定』）。
    返回实际点掉的弹窗个数。
    """
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return 0

    clicked = 0
    picks = 0
    deadline = time.monotonic() + max(0.0, wait)
    while clicked < max_rounds:
        found = False
        for fr in page.frames:
            try:
                info = await fr.evaluate(_MARKET_MODAL_JS)
            except Exception:
                continue
            if not (info and info.get("ok")):
                continue
            box_text = f"{info.get('title') or ''}"
            if text_filter:
                if not any(kw in box_text for kw in text_filter):
                    continue
            # 「配置差异项」弹窗：必须先随便选一款车款，再点确定，否则确定可能无效
            if "配置差异" in box_text:
                if picks < 2 and await _pick_config_diff_car(page, wait=0.8):
                    picks += 1
                    found = True
                    await asyncio.sleep(0.4)
                    continue
            ok = False
            try:
                await fr.locator("[data-bot-market-confirm='1']").last.click(timeout=2500)
                ok = True
            except Exception as exc:
                # Playwright 点击被叠层挡住 → 用 JS 原生 click 兜底
                try:
                    await fr.evaluate(
                        "() => { const a = document.querySelectorAll('[data-bot-market-confirm=\"1\"]');"
                        " const b = a[a.length - 1]; if (b) b.click(); }")
                    ok = True
                    print(f"  [i] 弹窗按钮被遮挡 → 已用 JS 原生点击")
                except Exception:
                    print(f"  [!] 一车一件弹窗『{info.get('label')}』点击失败: {exc}")
            if not ok:
                return clicked
            clicked += 1
            found = True
            print(f"  [✓] 已点一车一件弹窗『{info.get('label')}』"
                  f"（{info.get('title') or info.get('cls')}）")
            log_audit(f"一车一件弹窗确认: {info.get('title') or info.get('cls')} → {info.get('label')}")
            await asyncio.sleep(0.5)
            break
        if not found:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.4)
    return clicked


async def _market_click_close_x(page: Page) -> bool:
    """点弹窗右上角 X（确定按钮点不到时的兜底）"""
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return False
    for fr in page.frames:
        try:
            if await fr.evaluate(_MARKET_CLOSE_X_JS):
                try:
                    await fr.locator("[data-bot-market-x='1']").first.click(timeout=1500)
                except Exception:
                    await fr.evaluate(
                        "() => { const b=document.querySelector('[data-bot-market-x=\"1\"]'); if(b) b.click(); }")
                print("  [✓] 已点弹窗右上角 X")
                log_audit("一车一件弹窗 → 点 X 关闭")
                await asyncio.sleep(0.3)
                return True
        except Exception:
            continue
    return False


async def _market_vin_blocked(page: Page, vin_sel: str) -> bool:
    """VIN 输入框是否被弹窗/遮罩挡住"""
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return False
    for fr in page.frames:
        try:
            if await fr.evaluate(_MARKET_BLOCKED_JS, vin_sel):
                return True
        except Exception:
            continue
    return False


async def _market_handle_vehicle_dialogs(page: Page, wait: float = 15.0) -> None:
    """VIN 查询后的两段式弹窗处理：

    ① 「车辆信息」弹窗 → 点『确定』（部分车型点完直接进详情页）
    ② 部分车型接着弹「配置差异项」→ 随便选一款车款 → 再点『确定』

    连续两次扫描都没有弹窗可处理就提前结束，避免白等。
    """
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return
    deadline = time.monotonic() + max(0.0, wait)
    idle = 0
    while time.monotonic() < deadline:
        acted = False
        if await _pick_config_diff_car(page, wait=0.0):
            acted = True
        if await dismiss_market_modal(page, wait=0.3, max_rounds=2,
                                      text_filter=["车辆信息", "配置差异", "提示", "警告"]):
            acted = True
        if acted:
            idle = 0
            await asyncio.sleep(0.6)
            continue
        idle += 1
        if idle >= 2:
            break
        await asyncio.sleep(0.5)


async def _market_popup_open(page: Page) -> bool:
    """页面上是否还有可识别的弹窗"""
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return False
    for fr in page.frames:
        try:
            info = await fr.evaluate(_MARKET_MODAL_JS)
        except Exception:
            continue
        if info and info.get("ok"):
            return True
    return False


async def _market_clear_modals(page: Page, rounds: int = 3) -> None:
    """把一车一件页面上残留的弹窗清干净：先选车款(配置差异项) → 再点确定 → 再点 X"""
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return
    for _ in range(max(1, rounds)):
        picked = await _pick_config_diff_car(page, wait=0.0)
        clicked = await dismiss_market_modal(page, wait=0.5, max_rounds=2)
        if picked or clicked:
            await asyncio.sleep(0.3)
            continue
        if await _market_click_close_x(page):
            continue
        break
    # 兜底：整轮都点不动（确定按钮可能失效）→ 试右上角 X
    if await _market_popup_open(page):
        await _market_click_close_x(page)


# ============================================================
# STEP 5: 一车一件 VIN 首页 → 输入 VIN → 查询 → 跳详情页
# ============================================================
@register_step(desc="一车一件 VIN 首页查询车辆 → 跳详情页", inputs=["vin"])
async def step_market_vin_search(page: Page, context: dict, **kwargs) -> Page:
    vehicle = context.get("vehicle")
    vin = kwargs.get("vin") or (vehicle.vin if vehicle else "")
    '''
    一车一件首页（只有 VIN 大输入框） → 输 VIN 点查询 → 页面跳转到详情页
    复用同一个 page（真实网站可能是 hash 路由 SPA，不一定真的跳 URL）
    '''
    print(f"\n🔹 STEP 5: 一车一件 VIN 首页 → VIN {vin} 查询")

    vin_input_sel = _market_sel("vin_input")
    vin_search_sel = _market_sel("vin_search")

    # 人工等效：已人工查过 VIN 进入详情页（URL 已跳详情 / 详情元素已出现）→ 跳过
    vin_input = page.locator(vin_input_sel).first
    if not await is_visible_now(vin_input):
        print(f"  [i] 未找到 VIN 输入框 → 判定已在详情页（可能人工已查询），跳过")
        log_audit(f"跳过 VIN 查询（已在详情页）: {vin}")
        return page

    # ⓪ 先清掉上一步遗留的弹窗（车辆信息 / 配置差异项 / 遮罩），否则 VIN 输入框点不到
    await _market_clear_modals(page, rounds=3)

    # 🍃 反爬保护 ① 速率限流 + ② 封禁检查（均受控制台开关控制）
    from bot.config import USE_MOCK_MARKET
    _rate_on = bool(context.get("market_rate_limit", True))
    _breaker_on = bool(context.get("market_breaker", True))
    if not USE_MOCK_MARKET:
        # 封禁冷却期内不再访问一车一件（避免延长封禁），本单直接以定损价提交
        if _breaker_on:
            _left = market_ban_remaining()
            if _left > 0:
                print(f"\n  ⏸ 一车一件处于封禁冷却（剩 {_left/60:.1f} 分钟）"
                      f"→ 本单跳过比价，全部以定损价提交")
                log_audit(f"一车一件封禁冷却剩 {int(_left)}s → 跳过 VIN {vin} 比价")
                context["skip_market_compare"] = True
                context["market_touched"] = False   # 没真正查询 → 下一单免冷却
                return page
        await _market_rate_wait("vin", rate_enabled=_rate_on, breaker_enabled=_breaker_on)
        if _rate_on:
            await _market_humanize(page)
        if _breaker_on and await _market_check_ban(page):
            raise RuntimeError("一车一件触发反爬封禁，已进入自动休眠")

    async def _fill_and_search() -> None:
        await safe_fill(vin_input, vin, f"VIN码输入框 → {vin}")
        await human_delay()
        # 真实外网可能需要点 span 包裹的图标按钮，尝试 text 匹配
        search_btn = page.locator(vin_search_sel).first
        try:
            await safe_click(search_btn, "查询 VIN 按钮")
        except Exception:
            # fallback: 用 Enter 提交
            await vin_input.press("Enter")
            print(f"  [✓] (fallback) VIN 输入框按 Enter 提交")

    try:
        await _fill_and_search()
    except Exception as exc:
        # 弹窗/遮罩把 VIN 输入框挡住了（实测会一直重试到 15s 超时）
        print(f"\n  [!] VIN 输入被弹窗遮挡（{type(exc).__name__}）→ 清理弹窗后重试")
        log_audit(f"VIN 输入被遮挡 → 清理弹窗重试: {vin}")
        await _market_clear_modals(page, rounds=4)
        need_reload = not await is_visible_now(vin_input)
        if (not need_reload and not USE_MOCK_MARKET
                and await _market_vin_blocked(page, vin_input_sel)):
            need_reload = True
        if need_reload and not USE_MOCK_MARKET:
            print("  [!] 弹窗仍关不掉 → 重新加载一车一件首页")
            log_audit("一车一件弹窗卡死 → 重新加载首页")
            from bot.config import MARKET_REAL_URL
            try:
                await page.goto(MARKET_REAL_URL, wait_until="domcontentloaded",
                                timeout=DEFAULT_TIMEOUT * 2)
            except Exception:
                await page.reload(wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT * 2)
            await asyncio.sleep(2.0)
            await _market_clear_modals(page, rounds=2)
        await _fill_and_search()
    await human_delay(800, 1500)

    # 🍃 查询后再扫封禁（反爬常在提交后异步返回封禁提示 → 轮询 5 次，别只看一眼）
    if not USE_MOCK_MARKET and _breaker_on:
        for _ in range(5):
            if await _market_check_ban(page):
                raise RuntimeError("一车一件触发反爬封禁（VIN 查询后），已进入自动休眠")
            await asyncio.sleep(0.6)

    # 一车一件查 VIN 后固定弹「车辆信息」→ 点确定；部分车型接着弹「配置差异项」→ 随便选一款车款 → 确定
    await _market_handle_vehicle_dialogs(page, wait=15.0)

    # 等待详情页车辆信息出现（查不到 VIN 会抛超时 → 设置跳过 flag）
    global _market_vin_fail_streak
    try:
        await _wait_for_market_detail(page)
        await _market_handle_vehicle_dialogs(page, wait=8.0)
        if not USE_MOCK_MARKET:
            ok = await _ensure_market_ready(page, timeout=20.0)
            if not ok:
                raise RuntimeError("未出现零件查询输入框（弹窗未处理完或 VIN 无数据）")
        _market_vin_fail_streak = 0
        print(f"  [✓] VIN 查询成功，已进入详情页")
    except Exception as exc:
        # 先复检封禁：封禁页面同样"没有零件查询框"，别把它当成"查不到 VIN"继续请求
        if not USE_MOCK_MARKET and _breaker_on and await _market_check_ban(page):
            _market_vin_fail_streak = 0
            raise RuntimeError("一车一件触发反爬封禁（VIN 查询无详情），已进入自动休眠")
        _market_vin_fail_streak += 1
        if not USE_MOCK_MARKET and _breaker_on and _market_vin_fail_streak >= 2:
            # 连续 2 次查不到 → 大概率已被风控，主动暂停，不再打第三次
            _market_vin_fail_streak = 0
            market_set_ban(cfg.MARKET_BAN_COOLDOWN, "连续 2 次 VIN 查询无结果 → 疑似封禁")
            context["skip_market_compare"] = True
            raise RuntimeError("一车一件连续 2 次 VIN 查询无结果（疑似封禁），已暂停一车一件")
        print(f"\n  ⚠️ VIN {vin} 在一车一件查不到（{type(exc).__name__}）→ 跳过比价环节，全部以定损价为准")
        log_audit(f"VIN {vin} 查不到 → 跳过比价（连续失败 {_market_vin_fail_streak} 次）")
        context["skip_market_compare"] = True
        return page
    return page


# ============================================================
# 一车一件「配置差异项」弹窗（部分车型在车辆信息确定后出现）
#   DOM：el-dialog 标题「配置差异项」，右侧一列「车款（可直接选择）」
#        随便点一款车款，再点「确定」即可，页面才进入零件查询
# ============================================================
_JS_CONFIG_DIFF = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-bot-carpick]')
    .forEach(e => e.removeAttribute('data-bot-carpick'));
  const boxes = Array.from(document.querySelectorAll(
      '.el-dialog, .el-dialog__wrapper, .el-message-box, [role="dialog"], .modal, .layui-layer'));
  const dlg = boxes.find(el => {
    if (!el || el.offsetParent === null) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 240 || r.height < 120) return false;
    return (el.innerText || '').includes('配置差异项');
  });
  if (!dlg) return {ok: false, stage: 'none'};

  const dRect = dlg.getBoundingClientRect();
  const skip = ['确定', '下一步', '上一步', '车款（可直接选择）', '选择配置差异项', '配置差异项'];
  const pick = (el) => {
    el.setAttribute('data-bot-carpick', '1');
    return {ok: true, stage: 'list', picked: clean(el.innerText).slice(0, 40)};
  };

  // ① 严格：右半边 + pointer/类名像选项 + 文本是车款
  let cands = Array.from(dlg.querySelectorAll('div,li,span,p,a'))
    .filter(el => {
      if (el.offsetParent === null) return false;
      const r = el.getBoundingClientRect();
      if (r.width < 60 || r.height < 16 || r.height > 90) return false;
      if (r.left < dRect.left + dRect.width * 0.42) return false;
      const t = clean(el.innerText);
      if (!t || t.length > 90) return false;
      if (skip.includes(t)) return false;
      const cls = String(el.getAttribute('class') || '');
      const s = getComputedStyle(el);
      return s.cursor === 'pointer' || /item|option|car|list|row/i.test(cls);
    });
  if (cands.length) return pick(cands[0]);

  // ② 宽松：找「车款（可直接选择）」表头，点它同级的下一行
  const hdr = Array.from(dlg.querySelectorAll('div,span,p,th,dt'))
    .find(el => clean(el.innerText) === '车款（可直接选择）');
  if (hdr) {
    const scope = hdr.parentElement || dlg;
    const rows = Array.from(scope.children).filter(el => {
      if (el === hdr || el.contains(hdr)) return false;
      const t = clean(el.innerText);
      return t && t.length > 3 && t.length <= 90;
    });
    if (rows.length) return pick(rows[0]);
    // 退一步：整个弹窗右半边任意可点文本
    const anyRight = Array.from(dlg.querySelectorAll('div,li,span,p,a')).filter(el => {
      if (el.offsetParent === null) return false;
      const r = el.getBoundingClientRect();
      if (r.left < dRect.left + dRect.width * 0.5) return false;
      if (r.width < 60 || r.height < 16 || r.height > 90) return false;
      const t = clean(el.innerText);
      return t && t.length > 3 && t.length <= 90 && !skip.includes(t);
    });
    if (anyRight.length) return pick(anyRight[0]);
  }
  return {ok: false, stage: 'no-list'};
}
"""


async def _pick_config_diff_car(page: Page, wait: float = 6.0) -> bool:
    """「配置差异项」弹窗 → 随便点一款车款。找到并点了返回 True"""
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return False

    deadline = time.monotonic() + max(0.0, wait)
    while True:
        for fr in page.frames:
            try:
                info = await fr.evaluate(_JS_CONFIG_DIFF)
            except Exception:
                continue
            if not (info and info.get("ok")):
                continue
            try:
                await fr.locator("[data-bot-carpick='1']").last.click(timeout=3000)
                print(f"  [✓] 配置差异项 → 已选车款『{info.get('picked')}』")
                log_audit(f"配置差异项 → 选车款『{info.get('picked')}』")
                await asyncio.sleep(0.4)
                return True
            except Exception as exc:
                # Playwright 点击被遮挡 → JS 原生 click 兜底
                try:
                    await fr.evaluate(
                        "() => { const a=document.querySelectorAll('[data-bot-carpick=\"1\"]');"
                        " const e=a[a.length-1]; if(e) e.click(); }")
                    print(f"  [✓] 配置差异项 → 已选车款『{info.get('picked')}』(JS 兜底)")
                    log_audit(f"配置差异项 → 选车款『{info.get('picked')}』(JS)")
                    await asyncio.sleep(0.4)
                    return True
                except Exception:
                    print(f"  [!] 配置差异项选车款失败: {exc}")
                    return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.3)


async def _ensure_market_ready(page: Page, timeout: float = 20.0) -> bool:
    """等一车一件进入零件查询界面；期间自动处理 车辆信息 / 配置差异项 弹窗"""
    part_input = page.locator(_market_sel("part_input")).first
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            if await part_input.count() > 0 and await part_input.is_visible():
                return True
        except Exception:
            pass
        # 弹「配置差异项」→ 选一款车款；再点「确定」；都没有就试试右上角 X
        picked = await _pick_config_diff_car(page, wait=1.0)
        clicked = await dismiss_market_modal(page, wait=1.0, max_rounds=2,
                                             text_filter=["车辆信息", "配置差异", "提示", "警告"])
        if not picked and not clicked:
            await _market_click_close_x(page)
        await asyncio.sleep(0.3)
    try:
        return await part_input.count() > 0 and await part_input.is_visible()
    except Exception:
        return False


async def _wait_for_market_detail(page: Page) -> None:
    """
    等待 VIN 查询完成信号：
    - mock:  URL 跳到 market_detail.html
    - real:  等页面 load + 短暂 sleep（SPA hash 路由不一定跳 URL）
    """
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        # mock 里 location.href 会从 market_home.html 跳到 market_detail.html
        try:
            await page.wait_for_url("**/market_detail.html**", timeout=DEFAULT_TIMEOUT)
        except Exception:
            # SPA hash 路由不会真跳 URL，等元素出现
            await expect(page.locator("#current-vin")).to_be_visible(timeout=DEFAULT_TIMEOUT)
    else:
        # 真实外网：等 1-2 秒 + 看是否有结果容器
        await page.wait_for_load_state("networkidle")
        try:
            await page.wait_for_timeout(1500)
        except Exception:
            pass


# ============================================================
# STEP 6: 一车一件详情页 → 逐个查询零件市场价
# ============================================================
@register_step(desc="一车一件详情页逐个查询零件市场价", inputs=["parts"])
async def step_market_query_parts(page: Page, context: dict, **kwargs) -> None:
    parts = kwargs.get("parts") or context.get("parts") or []
    tolerance = float(kwargs.get("tolerance") or context.get("tolerance") or 0)
    print(f"\n🔹 STEP 6: 一车一件详情页 → 逐个查询 {len(parts)} 个零件市场价")
    if tolerance:
        print(f"  [i] 价格容忍度 ¥{tolerance:g}（超出市场价在此金额以内视为合规）")

    from bot.config import USE_MOCK_MARKET

    _rate_on = bool(context.get("market_rate_limit", True))
    _breaker_on = bool(context.get("market_breaker", True))

    part_input_sel  = _market_sel("part_input")
    part_search_sel = _market_sel("part_search")

    part_input = page.locator(part_input_sel).first
    part_search_btn = page.locator(part_search_sel).first

    for i, part in enumerate(parts):
        print(f"\n  📦 [{i+1}/{len(parts)}] 查询: {part.code}  {part.name}")

        # 确保仍在零件查询界面；若被退回 VIN 首页则用原 VIN 重新查询
        if not await _ensure_market_ready(page, timeout=6.0):
            if not await _recover_market_page(page, context):
                print("  [!] 一车一件已不在零件查询界面，自动恢复失败 → 剩余零件全部以定损价为准")
                for rest in parts[i:]:
                    rest.market_price = None
                    rest.within_market = None
                break

        # 🍃 反爬保护：每个零件查询前都限流 + 扫封禁（受控制台开关控制）
        if not USE_MOCK_MARKET:
            await _market_rate_wait("part", rate_enabled=_rate_on, breaker_enabled=_breaker_on)
            if _rate_on:
                await _market_humanize(page)
            if _breaker_on and await _market_check_ban(page):
                print("  🛑 触发反爬封禁 → 剩余零件全部以定损价为准")
                for rest in parts[i:]:
                    rest.market_price = None
                    rest.within_market = None
                break

        try:
            await safe_fill(part_input, part.code, f"零件编码输入框 → {part.code}")
            await human_delay()

            try:
                await safe_click(part_search_btn, f"查询零件 {part.code}")
            except Exception:
                await part_input.press("Enter")
                print(f"      (fallback Enter)")

            await human_delay(600, 1000)

            # 🍃 零件查询后再扫一次封禁
            if not USE_MOCK_MARKET and _breaker_on and await _market_check_ban(page):
                print("  🛑 触发反爬封禁（零件查询后）→ 剩余零件全部以定损价为准")
                for rest in parts[i:]:
                    rest.market_price = None
                    rest.within_market = None
                break

            # 读市场价（结果区 section.part-list 此时应已出现）
            price, meta = await _extract_market_price(page)
        except Exception as exc:
            # 单个零件失败不能中断整个案件 → 跳过该零件，继续下一个
            print(f"      ⚠️ 零件 {part.code} 查询异常（{type(exc).__name__}）→ 跳过该零件，以定损价为准")
            log_audit(f"零件 {part.code} 查询异常，跳过：{exc}")
            part.market_price = None
            part.within_market = None
            await _close_market_result(page)
            continue

        # 回到可继续输入的状态（清空搜索框，绝不按 Esc / 绝不点『重新查询』）
        await _close_market_result(page)

        # 零件编码查不到 / 价格为 0 → 跳过当前零件，以定损价为准
        if price is None or price <= 0:
            print(f"      ⚠️ 零件 {part.code} 查不到市场价（price={price}）→ 跳过，以定损价 ¥{part.local_price} 为准")
            part.market_price = None
            part.within_market = None
            continue

        if meta.get("wrong_oe"):
            cand = "、".join(f"¥{v:g}" for v in sorted(meta.get("prices") or []))
            print(f"      [!] 查询的 OE 号不属于本车（定损选错配件）→ 属本车 OE 号价 {cand}，取最低非0价 ¥{price:g}")
            log_audit(f"零件 {part.code} 查询OE不属于本车，多OE价[{cand}] → 取最低 ¥{price:g} 用于比价")

        part.market_price = price
        part.check(tolerance)
        print(f"      本地价 ¥{part.local_price}  市场价 ¥{price}  {part.status_text}")
        await human_delay()


async def _extract_market_price(page: Page, timeout: float = 8.0) -> tuple:
    """
    从一车一件「查询结果」表提取市场价 → (价格, 附加信息)

    - mock:  #result-tbody tr 下的 td.market-price-cell
    - real:  按表头找「4S店价格」列（生产实测该列就是市场价，一个单元格里可能有
              原价 1604.05 + 折扣价 0.00 两个数字 → 取第一个大于 0 的）
    - 定损选错配件：结果里出现「您查询的OE号不属于本车」时，这一行会有多个 OE 号
      对应的价格（含 0），此时取「非 0 的最低价」作为市场价，附加信息 wrong_oe=True
    """
    from bot.config import USE_MOCK_MARKET

    if USE_MOCK_MARKET:
        tbody_sel = _market_sel("result_tbody")
        price_sel = _market_sel("market_price_cell")
        rows = page.locator(f"{tbody_sel} tr")
        count = await rows.count()
        if count == 0:
            return None, {"wrong_oe": False, "prices": []}
        for i in range(count):
            row = rows.nth(i)
            row_html = await row.evaluate("el => el.outerHTML")
            if any(kw in row_html for kw in ["⭐", "exact-match", "exact-row", "精准匹配"]):
                txt = (await row.locator(price_sel).first.text_content() or "").strip()
                return _parse_price(txt), {"wrong_oe": False, "prices": []}
        txt = (await rows.first.locator(price_sel).first.text_content() or "").strip()
        return _parse_price(txt), {"wrong_oe": False, "prices": []}

    # 真实外网：整页 JS 读取（dataenlighten 结果区是 section.part-list 的 div 布局）
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for fr in page.frames:
            # 策略 A：section.part-list 里每行的 .cell.price-oe .price
            try:
                data = await fr.evaluate(_JS_MARKET_PART_LIST_PRICE)
            except Exception:
                data = None
            rows = (data or {}).get("rows") or []
            # ① 查询的 OE 不属于本车：优先用「属于本车」的几个 OE 价里非0最低价
            #    （is-requested 就是你查的那个 OE，正常查询时两边价格一样，不必改价也不必刷日志）
            for raw in rows:
                row_text = raw.get("rowText") or ""
                if "不属于本车" not in row_text:
                    continue
                own: list[float] = []      # 属于本车的 OE 价
                req_price: Optional[float] = None
                for cell in (raw.get("oePrices") or []):
                    p = _parse_market_price_cell(cell.get("price") or "")
                    if p is None:
                        continue
                    if cell.get("requested"):
                        req_price = float(p)
                    elif p > 0:
                        own.append(float(p))
                if own:
                    low = min(own)
                    # 只有"属本车价低于查询价"才算定损选错配件，需要改价
                    wrong = req_price is not None and low < req_price - 0.005
                    return low, {"wrong_oe": wrong, "prices": own, "requested": req_price}
                if req_price and req_price > 0:
                    # 属本车的 OE 全是 0 → 以定损价为准
                    return None, {"wrong_oe": False, "prices": [], "requested": req_price}
            first_price: Optional[float] = None
            for raw in rows:
                for cell in (raw.get("prices") or []):
                    price = _parse_market_price_cell(cell)
                    if price is None:
                        continue
                    row_text = raw.get("rowText") or ""
                    if "属于本车" in row_text:
                        return price, {"wrong_oe": False, "prices": []}
                    if first_price is None:
                        first_price = price
                    break
            if first_price is not None:
                return first_price, {"wrong_oe": False, "prices": []}
            # 策略 B：用「4S店价格」表头坐标向下取同列数字（布局再变也能兜住）
            try:
                geo = await fr.evaluate(_JS_MARKET_PRICE_GEOMETRY)
            except Exception:
                continue
            for txt in (geo or {}).get("prices") or []:
                price = _parse_market_price_cell(txt)
                if price is not None:
                    return price, {"wrong_oe": False, "prices": []}
        if time.monotonic() >= deadline:
            return None, {"wrong_oe": False, "prices": []}
        await asyncio.sleep(0.3)


# 生产源码实测（一车一件.html）：查询结果区是 section.part-list
#   <section class="part-list">
#     <header>查询结果 <span class="result-count">共 2 个结果</span></header>
#     <main><header>…<div class="cell price-oe">…<div class="price">4S店价格</div>…</header>
#     <main><ul><li class="is-expanded"><div class="content">
#         <div class="cell price-oe"><div class="price-oe__wrap"><div class="price-oe__bg">
#            <div class="price-oe__inner is-requested"><div class="price">693.00</div>
#                                               <div class="oe">E00130010…</div></div>
#            <div class="price-oe__inner is-right"><div class="price">693.00</div>…</div>
#         </div></div></div>
#     （.part-details 里的 el-table 是装配图子件表，价格跟本次 OE 无关，不能取）
_JS_MARKET_PART_LIST_PRICE = r"""
() => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const out = [];
  Array.from(document.querySelectorAll('.part-list')).forEach(pl => {
    Array.from(pl.querySelectorAll('main ul li')).forEach(li => {
      // 每个 price-oe__inner 是一个 OE 号对应的价格（is-requested = 你查的那个 OE）
      const oePrices = Array.from(li.querySelectorAll('.cell.price-oe .price-oe__inner'))
        .map(box => {
          const pEl = box.querySelector('.price');
          const oEl = box.querySelector('.oe');
          return {
            price: clean(pEl ? pEl.textContent : ''),
            oe: clean(oEl ? oEl.textContent : ''),
            requested: (box.className || '').indexOf('is-requested') >= 0,
          };
        })
        .filter(x => x.price && x.price !== '-' && x.price !== '--');
      const prices = Array.from(li.querySelectorAll('.cell.price-oe .price'))
        .map(el => clean(el.textContent))
        .filter(t => t && t !== '-' && t !== '--');
      const content = li.querySelector('.content') || li;
      out.push({
        prices: prices,
        oePrices: oePrices,
        rowText: clean(content.textContent).slice(0, 300),
      });
    });
  });
  return {rows: out};
}
"""


_JS_MARKET_PRICE_GEOMETRY = r"""
() => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const isPrice = (t) => /^\d{1,3}(,\d{3})*(\.\d+)?$|^\d+(\.\d+)?$/.test(t);
  const heads = [];
  Array.from(document.querySelectorAll('th,td,span,div,p')).forEach(el => {
    if (el.offsetParent === null) return;
    if (clean(el.textContent) === '4S店价格') heads.push(el);
  });
  if (!heads.length) return {ok: false, prices: []};

  // 取最深（叶子）的表头节点，面积最小
  heads.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (ra.width * ra.height) - (rb.width * rb.height);
  });
  const h = heads[0].getBoundingClientRect();

  const prices = [];
  const seenTop = new Set();
  Array.from(document.querySelectorAll('span,div,td,p,a')).forEach(el => {
    if (el.offsetParent === null) return;
    const t = clean(el.textContent);
    if (!isPrice(t)) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.height > 60) return;
    if (Math.abs(r.left - h.left) > 24) return;   // 与表头同一列
    if (r.top <= h.bottom - 2) return;           // 表头下方
    const bucket = Math.round(r.top / 4);
    if (seenTop.has(bucket)) return;
    seenTop.add(bucket);
    prices.push(t);
  });
  return {ok: prices.length > 0, prices: prices};
}
"""


# ============================================================
# 零件查价后回到「可继续输入」的状态
#   真实站点（dataenlighten）结果区是页面内的 section.part-list，搜索框一直在，
#   Esc 会被 SPA 当成“返回”直接退回 VIN 首页 → 后面所有零件都查不到。
#   所以这里绝不按 Esc、绝不点『确定』、绝不点『重新查询』。
# ============================================================
async def _close_market_result(page: Page) -> None:
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return

    # 1) 清空搜索框（有 .icon-clear 清除按钮就点一下）
    try:
        clear_btn = page.locator(".search-input .icon-clear, .input .icon-clear").first
        if await clear_btn.count() > 0 and await clear_btn.is_visible():
            await clear_btn.click(timeout=1500)
            await asyncio.sleep(0.3)
    except Exception:
        pass

    # 2) 搜索框不在（被结果视图顶掉）→ 点「常用配件查询」标签切回
    try:
        inp = page.locator(_market_sel("part_input")).first
        if await inp.count() == 0 or not await inp.is_visible():
            tab = page.locator(".common-parts header span:has-text('常用配件查询')").first
            if await tab.count() == 0:
                tab = page.locator("span:has-text('常用配件查询')").first
            if await tab.count() > 0 and await tab.is_visible():
                await tab.click(timeout=2000)
                print("      [i] 已切回『常用配件查询』回到查询界面")
                await asyncio.sleep(0.5)
    except Exception:
        pass


async def _recover_market_page(page: Page, context: dict) -> bool:
    """一车一件被退回 VIN 首页 → 用 context['vin'] 重新查询，回到零件查询界面"""
    from bot.config import USE_MOCK_MARKET
    if USE_MOCK_MARKET:
        return False

    vin = (context or {}).get("vin") or ""
    if not vin:
        return False

    try:
        home_input = page.locator("input[placeholder*='VIN']").first
        if await home_input.count() == 0 or not await home_input.is_visible():
            return False

        print(f"  [i] 页面已退回 VIN 首页 → 用 VIN {vin} 重新查询")
        await safe_fill(home_input, vin, "VIN码输入框（恢复）")
        await human_delay()
        query_btn = page.locator("button:visible:has-text('查询')").first
        await safe_click(query_btn, "查询 VIN 按钮（恢复）")

        await _market_handle_vehicle_dialogs(page, wait=12.0)
        await _wait_for_market_detail(page)
        ok = await _ensure_market_ready(page, timeout=20.0)
        if ok:
            print(f"  [✓] 已恢复到零件查询界面")
        return ok
    except Exception as exc:
        print(f"  [!] 重新查询 VIN 失败: {exc}")
        return False


def _parse_market_price_cell(cell: str) -> Optional[float]:
    if not cell:
        return None
    for m in re.finditer(r"\d+(?:\.\d+)?", cell.replace(",", "")):
        try:
            value = float(m.group(0))
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _parse_price(text: str) -> Optional[float]:
    if not text: return None
    # 清理：¥ ￥ , 空格 中文
    cleaned = re.sub(r"[¥￥\s,]", "", text)
    try: return float(cleaned)
    except ValueError: return None


# ============================================================
# STEP 7 辅助：在生产定损平台里把某个零件的「报价」改成 new_price
# 业务规则：本地价不可修改，只能改「报价」列
# ============================================================
_JS_MARK_PART_ROW = r"""
([code, serialNo]) => {
  const decode = (s) => {
    const t = document.createElement('textarea');
    t.innerHTML = s || '';
    return t.value;
  };
  document.querySelectorAll("tr[data-bot-target='1']")
    .forEach(el => el.removeAttribute('data-bot-target'));
  const trs = Array.from(document.querySelectorAll('tr[data-lossobj]'));
  for (const tr of trs) {
    let o = {};
    try { o = JSON.parse(decode(tr.getAttribute('data-lossobj'))); } catch (e) { continue; }
    const c = String(o.factPartCode || o.factPartShortCode || '').trim();
    const s = parseInt(o.serialNo || 0, 10);
    const hit = (code && c === code) || (!code && serialNo && s === serialNo);
    if (hit) {
      tr.setAttribute('data-bot-target', '1');
      return true;
    }
  }
  return false;
}
"""

_JS_QUOTE_COL = r"""
() => {
  const tr = document.querySelector("tr[data-bot-target='1']");
  if (!tr) return {ok: false, reason: 'row-not-found'};
  const cells = Array.from(tr.querySelectorAll('td'));
  // 生产表格中「修理厂报价」也含“报价”，且在真正的「报价」列之前。
  // 优先按真实输入框/字段名定位，绝不能再按第一个含“报价”的表头模糊匹配。
  let quoteIdx = cells.findIndex(td => td.querySelector(
    "input[name='estiPartPrice']:not([type='hidden']), input.partPrice:not([type='hidden'])"));
  let matchedBy = quoteIdx >= 0 ? 'input[estiPartPrice]' : '';
  const table = tr.closest('table');
  let ths = table ? Array.from(table.querySelectorAll('thead th, thead td')) : [];
  if (!ths.length && table) {
    const box = table.closest('.bootstrap-table');
    if (box) ths = Array.from(box.querySelectorAll('.fixed-table-header thead th, .fixed-table-header thead td'));
  }
  const heads = ths.map(h => (h.innerText || '').replace(/\s+/g, '').trim());
  if (quoteIdx < 0) {
    quoteIdx = ths.findIndex(h => (h.getAttribute('data-field') || '') === 'estiPartPrice');
    if (quoteIdx >= 0) matchedBy = 'data-field[estiPartPrice]';
  }
  if (quoteIdx < 0) {
    quoteIdx = heads.findIndex(h => /^(报价|换件报价|定损报价|核报价|审定价)([(（].*[)）])?$/.test(h));
    if (quoteIdx >= 0) matchedBy = 'exact-head';
  }
  const localIdx = heads.findIndex(h => h.includes('本地价'));
  return {
    ok: quoteIdx >= 0,
    colIndex: quoteIdx,
    quoteIndex: quoteIdx,
    localIndex: localIdx,
    matchedBy: matchedBy,
    heads: heads,
    cellCount: cells.length,
    hasInput: quoteIdx >= 0 && quoteIdx < cells.length &&
      !!cells[quoteIdx].querySelector("input:not([type='hidden'])"),
    cellText: (quoteIdx >= 0 && quoteIdx < cells.length) ? (cells[quoteIdx].innerText || '').trim() : '',
  };
}
"""


async def _loss_fix_quote_price(page: Page, part, new_price: float) -> bool:
    """把某零件的「报价」列改成 new_price（不是本地价，本地价只读）。

    任何异常都在内部吃掉，返回 False，绝不向上抛（避免整个案件被跳过）。
    """
    # ---- 1) mock：页面自带 setQuotePrice（生产不存在，必须 typeof 保护）----
    try:
        res = await page.evaluate(
            """([idx, price]) => {
                if (typeof setQuotePrice !== 'function') return {supported: false};
                try { return {supported: true, result: setQuotePrice(idx, price)}; }
                catch (e) { return {supported: true, error: String(e)}; }
            }""",
            [max(0, part.index - 1), new_price],
        )
        if res and res.get("supported"):
            if res.get("error"):
                print(f"      [!] mock setQuotePrice 报错: {res['error']}")
                return False
            ret = res.get("result") or {}
            return bool(ret.get("fixed", True))
    except Exception:
        pass

    # ---- 2) 生产：定位该零件所在行（data-lossobj 的 factPartCode / serialNo）----
    row = None
    info = None
    for fr in page.frames:
        try:
            hit = await fr.evaluate(_JS_MARK_PART_ROW, [part.code or "", part.index or 0])
        except Exception:
            continue
        if hit:
            try:
                row = fr.locator("tr[data-bot-target='1']").first
                info = await fr.evaluate(_JS_QUOTE_COL)
            except Exception:
                row = None
                continue
            break
    if row is None:
        print(f"      [!] 没定位到零件 {part.code} 在换件表里的行（data-lossobj 未匹配）")
        return False

    info = info or {}
    col = int(info.get("colIndex") if info.get("colIndex") is not None else -1)
    if col < 0:
        print(f"      [!] 换件表里找不到『报价』列（表头: {info.get('heads')}）→ 无法自动改价")
        return False

    col_tag = "报价"
    cell = row.locator("td").nth(col)
    print(f"      [i] 报价列定位: 第 {col + 1}/{info.get('cellCount')} 列（{info.get('matchedBy') or 'unknown'}）")

    # ---- 3) 单元格里有输入框 → 直接填 ----
    try:
        inp = cell.locator("input:not([type='hidden'])").first
        if await inp.count() > 0 and await inp.is_visible():
            try:
                before = (await inp.input_value() or "").strip()
            except Exception:
                before = ""
            await inp.click(timeout=4000)
            try:
                await inp.press("Control+A")
            except Exception:
                pass
            await inp.fill(str(int(new_price) if float(new_price).is_integer() else new_price))
            await inp.dispatch_event("input")
            await inp.dispatch_event("change")
            await inp.press("Tab")
            await asyncio.sleep(0.3)
            got = (await inp.input_value() or "").strip()
            got_price = _parse_price(got)
            before_price = _parse_price(before)
            target = float(new_price)
            # 生产页面会在 change/blur 后按自己的公式重算报价（实测 2409→2231.46、
            # 1872→1832.08，都低于目标价）。只要值确实变了、且不高于目标价即算改价成功，
            # 不能要求与输入值完全相等，否则会把已经改成功的零件误判为失败。
            if got_price is None or got_price <= 0:
                print(f"      [!] {col_tag}写入后读不到数值（期望 ¥{target}，实际 ¥{got}）")
                return False
            if before_price is not None and abs(got_price - before_price) < 0.01:
                print(f"      [!] {col_tag}写入后数值未变化（仍为 ¥{got}）")
                return False
            if got_price > target * 1.02:
                print(f"      [!] {col_tag}写入后仍高于目标价（期望 ≤¥{target}，实际 ¥{got}）")
                return False
            print(f"      [✓] {col_tag}已改价：输入 ¥{new_price} → 页面回读 ¥{got}")
            log_audit(f"定损平台改报价 {part.code}: ¥{part.local_price} → ¥{new_price}")
            return True
    except Exception as exc:
        print(f"      [!] 该行{col_tag}输入框填写失败: {str(exc).splitlines()[0][:100]}")

    # ---- 4) 双击唤起内联编辑，再试一次 ----
    try:
        await cell.dblclick(timeout=4000)
        await asyncio.sleep(0.4)
        inp = cell.locator("input:not([type='hidden'])").first
        if await inp.count() > 0 and await inp.is_visible():
            await inp.fill(str(new_price))
            await inp.dispatch_event("change")
            await inp.press("Tab")
            await asyncio.sleep(0.3)
            print(f"      [✓] 双击后{col_tag}已改为 ¥{new_price}")
            log_audit(f"定损平台双击改报价 {part.code}: → ¥{new_price}")
            return True
    except Exception as exc:
        print(f"      [!] 双击内联编辑失败: {str(exc).splitlines()[0][:100]}")

    print(f"      [!] {col_tag}单元格不可编辑（第 {col + 1}/{info.get('cellCount')} 列，"
          f"当前值 ¥{info.get('cellText')}，无可用 input；表头: {info.get('heads')}）")
    return False


def _need_resync_after_fix_failure(context: dict) -> None:
    """改价失败 → 标记需要重新同步定损（STEP 8 提交前会自动重跑一次）"""
    context["resync_loss"] = True
    print("  ⚠️ 有零件无法自动改价 → 提交前将重新执行『同步定损』，随后照常提交")


# ============================================================
def _quote_fix_part_key(part) -> str:
    """与改价历史表一致的零件键；无编码时退回落损单内序号"""
    code = (part.code or "").strip().upper()
    return code or f"#{int(part.index or 0):02d}"


@register_step(desc="自动修复超市场价零件报价列 + 点暂存", inputs=["parts"], outputs=["fixed_count"])
async def step_loss_fix_over_market(page: Page, context: dict, **kwargs) -> int:
    parts = kwargs.get("parts") or context.get("parts") or []
    """
    STEP 7: 定损平台 → 自动修复超市场价零件「报价」列 + 点暂存
    公式: 报价 = 一车一件价 × (定损价 / 本地价)，保留整数
    注意: 本地价只读不改，只能改报价列；同一报案已改成功的零件不再重复修改
    返回本次实际修复了多少个。本步骤不抛异常（改价失败只记录，流程继续往下走）
    """
    over = [p for p in parts if p.within_market is False]
    if not over:
        print(f"\n🔹 STEP 7: 无超市场价零件，跳过修复")
        return 0

    claim_no = (context.get("claim_no") or "").strip().upper()
    history_error = ""
    try:
        from bot.pricing import get_price_service
        price_svc = get_price_service()
        history = price_svc.get_claim_quote_fixes(claim_no)
    except Exception as exc:
        history = None
        history_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    if history is None:
        # 没有可靠的幂等记录时继续改价可能让报价越来越低，宁可暂停自动修改。
        print(f"\n  ⚠️ 改价历史不可用（{history_error or '数据库读取失败'}）"
              f" → 本报案暂停自动改价，避免重复下调")
        log_audit(f"改价历史不可用，暂停自动改价 | 报案号={claim_no} | {history_error}")
        for p in over:
            p.fixed_price = None
        _need_resync_after_fix_failure(context)
        return 0

    # 先查历史，再决定是否计算公式；历史零件完全不进入本轮改价。
    candidates = []
    history_fixed = []
    for p in over:
        part_key = _quote_fix_part_key(p)
        previous = history.get(part_key)
        if previous is not None:
            # 历史零件本轮不再写页面、也不再计入“本单改过报价”，
            # 以免没有新变化时仍被强制走『车辆定损』造成重复同步。
            p.fixed_price = None
            history_fixed.append(p)
            print(f"  ⏭ #{p.index:02d} {part_key}: 本报案此前已改价"
                  f"（{previous.get('fixed_at') or '历史记录'}）→ 本次不再修改")
            log_audit(f"历史已改价，本次跳过 | 报案号={claim_no} | 零件={part_key} "
                      f"首次改价={previous.get('fixed_at') or ''}")
        elif p.suggested_fixed_price is not None:
            candidates.append(p)

    if not candidates:
        print(f"\n🔹 STEP 7: 无需本次改价"
              + (f"，历史已改价跳过 {len(history_fixed)} 个零件" if history_fixed else ""))
        return 0

    print(f"\n🔹 STEP 7: 自动修复 {len(candidates)} 个超市场价零件的报价列")
    fixed_count = 0
    failed = []
    skipped = []
    for pos, p in enumerate(candidates):
        part_key = _quote_fix_part_key(p)

        new_price = p.suggested_fixed_price
        # 护栏：新报价必须低于平台原报价（报价=定损价，折率<1 时公式结果必然更低）。
        # 若算出来不低于原报价，说明取到的基准价不对，硬写会被平台重算回去，直接跳过。
        old_quote = p.list_price or p.guide_price
        if old_quote and new_price >= old_quote:
            print(f"  [!] #{p.index:02d} {p.code}: 公式结果 ¥{new_price} ≥ 平台原报价 ¥{old_quote}"
                  f" → 不下调，跳过改价")
            p.fixed_price = None
            skipped.append(p)
            continue
        # 安全兜底：公式算出来的报价不能比市场价高（允许 +5% 容忍）
        if p.market_price and new_price > round(p.market_price * 1.05, 0):
            new_price = round(p.market_price * 0.98, 0)
            print(f"      [i] 公式结果高于市场价 5% → 压到市场价 98% = ¥{new_price}")
        p.fixed_price = new_price
        numer = p.list_price or p.guide_price
        denom = p.s_price_value or p.local_price
        ratio = f"{numer}/{denom}" if numer and denom else "?"
        base_label = "S价" if p.s_price_value else "本地价"
        print(f"  🛠️  #{p.index:02d} {p.code}: 报价 = ¥{p.market_price}×({ratio}) = ¥{new_price}"
              f"  ({base_label}¥{denom} 市场价¥{p.market_price})")
        try:
            ok = await _loss_fix_quote_price(page, p, new_price)
        except Exception as exc:
            print(f"      [!] 改价异常（不中断流程）: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
            ok = False
        if ok:
            fixed_count += 1
            if not price_svc.record_claim_quote_fix(
                    claim_no, part_key, new_price, part_name=p.name):
                print(f"      ⚠️ {part_key} 改价成功但历史记录写入失败 → 后续零件暂停自动改价")
                log_audit(f"改价历史写入失败，停止后续改价 | 报案号={claim_no} | 零件={part_key}")
                for rest in candidates[pos + 1:]:
                    rest.fixed_price = None
                failed.extend(candidates[pos + 1:])
                break
        else:
            p.fixed_price = None
            failed.append(p)
        await human_delay()

    new_candidates = len(candidates)
    print(f"  [✓] 本次改价成功 {fixed_count}/{new_candidates} 个零件")
    if history_fixed:
        print(f"  ⏭ 历史已改价跳过 {len(history_fixed)} 个零件（未重复修改）")
    if skipped:
        for p in skipped:
            print(f"      - 无需下调: #{p.index:02d} {p.code} 平台原报价 ¥{p.list_price}")
    if failed:
        for p in failed:
            print(f"      - 未改价: #{p.index:02d} {p.code} ¥{p.local_price}（将以定损价为准）")
        _need_resync_after_fix_failure(context)

    # 点「暂存」（mock #save-draft-btn / 生产 #submitBaseInfo 或文字『暂存』）
    if fixed_count:
        save_ok = False
        for sel in ("#save-draft-btn", "#submitBaseInfo"):
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await safe_click(btn, "定损平台 → 暂存按钮")
                    save_ok = True
                    break
            except Exception:
                continue
        if not save_ok:
            try:
                _, btn = await find_clickable(
                    page, ["button:has-text('暂存')", "a:has-text('暂存')", "input[value='暂存']"],
                    ["暂存"], "定损平台 → 暂存按钮", timeout=5000)
                await safe_click(btn, "定损平台 → 暂存按钮")
                save_ok = True
            except Exception:
                print(f"  ⚠️ 暂存按钮未找到/点击失败，继续提交流程")
        if save_ok:
            await confirm_el_messagebox(page, timeout=5.0)
            await human_delay()

    return fixed_count


# ============================================================
# STEP 8: 定损平台 → 提交 → 诊断对话框 → 提交定损单
# ============================================================
async def _dismiss_submit_failure(page: Page) -> bool:
    """处理「提交失败，请联系管理员」提示框：点确定关掉，返回 True

    生产上提交定损单偶发失败，关掉提示后再点一次通常就能过。
    提示框可能弹在主页面、定损平台 iframe 或新标签页里，这里都扫一遍。
    """
    deadline = time.time() + 4
    while time.time() < deadline:
        for target in [page] + [p for p in page.context.pages if p != page]:
            try:
                if target.is_closed():
                    continue
            except Exception:
                continue
            for frame in target.frames:
                try:
                    body = await frame.inner_text("body")
                except Exception:
                    continue
                if "提交失败" not in body and "请联系管理员" not in body:
                    continue
                print("  ⚠️ 弹出「提交失败，请联系管理员」→ 点确定关闭")
                try:
                    _, ok_btn = await find_clickable(
                        target,
                        ["button:has-text('确定')", ".el-message-box__btns button",
                         "input[value='确定']", "a:has-text('确定')"],
                        ["确定"], "提交失败提示 → 确定", timeout=3000)
                    await safe_click(ok_btn, "提交失败提示 → 确定")
                except Exception as exc:
                    print(f"  ⚠️ 提交失败提示的「确定」未点到: {str(exc).splitlines()[0][:120]}")
                log_audit("提交失败提示：已点确定")
                if target != page:
                    try:
                        await target.close()
                    except Exception:
                        pass
                return True
        await asyncio.sleep(0.4)
    return False


@register_step(desc="定损平台提交定损单 + 弹窗确认", inputs=["parts", "tolerance"])
async def step_loss_submit(page: Page, context: dict, **kwargs) -> None:
    parts = kwargs.get("parts") or context.get("parts") or []
    tolerance = float(kwargs.get("tolerance") or context.get("tolerance") or 0)
    print(f"\n🔹 STEP 8: 定损平台 → 提交定损单")

    # STEP 7 有零件改价失败 → 提交前重新同步一次定损（含确认弹窗），保证单据状态干净
    if context.get("resync_loss"):
        print("  [i] 改价未完全成功 → 先重新执行『同步定损』")
        try:
            await step_loss_sync(page, context)
        except Exception as exc:
            print(f"  [!] 重新同步定损失败（不中断）: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
        await human_delay()

    if not parts:
        print(f"  [i] 该案件无换件 → 无需价格比对，直接提交")
    else:
        over = [p for p in parts if p.within_market is False]
        ok   = [p for p in parts if p.within_market is True]
        print(f"  📊 比对结果: 合规 {len(ok)} 个, 超市场价 {len(over)} 个")
        if over:
            print("  ⚠️ 以下零件本地价超过市场价:")
            for p in over:
                print(f"      - {p.code} {p.name}: 本地价¥{p.local_price} > 市场价¥{p.market_price}")

    # 人工等效：诊断弹窗已被人工点开 → 跳过「提交」按钮，直接点弹窗内「提交定损单」
    # 弹窗可能有多种实现，用多个候选选择器轮询确认
    diag_candidates = [
        SELECTORS_PORTAL["diagnosis_modal"],
        ".el-message-box:visible", ".el-dialog:visible",
        ".modal.in:visible", ".modal.show:visible", ".modal:visible",
        "[role='dialog']:visible",
    ]
    diag_open = False
    for sel in diag_candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                diag_open = True
                print(f"  [i] 诊断弹窗已开（命中 {sel}）")
                break
        except Exception:
            continue

    if diag_open:
        print("  [i] 诊断对话框已弹出（可能人工已点提交）→ 跳过前置点击")
        log_audit("跳过定损提交按钮（诊断对话框已弹出）")
    else:
        try:
            _, submit_btn = await find_clickable(
                page,
                [SELECTORS_PORTAL["submit_loss"],
                 "button:has-text('提交')", "input[value='提交']",
                 "a:has-text('提交')"],
                ["提交", "提交定损"], "定损平台 → 提交按钮", timeout=8000)
            await safe_click(submit_btn, "定损平台 → 提交按钮", timeout=DEFAULT_TIMEOUT)
        except RuntimeError:
            print("  [i] 提交按钮与诊断弹窗均未找到 → 判定定损单可能已人工提交，继续")
            log_audit("跳过定损提交（按钮不存在，可能已人工提交）")
            return
        await human_delay()
        # 轮询等弹窗（不依赖 class 里必须有 "show"）
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            diag_open = False
            for sel in diag_candidates:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        diag_open = True
                        print(f"  [✓] 诊断结果对话框已弹出（命中 {sel}）")
                        break
                except Exception:
                    continue
            if diag_open:
                break
            await asyncio.sleep(0.5)
        if not diag_open:
            print(f"  [!] 提交后 12s 内未检测到诊断弹窗，继续尝试提交定损单")
            await dump_visible_overlays(page)

    # 提交定损单按钮：生产环境可能不是 #submit-loss-order，多候选尝试
    submit_order_candidates = [
        SELECTORS_PORTAL["submit_loss_order"],
        "button:has-text('提交定损单')",
        "button:has-text('提交定损')",
        ".el-message-box button:has-text('确定')",
        ".modal button:has-text('确定')",
    ]
    submit_order = None
    for sel in submit_order_candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                submit_order = loc
                print(f"  [i] 提交定损单按钮命中 {sel}")
                break
        except Exception:
            continue
    if submit_order is None:
        submit_order = page.locator(SELECTORS_PORTAL["submit_loss_order"]).first

    submitted = False
    for attempt in range(1, 4):
        try:
            await safe_click(submit_order, "诊断对话框 → 提交定损单")
        except Exception as exc:
            print(f"  [!] 提交定损单按钮点击失败：{exc}")
            break
        await human_delay()
        if await confirm_el_messagebox(page, timeout=8.0):
            print("  [✓] 已点击「提交成功/提示」框的「确定」")
            log_audit("提交定损单结果提示框：已点确定")
        if not await _dismiss_submit_failure(page):
            submitted = True
            break
        print(f"  [i] 第 {attempt} 次提交被系统拒绝 → 关闭提示后重试提交定损单")
        log_audit(f"提交定损单失败，重试第 {attempt + 1} 次")
        # 重试前：确保弹窗还开着；关了就重新点提交按钮
        still_open = False
        for sel in diag_candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    still_open = True; break
            except Exception:
                continue
        if not still_open:
            try:
                _, submit_btn = await find_clickable(
                    page,
                    [SELECTORS_PORTAL["submit_loss"], "button:has-text('提交')"],
                    ["提交", "提交定损"], "定损平台 → 提交按钮", timeout=4000)
                await safe_click(submit_btn, "重新打开诊断对话框")
                await human_delay()
            except Exception:
                pass
    if submitted:
        print(f"  [✓] 定损单已提交 → 回到报价处理流程")
    else:
        print("  ⚠️ 连续 3 次提交定损单失败，请人工确认")
        log_audit("提交定损单连续 3 次失败（已点确定关闭提示）")


# ============================================================
# STEP 9: 报价处理 → 报价完成 → 提交任务
# ============================================================
# 生产实测（2026-11-05）提交对话框 DOM：
#   <div role="dialog" aria-modal="true" aria-label="报价任务提交" class="el-dialog"
#        style="margin-top: 15vh; width: 880px;">
#     ...
#     <button type="button" class="el-button el-button--primary is-round">
#       <span>提交任务</span>
#     </button>
#   </div>
#（旧代码找的 #quote-submit-modal / #confirm-quote-submit 只在 mock 里存在 → 生产永远点不到）
_QUOTE_SUBMIT_DIALOG_SELS = (
    '[role="dialog"][aria-label*="报价任务提交"]',
    '.el-dialog[aria-label*="报价任务提交"]',
    '.el-dialog:has-text("报价任务提交")',
    '#quote-submit-modal',
)


# ---------- 保险金额类提示框：报价页点『提交』后弹出（Element UI message box） ----------
# 生产实测（标题=提示，按钮=取消 / 确定）：
#   ① 报价总额超过保险金额(【车损险】【14615.6】元)的70%,是否重新报价?          → 点『取消』（不重新报价），继续提交
#   ② 标的车报价"合计"金额大于车辆保险金额(【车损险】【21580】元)请确定是否提交下一节点! → 点『确定』，继续提交
_INSURANCE_BOX_SELECTORS = [
    ".el-message-box:visible",
    ".el-message-box__wrapper:visible",
    ".modal.in:visible", ".modal:visible", ".modal-dialog:visible",
    "[role='dialog']:visible", "[role='alertdialog']:visible",
    ".layui-layer:visible", ".message-box:visible", ".dialog:visible",
]


async def handle_insurance_amount_dialogs(page: Page, timeout: float = 8.0) -> int:
    """处理『报价完成 / 提交上级』点『提交』后弹出的两个保险金额提示框

    ① 报价总额超过保险金额(…元)的70%,是否重新报价  → 点『取消』（不重新报价）
    ② 标的车报价"合计"金额大于车辆保险金额(…元)请确定是否提交下一节点! → 点『确定』（继续提交）

    两个框可能连续弹出，这里循环处理直到 1.2 秒内无新提示框。
    返回处理掉的提示框数量（0 = 没弹）。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    handled = 0
    idle_since = None

    while time.monotonic() < deadline:
        acted = False
        for pg in list(dict.fromkeys([page, *page.context.pages])):
            for fr in pg.frames:
                for sel in _INSURANCE_BOX_SELECTORS:
                    try:
                        box = fr.locator(sel).last
                        if await box.count() == 0 or not await box.is_visible():
                            continue

                        # 优先读 message 区域，避免标题/按钮文案干扰关键字匹配
                        text = ""
                        for msg_sel in (".el-message-box__message", ".el-message-box__content",
                                        ".modal-body", ".layui-layer-content"):
                            m = box.locator(msg_sel).first
                            if await m.count() > 0:
                                text = await m.inner_text() or ""
                                if text.strip():
                                    break
                        if not text.strip():
                            text = await box.inner_text() or ""
                        flat = "".join(text.split())

                        # ① 报价总额超过保险金额(…元)的70%,是否重新报价 → 点『取消』
                        #    关键字按用户明确要求拆成两段匹配，避免误伤其他含"是否重新报价"的弹窗
                        #    忽略中间的个案差异（车型、金额、保险类型均可不同）
                        if "报价总额超过保险金额" in flat and "是否重新报价" in flat:
                            btn_candidates = ("取消",)
                            kind = "报价总额超保险金额 → 取消（不重新报价）"
                        # ② 标的车报价"合计"金额大于车辆保险金额(…元)请确定是否提交下一节点! → 点『确定』
                        elif ("标的车报价" in flat or "合计金额" in flat) and "保险金额" in flat and "提交下一节点" in flat:
                            btn_candidates = ("确定", "确 定")
                            kind = "标的车报价合计大于保险金额 → 确定（继续提交）"
                        else:
                            continue

                        clicked = False
                        for bt in btn_candidates:
                            btn = box.locator(
                                f"button:has-text('{bt}'), .el-button:has-text('{bt}'), "
                                f"[role='button']:has-text('{bt}'), a:has-text('{bt}')").first
                            if await btn.count() == 0 or not await btn.is_visible():
                                continue
                            await btn.click(timeout=4000)
                            clicked = True
                            break
                        if not clicked:
                            continue

                        handled += 1
                        acted = True
                        print(f"  [✓] 保险金额提示框：{kind}")
                        log_audit(f"保险金额提示框：{kind}")
                        try:
                            await box.wait_for(state="hidden", timeout=5000)
                        except Exception:
                            await asyncio.sleep(0.5)
                        await human_delay()
                        break
                    except Exception:
                        continue
                if acted:
                    break
            if acted:
                break

        if acted:
            idle_since = None
            continue
        if idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 1.2:
            break
        await asyncio.sleep(0.3)

    return handled


# ---------- 已本地化换件超 5 万元提示框 → 点确认，返回 True 表示触发了分支 ----------
# 生产实测（2026-11-17）：换件金额超大（≥19万）时，提交上级/报价完成后会弹此框
# 内容：已本地化换件的单项最大金额超过5万元,请提交远程操作平台处理!
# 按钮：确认（不是"取消"，点完必须重新走"分中心报价"分支）
async def _handle_localized_part_50k_dialog(page: Page, timeout: float = 5.0) -> bool:
    """处理『请提交远程操作平台处理』类提示框（含已本地化换件超 5 万）

    生产实测文案：已本地化换件的单项最大金额超过5万元,请提交远程操作平台处理!
    统一关键字：**请提交远程操作平台处理**（忽略金额/换件数等个案差异）
    点『确认』（不是取消），返回 True 表示需改走分中心报价分支。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pg in list(dict.fromkeys([page, *page.context.pages])):
            for fr in pg.frames:
                for sel in _INSURANCE_BOX_SELECTORS:
                    try:
                        box = fr.locator(sel).last
                        if await box.count() == 0 or not await box.is_visible():
                            continue
                        text = ""
                        for msg_sel in (".el-message-box__message", ".el-message-box__content",
                                        ".modal-body", ".layui-layer-content"):
                            m = box.locator(msg_sel).first
                            if await m.count() > 0:
                                text = await m.inner_text() or ""
                                if text.strip():
                                    break
                        if not text.strip():
                            text = await box.inner_text() or ""
                        flat = "".join(text.split())
                        # 统一关键字：请提交远程操作平台处理（含“已本地化换件超5万元”文案）
                        if "请提交远程操作平台处理" not in flat:
                            continue
                        for bt in ("确认", "确定", "确 定"):
                            btn = box.locator(
                                f"button:has-text('{bt}'), .el-button:has-text('{bt}'), "
                                f"[role='button']:has-text('{bt}'), a:has-text('{bt}')").first
                            if await btn.count() == 0 or not await btn.is_visible():
                                continue
                            await btn.click(timeout=4000)
                            print(f"  [✓] 『请提交远程操作平台处理』提示框 → 点『{bt}』，需改走分中心报价分支")
                            log_audit("请提交远程操作平台处理提示框：点确认 → 改走分中心报价分支")
                            try:
                                await box.wait_for(state="hidden", timeout=5000)
                            except Exception:
                                await asyncio.sleep(0.5)
                            await human_delay()
                            return True
                    except Exception:
                        continue
        await asyncio.sleep(0.3)
    return False


async def _quote_wait_submit_dialog(page: Page, timeout: float = 12.0):
    """等『报价任务提交』对话框出现 → 返回其 locator；超时/不可见返回 None"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for sel in _QUOTE_SUBMIT_DIALOG_SELS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.3)


async def _quote_click_submit_task(page: Page, dialog) -> bool:
    """点对话框里的『提交任务』按钮

    精简后的交互：
      1. 先清理一轮上层遮挡弹窗（el-message-box__wrapper 等）
      2. 尝试点击
      3. 若仍被 pointer events 拦截 → 再清一轮 + force 兜底
    """
    btn_sels = ["button:has-text('提交任务')", ".el-button:has-text('提交任务')",
                "#confirm-quote-submit"]

    def _log_cleared(n: int):
        if n > 0:
            print(f"  [i] 提交任务前清掉 {n} 个上层遮挡弹窗")
            log_audit(f"提交任务前清理遮挡弹窗: {n} 个")

    # 第一轮：清理 + 点击
    try:
        cleared = await dismiss_all_modals(page, max_rounds=2, per_timeout=1.5)
        _log_cleared(cleared)
        if cleared > 0:
            await asyncio.sleep(0.3)
    except Exception:
        pass

    clicked = await _try_click_submit_btn(page, dialog, btn_sels)
    if clicked:
        return True

    # 第二轮：force 模式硬点所有候选
    for sel in btn_sels:
        try:
            btn = dialog.locator(sel).first
            if await btn.count() == 0:
                continue
            await btn.click(timeout=3000, force=True)
            print(f"  [✓] 报价任务提交对话框 → 点击『提交任务』(force)")
            log_audit("报价任务提交对话框 → 提交任务(force)")
            return True
        except Exception:
            continue

    # 最终兜底：整页 force 点击
    try:
        btn = page.locator("button:visible:has-text('提交任务')").last
        if await btn.count() > 0:
            await btn.click(timeout=3000, force=True)
            print(f"  [✓] 报价任务提交 → 提交任务（页面兜底 + force）")
            log_audit("报价任务提交 → 提交任务（页面兜底 force）")
            return True
    except Exception:
        pass

    print("  [!] 未能点击『提交任务』按钮")
    return False


async def _try_click_submit_btn(page: Page, dialog, btn_sels: list) -> bool:
    """尝试在对话框里点击『提交任务』按钮，成功返回 True"""
    for sel in btn_sels:
        try:
            btn = dialog.locator(sel).first
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            await btn.click(timeout=6000)
            print(f"  [✓] 报价任务提交对话框 → 点击『提交任务』({sel})")
            log_audit("报价任务提交对话框 → 提交任务")
            return True
        except Exception as exc:
            err_msg = str(exc)
            if "intercepts pointer events" in err_msg:
                print(f"  [!] 提交任务按钮被遮挡（{sel}），将清理上层弹窗后重试")
            else:
                print(f"  [!] 提交任务按钮点击失败（{sel}）: {exc}")
    return False


_JS_QUOTE_SUMMARY_HEXUN = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, '');
  const isHead = (e) => {
    const t = norm(e.innerText || e.textContent);
    if (!(t === '金额汇总' || t === '金额汇总:' || t === '金额汇总：')) return false;
    // 只认真正渲染出来的块，避免别处隐藏 tab 里的同名标题干扰
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const heads = Array.from(document.querySelectorAll('div,span,h3,h4,h5,p,th,td,label'))
    .filter(isHead);
  for (const h of heads) {
    let box = h.parentElement;
    for (let i = 0; i < 6 && box; i++) {
      const t = box.innerText || '';
      if (t.indexOf('定损汇总') >= 0 || t.indexOf('报价汇总') >= 0) {
        return {found: true, has_hexun: t.indexOf('核损汇总') >= 0, text: t.slice(0, 200)};
      }
      box = box.parentElement;
    }
  }
  const body = document.body.innerText || '';
  return {found: body.indexOf('金额汇总') >= 0, has_hexun: body.indexOf('核损汇总') >= 0, text: ''};
}
"""


async def _quote_summary_has_hexun(page: Page) -> bool:
    """报价处理页『金额汇总』块里是否有「核损汇总」行（没有 → 走一级车辆定损路径）"""
    try:
        info = await page.evaluate(_JS_QUOTE_SUMMARY_HEXUN)
    except Exception:
        return True
    if not info or not info.get("found"):
        print("  [i] 未找到『金额汇总』块 → 默认按无核损汇总处理")
        return False
    return bool(info.get("has_hexun"))


# ============================================================
# 报价汇总『换件金额』+ 当前任务环节：> 4 万 → 一级报价无权限 → 业务动作『提交上级』
# 生产表头：换件金额 | 维修金额（工时费）| 辅料费（材料费）| 管理费 | 合计 | 直供换件金额
# 生产行：  定损汇总 / 报价汇总 / 合计（大写）
# ============================================================
_JS_QUOTE_PART_AMOUNT = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, '');
  const num = (s) => {
    const m = String(s || '').replace(/[,\s¥￥]/g, '').match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : null;
  };
  // 单元格文本：输入框取 value（生产『修理金额』列是 disabled input，innerText 为空）
  const cellText = (el) => {
    if (!el) return '';
    const inp = el.querySelector('input');
    if (inp && (inp.value || '').trim()) return inp.value.trim();
    return (el.innerText || '').trim();
  };
  const pick = (heads, findRow, box) => {
    const idx = heads.findIndex(h => clean(h).includes('换件金额') && clean(h).indexOf('直供') < 0);
    if (idx < 0) return null;
    for (const row of findRow(box)) {
      const cells = Array.from(row.children);
      if (!cells.length) continue;
      const label = clean(cellText(cells[0]));
      if (label.indexOf('报价汇总') < 0) continue;
      const cell = cells[idx];
      return {found: true, amount: num(cellText(cell)), label: label,
              colIndex: idx, text: cellText(cell).trim(), heads: heads};
    }
    return null;
  };

  // ① 生产：金额汇总用 Vue div 网格（.cxlp-thead 表头 + .cxlp-tbody 行），不是 <table>
  for (const headRow of Array.from(document.querySelectorAll('.cxlp-thead'))) {
    const heads = Array.from(headRow.children).map(cellText);
    if (!heads.some(h => clean(h).includes('换件金额'))) continue;
    const box = headRow.parentElement || headRow;
    const hit = pick(heads, (b) => Array.from(b.querySelectorAll('.cxlp-tbody')), box);
    if (hit) return hit;
  }

  // ② 兜底：老页面 <table> 结构
  for (const t of Array.from(document.querySelectorAll('table'))) {
    const heads = Array.from(t.querySelectorAll('thead th, thead td'))
      .map(h => clean(h.innerText));
    if (!heads.some(h => h.includes('换件金额'))) continue;
    for (const tr of Array.from(t.querySelectorAll('tbody tr'))) {
      const cells = Array.from(tr.children);
      if (!cells.length) continue;
      const label = clean(cells[0].innerText);
      if (label.indexOf('报价汇总') < 0) continue;
      // 首列是行标题（th）时 cells 比表头多 1 列 → 列号右移
      const offset = Math.max(0, cells.length - heads.length);
      const cell = cells[idxOf(heads) + offset];
      return {found: true, amount: num(cell ? cell.innerText : ''),
              label: label, colIndex: idxOf(heads), text: (cell ? cell.innerText : '').trim(),
              heads: heads};
    }
  }
  function idxOf(heads) {
    return heads.findIndex(h => h.includes('换件金额') && h.indexOf('直供') < 0);
  }
  return {found: false, amount: null};
}
"""

_JS_QUOTE_TASK_STAGE = r"""
() => {
  const items = Array.from(document.querySelectorAll('.el-form-item'));
  for (const it of items) {
    const lab = it.querySelector('label');
    const lt = (lab ? lab.innerText : '').replace(/\s+/g, '');
    if (lt.indexOf('当前任务环节') < 0) continue;
    const inp = it.querySelector('input');
    if (inp) return {ok: true, value: (inp.value || '').trim()};
    return {ok: true, value: (it.innerText || '').replace(lt, '').trim()};
  }
  return {ok: false, value: ''};
}
"""

# 传给「提交上级」子流程的可配置参数（流程编辑器节点里可覆盖）
_TRANSFER_KWARG_KEYS = (
    "submit_path", "group_option", "group_code", "group_key",
    "group_exclude", "opinion", "staff_code",
)


def _transfer_kwargs(kwargs: dict) -> dict:
    return {k: v for k, v in (kwargs or {}).items()
            if k in _TRANSFER_KWARG_KEYS and v not in (None, "")}


async def quote_task_stage(page: Page) -> str:
    """报价任务提交弹窗里的『当前任务环节』（一级报价 / 九级报价）"""
    try:
        info = await page.evaluate(_JS_QUOTE_TASK_STAGE)
    except Exception:
        return ""
    return str((info or {}).get("value") or "") if isinstance(info, dict) else ""


async def quote_needs_superior(page: Page, threshold: float) -> tuple:
    """报价汇总『换件金额』是否超过一级报价权限 → (是否提交上级, 金额, 当前任务环节)

    已处于『九级报价』环节说明本单已经提过上级并被转回，不能再重复提交。
    """
    info = None
    try:
        info = await page.evaluate(_JS_QUOTE_PART_AMOUNT)
    except Exception:
        info = None
    amount = None
    if isinstance(info, dict) and info.get("found") and info.get("amount") is not None:
        amount = float(info["amount"])
    stage = await quote_task_stage(page)
    if amount is None:
        print("  [i] 未读到『金额汇总 → 报价汇总 → 换件金额』→ 不做高额判断")
        return False, None, stage
    if threshold <= 0:
        return False, amount, stage
    if "九级报价" in stage:
        print(f"  [i] 当前任务环节={stage} → 已提过上级，不再重复提交")
        return False, amount, stage
    return amount > threshold, amount, stage


# ============================================================
# 金额汇总一致性：报价汇总 vs 定损汇总 的『换件金额』
# 两者不一致（改价 / 取消直供价后常见）→ 提交路径必须选『车辆定损』，
# 回定损环节把换件金额同步后再提交，否则平台会以金额不符拒绝。
# ============================================================
_JS_QUOTE_AMOUNT_COMPARE = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, '');
  const num = (s) => {
    const m = String(s || '').replace(/[,\s¥￥]/g, '').match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : null;
  };
  const cellText = (el) => {
    if (!el) return '';
    const inp = el.querySelector('input');
    if (inp && (inp.value || '').trim()) return inp.value.trim();
    return (el.innerText || '').trim();
  };
  const idxOf = (heads) => heads.findIndex(
      h => clean(h).includes('换件金额') && clean(h).indexOf('直供') < 0);

  // ① 生产：金额汇总用 Vue div 网格（.cxlp-thead 表头 + .cxlp-tbody 行）
  for (const headRow of Array.from(document.querySelectorAll('.cxlp-thead'))) {
    const heads = Array.from(headRow.children).map(cellText);
    const idx = idxOf(heads);
    if (idx < 0) continue;
    const box = headRow.parentElement || headRow;
    const out = {quote: null, loss: null};
    for (const row of Array.from(box.querySelectorAll('.cxlp-tbody'))) {
      const cells = Array.from(row.children);
      if (!cells.length) continue;
      const label = clean(cellText(cells[0]));
      const amt = num(cellText(cells[idx]));
      if (label.indexOf('报价汇总') >= 0) out.quote = amt;
      else if (label.indexOf('定损汇总') >= 0) out.loss = amt;
    }
    if (out.quote !== null || out.loss !== null) {
      return {found: true, quote_amount: out.quote, loss_amount: out.loss};
    }
  }

  // ② 兜底：老页面 <table> 结构（首列是行标题 th 时列号右移）
  for (const t of Array.from(document.querySelectorAll('table'))) {
    const heads = Array.from(t.querySelectorAll('thead th, thead td'))
      .map(h => clean(h.innerText));
    const idx = idxOf(heads);
    if (idx < 0) continue;
    const out = {quote: null, loss: null};
    for (const tr of Array.from(t.querySelectorAll('tbody tr'))) {
      const cells = Array.from(tr.children);
      if (!cells.length) continue;
      const label = clean(cellText(cells[0]));
      const offset = Math.max(0, cells.length - heads.length);
      const amt = num(cellText(cells[idx + offset]));
      if (label.indexOf('报价汇总') >= 0) out.quote = amt;
      else if (label.indexOf('定损汇总') >= 0) out.loss = amt;
    }
    if (out.quote !== null || out.loss !== null) {
      return {found: true, quote_amount: out.quote, loss_amount: out.loss};
    }
  }
  return {found: false};
}
"""


async def quote_part_amount_compare(page: Page) -> tuple:
    """读取金额汇总里 定损汇总 / 报价汇总 的『换件金额』→ (定损, 报价)

    任一项读不到时返回 None；两项都读到时调用方自行比较。
    """
    try:
        info = await page.evaluate(_JS_QUOTE_AMOUNT_COMPARE)
    except Exception:
        return None, None
    if not isinstance(info, dict) or not info.get("found"):
        return None, None

    def _f(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    return _f(info.get("loss_amount")), _f(info.get("quote_amount"))


async def _wait_no_loading(page: Page, timeout: float = 10.0) -> None:
    """等 Element UI loading 遮罩消失

    生产问题（RDAA2026110000N0447365，2026-11-17）：
    报价任务提交弹窗刚出现时带 .el-loading-mask，遮罩未消失前点击全部被拦截，
    Playwright 直接抛 15s Timeout（日志：提交路径下拉打不开：TimeoutError）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await page.locator(".el-loading-mask:visible").count() == 0:
                return
        except Exception:
            return
        await asyncio.sleep(0.3)


_JS_MARK_FORM_FIELD = r"""
([labels, token]) => {
  const norm = (s) => (s || '').replace(/\s+/g, '').replace(/[:：]/g, '');
  const want = (labels || []).map(norm);
  document.querySelectorAll('[data-bot-field="' + token + '"]')
      .forEach(e => e.removeAttribute('data-bot-field'));
  const labelEls = Array.from(document.querySelectorAll(
      '.el-form-item__label, label, .el-form-item label'));
  const mark = (le) => {
    const t = norm(le.innerText || le.textContent);
    // ① 先找 label 后面的兄弟节点（Element UI 标准结构：label + content）
    let cand = null;
    for (let n = le.nextElementSibling; n && !cand; n = n.nextElementSibling) {
      if (n.querySelector) cand = n.querySelector('.el-select, input, textarea');
    }
    // ② 再退回所在 form-item 内查找
    if (!cand) {
      const box = le.closest('.el-form-item') || le.parentElement;
      if (box) cand = box.querySelector('.el-select, input, textarea');
    }
    if (!cand) return '';
    // 直接标记控件本身（而非整行 form-item），避免同一行多个 el-select 时点错
    const ctrl = cand.closest('.el-select') || cand;
    ctrl.setAttribute('data-bot-field', token);
    return t;
  };
  // 第一轮：标签文字完全相等（优先，避免「所属组别」误命中「组别」）
  for (const le of labelEls) {
    if (!want.includes(norm(le.innerText || le.textContent))) continue;
    const got = mark(le);
    if (got) return {found: true, label: got};
  }
  // 第二轮：包含匹配（如「提交路径选择（必填）」）
  for (const le of labelEls) {
    const t = norm(le.innerText || le.textContent);
    if (!want.some(w => w && t.includes(w))) continue;
    const got = mark(le);
    if (got) return {found: true, label: got};
  }
  return {found: false};
}
"""


async def _quote_field_scope(page: Page, labels: Sequence[str]):
    """按 Element UI 表单「标签文字完全相等」定位表单项（提交路径 / 组别代码 / 人员代码）

    生产『报价任务提交』弹窗里同一行有多个 el-select，用 :has-text 会把整行容器
    都匹配上，导致点到『当前任务环节』而不是『提交路径选择』→ 下拉永远打不开、
    路径停在系统默认『二级报价』。这里改为精确比对 label 文本。

    每个字段用独立 token 标记，避免先后解析「组别代码」「人员代码」时互相覆盖。
    """
    token = str(labels[0])
    try:
        info = await page.evaluate(_JS_MARK_FORM_FIELD, [list(labels), token])
    except Exception:
        info = None
    if isinstance(info, dict) and info.get("found"):
        loc = page.locator(f'[data-bot-field="{token}"]').first
        if await loc.count() > 0:
            return loc
    return None


_JS_SUBMIT_PATH_CANDIDATES = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, '').replace(/[:：]/g, '');
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  // 只在可见的『报价任务提交』弹窗里找；找不到再退回整页
  const dlgs = Array.from(document.querySelectorAll('.el-dialog'));
  let dlg = dlgs.find(d => vis(d) && (d.innerText || '').indexOf('报价任务提交') >= 0);
  if (!dlg) dlg = document.body;
  document.querySelectorAll('[data-bot-path-cand]')
      .forEach(e => e.removeAttribute('data-bot-path-cand'));
  const out = [];
  Array.from(dlg.querySelectorAll('.el-select')).forEach((el, i) => {
    if (!vis(el)) return;
    const item = el.closest('.el-form-item');
    let label = '';
    if (item) {
      const lab = item.querySelector('.el-form-item__label, label');
      if (lab && !lab.contains(el)) label = norm(lab.innerText || lab.textContent);
    }
    const inp = el.querySelector('input');
    let value = '';
    if (inp) value = (inp.value || '').trim();
    const disabled = el.classList.contains('is-disabled')
        || (inp && (inp.disabled === true));
    el.setAttribute('data-bot-path-cand', String(i));
    out.push({i: i, label: label, value: value, disabled: !!disabled});
  });
  return out;
}
"""


async def _dump_submit_path_failure(page: Page, dialog, target: str, candidates) -> None:
    """提交路径选不中时，把弹窗结构 + 可见下拉项 + 页面快照写到日志目录，便于离线定位"""
    lines = [f"目标提交路径：{target}", f"候选控件数：{len(candidates)}"]
    try:
        txt = await dialog.inner_text(timeout=2000)
        lines.append("对话框文本：\n" + txt[:2500])
    except Exception as exc:
        lines.append(f"读取对话框文本失败：{type(exc).__name__}")
    try:
        items = await page.evaluate(_JS_SUBMIT_PATH_CANDIDATES)
        lines.append("弹窗内 el-select：" + repr(items)[:2000])
    except Exception as exc:
        lines.append(f"读取 el-select 失败：{type(exc).__name__}")
    try:
        seen = await page.evaluate(
            r"""() => {
              const vis = el => {
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
              };
              const out = [];
              for (const d of Array.from(
                       document.querySelectorAll('.el-select-dropdown')).filter(vis)) {
                for (const it of d.querySelectorAll('.el-select-dropdown__item')) {
                  const t = (it.innerText || '').trim();
                  if (t) out.push(t);
                }
              }
              return out;
            }""")
        lines.append("当前可见下拉项：" + repr(seen)[:2000])
    except Exception:
        pass
    msg = "\n".join(lines)
    print("  [诊断] 提交路径选择失败：\n" + msg[:1500])
    log_audit("提交路径选择失败诊断：\n" + msg)
    try:
        from bot.logging_system import get_log_dir
        outdir = get_log_dir()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            html_path = outdir / f"submit_path_fail_{stamp}.html"
            html_path.write_text(await page.content(), encoding="utf-8")
            print(f"  [诊断] 页面快照：{html_path}")
        except Exception:
            pass
        try:
            png_path = outdir / f"submit_path_fail_{stamp}.png"
            await page.screenshot(path=str(png_path), full_page=False)
            print(f"  [诊断] 截图：{png_path}")
        except Exception:
            pass
    except Exception:
        pass


async def _quote_pick_submit_path(page: Page, dialog, target: str) -> bool:
    """『报价任务提交』对话框 → 提交路径选择下拉 → 选 target（如 九级报价）

    生产实测（2026-11-17 RDAA2026110000N0483918）：
      · 同一行有『当前任务环节』『提交路径选择』两个 el-select，按 :has-text 会
        匹配到整行，点到的是只读的『当前任务环节』→ 下拉打不开，路径停在二级报价；
      · 下拉共 9+ 项，『九级报价』在最底部，初始视口看不到；
      · 弹窗刚出现时常带 loading 遮罩，直接 click 会 15s 超时。
    因此：按 label 精确定位表单项 → 等遮罩消失 → 打开下拉 → 用 JS 按文本完全
    相等直接点击（不依赖视口/滚动）→ 回读 input.value 校验；失败再滚动兜底。
    """
    candidates: list[tuple[str, object]] = []
    scope = await _quote_field_scope(page, ("提交路径选择", "提交路径"))
    if scope is not None:
        candidates.append(("label=提交路径", scope))

    # 按弹窗内全部 el-select 的「标签 + 当前值」建候选：
    # label 定位失败时，仍能按『当前值=二级报价』等特征找回正确的提交路径控件。
    cand_items = None
    try:
        cand_items = await page.evaluate(_JS_SUBMIT_PATH_CANDIDATES)
    except Exception:
        cand_items = None
    if isinstance(cand_items, list) and cand_items:
        def _score(it: dict):
            label = str(it.get("label") or "")
            val = str(it.get("value") or "")
            if it.get("disabled"):
                return None
            if "当前任务环节" in label or "任务环节" in label:
                return None                      # 只读的环境字段，绝不能点
            if "提交路径" in label:
                return 0
            if val == "二级报价":
                return 1                         # 生产默认值 → 基本可确认是提交路径
            if any(v and v in val for v in
                   ("九级报价", "分中心报价", "车辆定损", "一级车辆核损")):
                return 2
            return 3
        scored = []
        for it in cand_items:
            s = _score(it)
            if s is not None:
                scored.append((s, it))
        scored.sort(key=lambda x: (x[0], x[1].get("i", 0)))
        desc = [f"#{it.get('i')}「{it.get('label') or '无标签'}」={it.get('value') or '空'}"
                for _, it in scored]
        if desc:
            print(f"  [i] 弹窗内 el-select 候选：{desc}")
            log_audit(f"报价任务提交弹窗 el-select 候选：{desc}")
        for _, it in scored[:4]:
            loc = page.locator(f'[data-bot-path-cand="{it.get("i")}"]').first
            try:
                if await loc.count() > 0:
                    candidates.append(
                        (f"select#{it.get('i')}「{it.get('label') or '无标签'}」", loc))
            except Exception:
                continue

    if not candidates:
        fallback = page.locator(SELECTORS_PORTAL["quote_task_path_input"]).first
        if await fallback.count() > 0:
            candidates.append(("旧选择器", fallback))
    if not candidates:
        print("  [!] 未找到『提交路径选择』控件")
        await _dump_submit_path_failure(page, dialog, target, candidates)
        return False

    async def _trigger(scope):
        """下拉触发器：scope 可能是 .el-select 控件本身，也可能是整行 form-item"""
        try:
            el = scope.locator(".el-select").first
            if await el.count() > 0:
                return el
        except Exception:
            pass
        return scope

    async def _current(scope) -> str:
        """回读提交路径当前值（只读该控件内的 input，避免读到其它字段）"""
        try:
            inp = scope.locator("input").first
            if await inp.count() > 0:
                return (await inp.input_value() or "").strip()
        except Exception:
            pass
        try:
            if (await scope.evaluate("el => el.tagName.toLowerCase()")) == "input":
                return (await scope.input_value() or "").strip()
        except Exception:
            pass
        return ""

    async def _open(scope) -> bool:
        trigger = await _trigger(scope)
        for _ in range(3):
            await _wait_no_loading(page, timeout=6.0)
            for how in ("normal", "force", "inner_force", "js"):
                try:
                    if how == "normal":
                        await trigger.click(timeout=4000)
                    elif how == "force":
                        await trigger.click(timeout=3000, force=True)
                    elif how == "inner_force":
                        await trigger.locator("input").first.click(timeout=2500, force=True)
                    else:
                        # 遮罩/浮层挡住时，直接触发 DOM click
                        await trigger.evaluate("el => el.click()")
                except Exception:
                    continue
                await asyncio.sleep(0.5)
                try:
                    if await page.locator(".el-select-dropdown:visible").count() > 0:
                        return True
                except Exception:
                    pass
        return False

    async def _pick_by_js(text: str) -> bool:
        """在可见下拉里按文本完全相等直接 JS 点击（含滚动兜底），返回是否点中"""
        try:
            info = await page.evaluate(
                r"""
                (want) => {
                  const norm = s => (s || '').replace(/\s+/g, '');
                  const vis = el => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden'
                           && st.display !== 'none';
                  };
                  const drops = Array.from(document.querySelectorAll('.el-select-dropdown'))
                      .filter(vis);
                  for (const d of drops) {
                    const items = Array.from(d.querySelectorAll('.el-select-dropdown__item'));
                    for (const it of items) {
                      if (norm(it.innerText || it.textContent) !== want) continue;
                      it.scrollIntoView({block: 'center'});
                      it.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                      it.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                      it.click();
                      return {ok: true, text: (it.innerText || '').trim()};
                    }
                  }
                  // 没找到 → 把每个可见下拉滚到底，返回当前项文本方便排查
                  const seen = [];
                  for (const d of drops) {
                    const wrap = d.querySelector('.el-scrollbar__wrap') || d;
                    for (let i = 0; i < 6; i++) {
                      wrap.scrollTop = wrap.scrollHeight;
                    }
                    d.querySelectorAll('.el-select-dropdown__item').forEach(x => {
                      const t = (x.innerText || '').trim();
                      if (t && !seen.includes(t)) seen.push(t);
                    });
                  }
                  return {ok: false, seen: seen.slice(0, 40)};
                }
                """,
                "".join(text.split()),
            )
        except Exception as exc:
            print(f"  [!] JS 选项点击异常：{type(exc).__name__}")
            return False
        if isinstance(info, dict) and info.get("ok"):
            return True
        if isinstance(info, dict) and info.get("seen"):
            print(f"  [i] 下拉可见项：{info['seen'][:20]}")
        return False

    async def _pick_scroll(text: str) -> bool:
        """滚动兜底：逐屏滚动，文本完全相等才点"""
        dropdown = page.locator(".el-select-dropdown:visible").last
        for _ in range(14):
            try:
                items = dropdown.locator(".el-select-dropdown__item")
                n = await items.count()
            except Exception:
                n = 0
            for i in range(n):
                it = items.nth(i)
                try:
                    if not await it.is_visible():
                        continue
                    t = ((await it.inner_text()) or "").strip()
                except Exception:
                    continue
                if t != text:
                    continue
                try:
                    await it.click(timeout=3000)
                except Exception:
                    try:
                        await it.click(timeout=2000, force=True)
                    except Exception:
                        continue
                return True
            moved = False
            for sel in (".el-scrollbar__wrap", ".el-scrollbar__view", ""):
                try:
                    node = dropdown.locator(sel).first if sel else dropdown
                    if sel and await node.count() == 0:
                        continue
                    await node.evaluate(
                        "el => { el.scrollTop = el.scrollTop + Math.max(80, el.clientHeight * 0.8); }")
                    moved = True
                    break
                except Exception:
                    continue
            if not moved:
                break
            await asyncio.sleep(0.3)
        return False

    async def _pick_by_keyboard(text: str) -> bool:
        """键盘兜底：↓ 逐项高亮，高亮项文字与目标完全相等时回车"""
        for _ in range(45):
            try:
                await page.keyboard.press("ArrowDown")
            except Exception:
                return False
            await asyncio.sleep(0.08)
            try:
                hovered = await page.evaluate(
                    r"""() => {
                      const vis = el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 2 && r.height > 2;
                      };
                      const drops = Array.from(
                        document.querySelectorAll('.el-select-dropdown')).filter(vis);
                      for (const d of drops) {
                        const it = d.querySelector(
                          '.el-select-dropdown__item.hover, .el-select-dropdown__item.hovering');
                        if (it) return (it.innerText || '').trim();
                      }
                      return '';
                    }""")
            except Exception:
                hovered = ""
            if (hovered or "").replace(" ", "") == (text or "").replace(" ", ""):
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    return False
                return True
        return False

    async def _pick_on(scope) -> bool:
        cur = await _current(scope)
        if target in cur:
            print(f"  [i] 提交路径已是『{target}』，无需修改")
            return True

        # ① 原生 select 兜底
        try:
            native = scope.locator("select").first
            if await native.count() > 0:
                await native.select_option(label=target)
                print(f"  [✓] 提交路径选择 → {target}（原生 select）")
                return True
        except Exception:
            pass

        # ② Element UI 下拉：打开 → JS 精确点击 → 滚动 → 键盘，最后回读校验
        for attempt in range(1, 3):
            if not await _open(scope):
                print(f"  [!] 提交路径下拉打不开（第{attempt}次）")
                continue
            ok = await _pick_by_js(target)
            if not ok:
                ok = await _pick_scroll(target)
            if not ok:
                ok = await _pick_by_keyboard(target)
            if not ok:
                print(f"  [!] 下拉里没找到『{target}』（第{attempt}次）")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.4)
                continue
            await asyncio.sleep(0.8)
            cur = await _current(scope)
            if target in cur:
                print(f"  [✓] 提交路径选择 → {cur}")
                log_audit(f"提交路径选择 → {cur}")
                try:
                    await page.locator(".el-select-dropdown:visible").first.wait_for(
                        state="hidden", timeout=3000)
                except Exception:
                    pass
                return True
            print(f"  [!] 点了『{target}』但输入框显示『{cur}』（第{attempt}次）")
        return False

    # 逐个候选控件尝试（label 命中 → 按当前值/位置排序的弹窗内 el-select）
    for idx, (name, cand) in enumerate(candidates, 1):
        if idx > 1:
            print(f"  [i] 提交路径换候选控件重试（{idx}/{len(candidates)}）：{name}")
            log_audit(f"提交路径换候选控件：{name}")
        try:
            if await _pick_on(cand):
                return True
        except Exception as exc:
            print(f"  [!] 候选『{name}』选择异常：{type(exc).__name__}: {str(exc)[:120]}")
            continue

    print(f"  [!] 提交路径未能选成『{target}』")
    await _dump_submit_path_failure(page, dialog, target, candidates)
    return False


def _quote_decide_submit_path(parts, has_hexun: bool,
                              direct_supply_cancelled: bool = False,
                              loss_amount=None, quote_amount=None) -> tuple:
    """决定提交路径 → (目标路径 or None, 改价零件数, 原因文案)

    ① 以下任一条件成立 → 『车辆定损』（回定损环节同步换件金额）：
       · 本单改过报价（有 fixed_price）
       · 本单取消过直供价（切换过价格类型）
       · 报价汇总换件金额 ≠ 定损汇总换件金额
    ② 未改价 + 有核损汇总 → None（保持系统默认）
    ③ 未改价 + 无核损汇总 → 『一级车辆核损』
    """
    fixed = 0
    for p in parts or []:
        val = p.get("fixed_price") if isinstance(p, dict) else getattr(p, "fixed_price", None)
        if val is not None:
            fixed += 1
    reasons = []
    if fixed > 0:
        reasons.append(f"本单改过报价（{fixed} 个零件）")
    if (loss_amount is not None and quote_amount is not None
            and abs(float(loss_amount) - float(quote_amount)) > 0.01):
        reasons.append(f"报价汇总换件金额 ¥{quote_amount:,.2f} ≠ 定损汇总 ¥{loss_amount:,.2f}")
    if direct_supply_cancelled:
        reasons.append("本单已取消直供价（换件金额已变）")
    if reasons:
        return "车辆定损", fixed, "；".join(reasons)
    if has_hexun:
        return None, fixed, "未改价 + 金额汇总含『核损汇总』"
    return "一级车辆核损", fixed, "未改价 + 金额汇总无『核损汇总』"


_JS_QUICK_LINK_PROCESS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, '');
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const isProc = el => norm(el.innerText) === '处理';
  const nodes = [...document.querySelectorAll('div,section,table,tr,td,form')];
  // 取「同时含『快速链接专栏』标题和『处理』按钮」的最小容器，避免扩到整页误点其它处理按钮
  const cands = nodes.filter(el => norm(el.innerText).includes('快速链接专栏')
                                   && [...el.querySelectorAll('button,a,[role=button],span')].some(isProc));
  cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  if (!cands.length) return {found: false, reason: '未找到含『处理』的快速链接专栏'};
  const box = cands[0];
  const btns = [...box.querySelectorAll('button,a,[role=button],span')]
      .filter(b => isProc(b) && vis(b));
  if (!btns.length) return {found: false, reason: '专栏内没有可点的『处理』'};
  document.querySelectorAll('[data-bot-quick-process]')
      .forEach(e => e.removeAttribute('data-bot-quick-process'));
  btns[0].setAttribute('data-bot-quick-process', '1');
  return {found: true};
}
"""


async def _quick_link_present(page: Page, timeout: float = 10.0) -> bool:
    """轮询『系统提示』弹窗里的快速链接专栏是否已渲染（弹窗内容异步加载）"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            info = await page.evaluate(_JS_QUICK_LINK_PROCESS)
            if isinstance(info, dict) and info.get("found"):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.4)


async def _quote_click_quick_process(page: Page) -> Optional[Page]:
    """提交上级后的『系统提示』弹窗 → 快速链接专栏 → 点『处理』

    之前本工号处理过的报案号，提交上级后会弹「系统提示」，
    下方快速链接专栏里有『处理』按钮：点了就回到本人的报价处理页（九级报价环节），
    可直接报价完成，不需要再走平级移交。
    """
    if not await _quick_link_present(page, timeout=10.0):
        info = None
        try:
            info = await page.evaluate(_JS_QUICK_LINK_PROCESS)
        except Exception as exc:
            print(f"  [!] 查找『快速链接专栏』失败：{type(exc).__name__}: {exc}")
        print(f"  [!] 未找到『快速链接专栏 → 处理』（{(info or {}).get('reason', '')}）")
        return None

    loc = page.locator("[data-bot-quick-process='1']").first
    clicked = False
    try:
        async with page.expect_popup(timeout=2500) as pop:
            await loc.click(timeout=4000)
            clicked = True
        newp = await pop.value
        await newp.wait_for_load_state("domcontentloaded")
        print("  [✓] 已点快速链接『处理』→ 新标签页打开报价处理")
        log_audit("提交上级后点快速链接『处理』→ 新标签页")
        return newp
    except Exception:
        pass
    if not clicked:
        try:
            await loc.click(timeout=4000)
            clicked = True
        except Exception as exc:
            print(f"  [!] 『处理』按钮点击失败：{type(exc).__name__}: {exc}")
            return None
    await asyncio.sleep(1.5)
    print("  [✓] 已点快速链接『处理』→ 当前页回到报价处理")
    log_audit("提交上级后点快速链接『处理』→ 当前页")
    return page


@register_step(desc="报价处理 → 报价完成 → 提交任务")
async def step_quote_finalize(page: Page, context: dict, **kwargs) -> None:
    print(f"\n🔹 STEP 9: 报价处理 → 报价完成 → 提交任务")
    from bot.config import SELECTORS_PORTAL as SP

    claim_no = context.get("claim_no", "")
    # 二次处理后经『快速链接 → 处理』回到报价页：后续提交路径保持系统默认
    after_quick_process = False

    # ========== 0) 高额分支：报价汇总『换件金额』> 4 万 → 业务动作『提交上级』 ==========
    # 一级报价权限上限 4 万；超过就直接提交上级（九级报价 / 市公司报价组），
    # 不再先试『报价完成』让系统拒一次。已提过上级（九级报价环节）不重复提交。
    if not context.get("high_amount_transferred"):
        threshold = float(kwargs.get("high_amount_threshold")
                          or context.get("high_amount_threshold")
                          or cfg.HIGH_AMOUNT_THRESHOLD)
        need, amount, stage = await quote_needs_superior(page, threshold)
        if need:
            print(f"  💰 报价汇总『换件金额』¥{amount:,.2f} > ¥{threshold:,.0f}"
                  f"（当前任务环节={stage or '未读到'}）→ 业务动作『提交上级』")
            log_audit(f"换件金额 ¥{amount:.2f} 超 ¥{threshold:g} → 提交上级（九级报价）")
            # 先标记，确保本处理轮次内失败也不会在兜底里再次提交上级
            context["high_amount_transferred"] = True
            await step_quote_submit_superior(page, context, **_transfer_kwargs(kwargs))

            # 分中心例外：单件换件 >5 万，已继续提交上级到分中心报价 → 本环节结束
            if context.get("branch_center_done"):
                print("  ✅ 已继续提交上级到分中心报价 → 本环节结束（不平级移交）")
                return

            # 第一次提交上级成功后决定收回路径：
            # 优先看『系统提示』弹窗里有没有快速链接专栏（实时信号，最可靠），
            # 有 → 点『处理』→ 新报价页直接做『报价完成』；
            # 没有 → 回退到平级移交回本工号后再报价完成。
            # 不再依赖本地 SQLite 历史表预判『首次/二次接收』——弹窗内容才是最终依据。
            print(f"  [i] {claim_no} 提交上级成功 → 检查弹窗快速链接专栏")
            log_audit("提交上级成功 → 检查快速链接专栏（优先）")

            new_page = await _quote_click_quick_process(page)
            if new_page is not None:
                page = new_page
                context["quote_page"] = page
                context["quick_process_done"] = True
                after_quick_process = True
                try:
                    await page.bring_to_front()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                print("  🔁 已在九级报价环节 → 业务动作改『报价完成』继续提交")
            else:
                print("  [i] 弹窗无快速链接专栏 → 走平级移交回本工号后再报价完成")
                log_audit("弹窗无快速链接专栏 → 回退平级移交")
                context["transfer_back_needed"] = True
                return

    # ========== 0) 决定提交路径 ==========
    # 规则优先级（从上到下）：
    #   ① 改过报价 / 取消过直供价 / 报价汇总换件金额 ≠ 定损汇总换件金额
    #      → 一定提给『车辆定损』（回定损环节同步换件金额），不管有没有核损汇总
    #   ② 没改价 + 无「核损汇总」行 → 『一级车辆核损』
    #   ③ 没改价 + 有「核损汇总」行 → 保持系统默认路径
    parts = kwargs.get("parts") or context.get("parts") or []
    has_hexun = await _quote_summary_has_hexun(page)
    loss_amt, quote_amt = await quote_part_amount_compare(page)
    if loss_amt is not None and quote_amt is not None:
        if abs(loss_amt - quote_amt) > 0.01:
            print(f"  [i] 换件金额不一致：报价汇总 ¥{quote_amt:,.2f} ≠ 定损汇总 ¥{loss_amt:,.2f}"
                  f" → 需回『车辆定损』同步")
        else:
            print(f"  [i] 换件金额一致：报价汇总 = 定损汇总 = ¥{quote_amt:,.2f}")
    else:
        print(f"  [i] 未同时读到定损汇总/报价汇总换件金额（定损={loss_amt} 报价={quote_amt}）"
              f" → 不做金额一致性判断")
    path_target, fixed, why = _quote_decide_submit_path(
        parts, has_hexun,
        direct_supply_cancelled=bool(context.get("direct_supply_cancelled")),
        loss_amount=loss_amt, quote_amount=quote_amt)

    if after_quick_process:
        # 九级报价环节的二次提交，路径由平台按当前环节决定，不强行改
        print("  [i] 二次处理已在九级报价环节 → 提交路径保持系统默认")
        path_target = None

    if path_target == "车辆定损":
        print(f"  [i] {why} → 提交路径必定选『车辆定损』（忽略核损汇总={has_hexun}）")
        log_audit(f"{why} → 提交路径=车辆定损（核损汇总={has_hexun}）")
    elif path_target == "一级车辆核损":
        print(f"  [i] {why} → 提交路径将选『一级车辆核损』")
        log_audit(f"{why} → 提交路径=一级车辆核损")
    else:
        print(f"  [i] {why} → 提交路径保持系统默认")

    # 人工等效：提交对话框已被人工点开 → 跳过前面动作，直接点『提交任务』
    dialog = await _quote_wait_submit_dialog(page, timeout=1.0)
    if dialog is not None:
        print("  [i] 提交对话框已弹出（可能人工已操作）→ 跳过选业务动作与提交")
        log_audit("跳过报价提交前置动作（对话框已弹出）")
    else:
        # ========== 1) 选业务动作（Element UI el-select 下拉） ==========
        # 生产实测：业务动作不是 radio，是 el-select 下拉，选项文字：报价完成/退回/提交上级
        await _quote_pick_biz_action(page, SP["quote_biz_done_text"])

        # ========== 2) 滚到底部点『提交』按钮 ==========
        opinion_section = page.locator(SP["quote_opinion_section"])
        if await opinion_section.count() > 0:
            await opinion_section.scroll_into_view_if_needed()
            await human_delay()

        try:
            _, submit_btn = await find_clickable(
                page, QUOTE_SUBMIT_SELECTORS, ["提交", "提交任务"],
                "报价处理 → 提交按钮", timeout=8000)
            await safe_click(submit_btn, "报价处理 → 提交按钮")
        except RuntimeError:
            print("  [i] 提交按钮未找到 → 可能人工已提交，检查结果提示")
        await human_delay()

        # ========== 2.1) 保险金额类提示框（提交后可能连续弹两个） ==========
        # ① 报价总额超过保险金额(…元)的70%,是否重新报价 → 取消
        # ② 标的车报价"合计"金额大于车辆保险金额(…元)请确定是否提交下一节点! → 确定
        insurance_handled = await handle_insurance_amount_dialogs(page)

        # ========== 2.2) 『请提交远程操作平台处理』提示框 → 分中心报价 ==========
        # 生产实测（2026-11-17）：报价完成提交不了时弹
        #   「已本地化换件的单项最大金额超过5万元,请提交远程操作平台处理!」
        # 点『确认』后改走：业务动作『提交上级』→ 提交路径=分中心报价 →
        #   组别代码=1 → 选「1000000000000127-总公司分中心报价组」→ 提交任务
        if await _handle_localized_part_50k_dialog(page, timeout=2.5):
            context["high_amount_transferred"] = True
            context["branch_center_done"] = True
            log_audit("报价完成被拦 → 改走分中心报价（提交上级 → 分中心报价组）")
            await step_quote_submit_superior(
                page, context,
                submit_path=TRANSFER_SUBMIT_PATH_BRANCH,
                group_option=TRANSFER_GROUP_OPTION_BRANCH,
                group_code=TRANSFER_GROUP_CODE_BRANCH,
                group_key=TRANSFER_GROUP_OPTION_KEY_BRANCH,
                group_exclude="",
            )
            print("  ✅ 分中心报价已提交（本环节完成，不再平级移交）")
            return

        # ========== 3) 等『报价任务提交』对话框弹出 ==========
        # 生产 DOM：div[role=dialog][aria-label="报价任务提交"].el-dialog（宽 880px）
        dialog = await _quote_wait_submit_dialog(
            page, timeout=8.0 if insurance_handled else 15.0)
        if dialog is None and insurance_handled:
            # 点『取消』（不重新报价）后若提交被中断 → 再点一次『提交』继续
            print("  [i] 处理保险金额提示后未见『报价任务提交』→ 再点一次『提交』继续")
            log_audit("处理保险金额提示后重试提交")
            try:
                _, submit_btn2 = await find_clickable(
                    page, QUOTE_SUBMIT_SELECTORS, ["提交", "提交任务"],
                    "报价处理 → 提交按钮(重试)", timeout=6000)
                await safe_click(submit_btn2, "报价处理 → 提交按钮(重试)")
            except RuntimeError:
                pass
            await human_delay()
            await handle_insurance_amount_dialogs(page)
            dialog = await _quote_wait_submit_dialog(page, timeout=15.0)

        if dialog is not None:
            print("  [✓] 报价任务提交对话框已弹出")
        else:
            print("  [i] 提交对话框未弹出（可能人工已确认），继续检查结果")

    if dialog is not None:
        # 按上面的规则改提交路径（改完再点提交任务）
        if path_target:
            _path_ok = await _quote_pick_submit_path(page, dialog, path_target)
            if not _path_ok:
                raise RuntimeError(
                    f"『报价任务提交』弹窗里未能选中提交路径『{path_target}』"
                    f"（原因：{why}）→ 中断，避免用默认路径提交导致卡在错误环节")
            await human_delay()
        if await _quote_click_submit_task(page, dialog):
            # 对话框关闭 = 提交已被受理
            try:
                await dialog.wait_for(state="hidden", timeout=8000)
                print("  [✓] 报价任务提交对话框已关闭")
            except Exception:
                print("  [i] 提交对话框仍显示 → 以结果提示为准")
        # 兜底：保险金额提示框若在『提交任务』之后才弹，也按同一规则处理
        await handle_insurance_amount_dialogs(page, timeout=3.0)
        await human_delay()

    # 校验结果：权限不足 → 抛异常（由 main 的兜底路由接管）；成功提示 → 正常结束
    await _wait_page_result(
        page,
        success_keywords=["报价任务已成功提交", "提交成功", "操作成功"],
        timeout=DEFAULT_TIMEOUT,
        strict=False,
        label="报价完成提交",
    )

    print(f"\n🎉 流程完成！报价任务已成功提交")

    # ========== 4) 收尾：关掉『系统提示 / 关闭当前窗口』弹窗 ==========
    # 生产实测：提交成功后会弹 Element UI el-message-box，
    # 标题通常是"系统提示"，按钮文案是"关闭当前窗口"（不是"确定"）。
    # 不清理的话循环挂机时会带着这个弹窗边跑下一个案件。
    await asyncio.sleep(0.8)  # 等弹窗真正出现在 DOM
    try:
        dismissed = await dismiss_all_modals(page, max_rounds=2, per_timeout=2.0)
        if dismissed:
            print(f"  [✓] STEP 9 收尾：已清掉 {dismissed} 个残留弹窗")
            log_audit(f"STEP 9 收尾：清掉 {dismissed} 个残留弹窗")
    except Exception:
        pass

    # Mock 专用：通知首页刷新该报案号为已完成状态（循环挂机时防重复处理）
    claim_no = context.get("claim_no", "")
    if claim_no:
        try:
            await page.evaluate(f"""() => {{
                try {{ window.__claim_done?.({claim_no!r}); }} catch(e) {{}}
            }}""")
        except Exception:
            pass


async def _quote_pick_biz_action(page: Page, label_text: str) -> None:
    """报价页 el-select 业务动作下拉 → 点选指定文字的选项

    生产实测（2026-11-03）：业务动作区是 Element UI el-select 下拉框，
    触发后出现 .el-select-dropdown:visible .el-select-dropdown__item 列表。
    下拉触发器和选项都会被遮罩层盖住一部分 → 必须先点触发器让 dropdown 出现，
    再点列表项里 textContent 匹配的那个。
    """
    from bot.config import SELECTORS_PORTAL as SP

    # 兜底：如果旧版 radio 还在（mock 环境），直接按 label 文本点
    # mock 里 radio 默认就是 checked 的，但保险起见还是点一下
    radio_label = page.locator("label").filter(has=page.locator("input[type='radio']")).filter(has_text=label_text).first
    if await radio_label.count() > 0:
        try:
            await safe_click(radio_label, f"业务动作(radio) → {label_text}", timeout=1500)
            print(f"  [✓] 业务动作(radio) → {label_text}")
            await human_delay()
            return
        except Exception:
            pass

    # 主流：el-select 下拉
    trigger = page.locator(SP["quote_biz_action_trigger"]).first
    if await trigger.count() == 0:
        # 再兜一层：直接找 label 含「业务动作」的 el-select
        trigger = page.locator(".el-form-item").filter(has_text="业务动作").locator(".el-input").first
    if await trigger.count() == 0:
        print(f"  [!] 未找到业务动作下拉触发器 → 跳过（可能已默认选中）")
        return

    try:
        await trigger.scroll_into_view_if_needed(timeout=3000)
        await trigger.click(timeout=5000)
    except Exception as exc:
        print(f"  [!] 点击业务动作下拉触发器失败: {exc}")
        return

    # 等 dropdown 出现
    try:
        items = page.locator(SP["quote_biz_dropdown_item"])
        await items.first.wait_for(state="visible", timeout=4000)
    except Exception:
        print("  [!] 业务动作下拉未出现 → 跳过")
        return

    # 点匹配文字的选项
    target = page.locator(f"{SP['quote_biz_dropdown_item']}:has-text('{label_text}')").first
    if await target.count() > 0:
        try:
            await safe_click(target, f"业务动作下拉 → {label_text}", timeout=4000)
            print(f"  [✓] 业务动作(el-select) → {label_text}")
            await human_delay()
            return
        except Exception as exc:
            print(f"  [!] 点击业务动作选项 {label_text} 失败: {exc}")

    # 兜底：遍历所有 item，比 text
    n = await items.count()
    for i in range(n):
        it = items.nth(i)
        try:
            txt = await it.text_content(timeout=1000) or ""
        except Exception:
            continue
        if label_text in txt.strip():
            try:
                await safe_click(it, f"业务动作下拉 → {label_text} (兜底)", timeout=3000)
                print(f"  [✓] 业务动作(el-select 兜底) → {label_text}")
                await human_delay()
                return
            except Exception:
                continue

    print(f"  [!] 业务动作下拉未找到『{label_text}』选项")


# ============================================================
# STEP 9R：零件名称蓝色 / 纯橙色 → 业务动作「退回」→ 意见 → 评分 → 提交任务
# ============================================================
RETURN_OPINION_BLUE = "建议蓝色字体配件补传带VIN匹配的EPC截图或其他价格依据，并核实厂家指导价"
RETURN_OPINION_ORANGE = "橙色字体配件请用“标准点选方式添加”修改为橙色带下划线字体配件"


def return_opinion_text(context: dict) -> str:
    """蓝色 / 纯橙色零件对应的退回意见（两类同时存在时合并）"""
    lines = []
    if context.get("return_blue_parts"):
        lines.append(RETURN_OPINION_BLUE)
    if context.get("return_orange_parts"):
        lines.append(RETURN_OPINION_ORANGE)
    return "；".join(lines) if lines else RETURN_OPINION_BLUE


async def _return_wait_box_with(pages, keywords, timeout: float = 10.0):
    """在若干页面里等待包含任一关键字的可见弹窗，返回其 locator；超时返回 None"""
    if isinstance(keywords, str):
        keywords = (keywords,)
    sources = list(dict.fromkeys([p for p in pages if p is not None and not p.is_closed()]))
    deadline = time.monotonic() + max(0.0, timeout)
    sels = (".el-message-box:visible", ".el-message-box__wrapper:visible",
            ".el-dialog:visible", "[role='dialog']:visible", ".modal:visible")
    while True:
        for pg in sources:
            if pg.is_closed():
                continue
            try:
                frames = list(pg.frames)
            except Exception:
                continue
            for fr in frames:
                for sel in sels:
                    try:
                        box = fr.locator(sel).last
                        if await box.count() == 0 or not await box.is_visible():
                            continue
                        flat = "".join((await box.inner_text(timeout=1000) or "").split())
                        if any(k in flat for k in keywords):
                            return box
                    except Exception:
                        continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.3)


async def _return_click_box_btn(box, labels=("确定", "确 定", "确认")) -> bool:
    """点弹窗里的指定按钮（默认『确定』），失败用 JS 兜底"""
    for text in labels:
        for sel in (f"button:has-text('{text}')", f".el-button:has-text('{text}')"):
            try:
                btn = box.locator(sel).last
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=4000)
                    print(f"  [✓] 提示框 → 点『{text}』")
                    return True
            except Exception:
                continue
    try:
        clicked = await box.evaluate(
            """(el) => {
              const btns = Array.from(el.querySelectorAll('button'));
              const b = btns.find(x => (x.innerText || '').replace(/\\s/g, '').indexOf('确定') >= 0);
              if (!b) return false;
              b.click();
              return true;
            }""")
        return bool(clicked)
    except Exception:
        return False


async def _return_wait_eval_page(quote_page: Page, timeout: float = 15.0):
    """等待『案件质量评价』页打开（新标签页 / 同页跳转）"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for pg in quote_page.context.pages:
            try:
                if pg.is_closed():
                    continue
                if "/evaluation/" in (pg.url or ""):
                    return pg
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.3)


async def _return_fill_opinion(page: Page, text: str) -> None:
    """在『意见录入』文本框填写退回意见（Element UI textarea）"""
    sels = [
        "form textarea.el-textarea__inner[placeholder*='请输入']",
        "form textarea.el-textarea__inner",
        ".quote-opinion textarea, #quote-opinion-section textarea",
        SELECTORS_PORTAL["quote_opinion_input"],
        "textarea.el-textarea__inner",
    ]
    for sel in sels:
        for fr in page.frames:
            try:
                ta = fr.locator(sel).first
                if await ta.count() == 0 or not await ta.is_visible():
                    continue
                await ta.scroll_into_view_if_needed(timeout=3000)
                await ta.click(timeout=4000)
                try:
                    await ta.fill("", timeout=3000)
                except Exception:
                    pass
                await ta.fill(text, timeout=5000)
                val = await ta.input_value(timeout=2000)
                if text[:6] not in (val or ""):
                    # Vue 双向绑定兜底
                    await ta.evaluate(
                        """(el, v) => {
                          el.value = v;
                          el.dispatchEvent(new Event('input', {bubbles: true}));
                          el.dispatchEvent(new Event('change', {bubbles: true}));
                        }""", text)
                    val = await ta.input_value(timeout=2000)
                print(f"  [✓] 退回意见已填写（{sel[:46]}）")
                log_audit(f"退回意见填写：{val[:160]}")
                return
            except Exception:
                continue
    raise RuntimeError("报价页未找到『意见录入』输入框 → 无法填写退回意见")


_JS_EVAL_PICK_LAST = r"""
() => {
  const vis = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' &&
           parseFloat(s.opacity || '1') > 0 && r.width > 0 && r.height > 0;
  };
  let labels = Array.from(document.querySelectorAll('label.el-checkbox')).filter(vis);
  if (!labels.length) labels = Array.from(document.querySelectorAll('label')).filter(vis);
  if (!labels.length) return {ok: false, reason: '未找到可勾选项'};
  let target = labels.find(l => (l.innerText || '').indexOf('存在其他问题需退回') >= 0);
  const byKeyword = !!target;
  if (!target) target = labels[labels.length - 1];
  const inp = target.querySelector('input[type="checkbox"]');
  if (inp && inp.checked) {
    return {ok: true, already: true, by_keyword: byKeyword,
            text: (target.innerText || '').trim().slice(0, 60)};
  }
  target.click();
  const after = target.querySelector('input[type="checkbox"]');
  return {ok: true, checked: after ? !!after.checked : null, by_keyword: byKeyword,
          text: (target.innerText || '').trim().slice(0, 60)};
}
"""


async def _return_pick_eval_last_item(eval_page: Page) -> None:
    """评价页勾选最后一项（优先『存在其他问题需退回』的可见复选框）"""
    for fr in eval_page.frames:
        try:
            info = await fr.evaluate(_JS_EVAL_PICK_LAST)
        except Exception:
            continue
        if info and info.get("ok"):
            print(f"  [✓] 评价页勾选：{info.get('text')}"
                  f"（关键字匹配={info.get('by_keyword')}）")
            log_audit(f"评价页勾选评价项：{info.get('text')}")
            return
    raise RuntimeError("评价页未找到可勾选的评价项（需至少选择一项才能提交）")


@register_step(desc="零件名称蓝色/纯橙色 → 业务动作『退回』→ 填意见 → 评分 → 提交任务",
               inputs=["claim_no"], outputs=["return_part_color"])
async def step_quote_return_for_color_parts(page: Page, context: dict, **kwargs) -> None:
    from bot.config import SELECTORS_PORTAL as SP

    claim_no = context.get("claim_no", "")
    blue = context.get("return_blue_parts") or []
    orange = context.get("return_orange_parts") or []
    opinion = return_opinion_text(context)
    print(f"\n🔹 STEP 9R: 报价页 → 业务动作『退回』（报案号 {claim_no}）")
    print(f"  蓝色零件: {'、'.join(str(p.get('name')) for p in blue[:5]) or '无'}")
    print(f"  橙色零件: {'、'.join(str(p.get('name')) for p in orange[:5]) or '无'}")
    print(f"  退回意见: {opinion}")
    log_audit(f"零件名称蓝色/纯橙色 → 退回 | 报案号={claim_no} | 意见={opinion}")

    quote_page = context.get("quote_page") or page
    await quote_page.bring_to_front()
    if not quote_page.is_closed():
        page = quote_page

    # 1) 业务动作 → 退回
    await _quote_pick_biz_action(page, SP["quote_biz_back_text"])

    # 2) 意见录入
    await _return_fill_opinion(page, opinion)

    # 3) 提交 → 提示『请至少完成一次评分』→ 确定 → 打开评价页
    _, submit_btn = await find_clickable(
        page, QUOTE_SUBMIT_SELECTORS, ["提交", "提交任务"],
        "报价处理 → 提交按钮（退回）", timeout=15000)
    await safe_click(submit_btn, "报价处理 → 提交按钮（退回）")
    await human_delay()

    eval_page = None
    box = await _return_wait_box_with([page], ("评分", "评价"), timeout=10.0)
    if box is not None:
        print("  [i] 弹出『请至少完成一次评分』提示 → 点『确定』进入评价页")
        try:
            async with page.context.expect_page(timeout=15000) as pinfo:
                await _return_click_box_btn(box)
            eval_page = await pinfo.value
        except Exception:
            await _return_click_box_btn(box)
    if eval_page is None or eval_page.is_closed():
        eval_page = await _return_wait_eval_page(page, timeout=15.0)
    if eval_page is None or eval_page.is_closed():
        raise RuntimeError("退回提交后未进入『案件质量评价』页 → 中断")
    context["evaluation_page"] = eval_page
    try:
        await eval_page.bring_to_front()
        await eval_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(1.0)

    # 4) 评价页：勾选最后一项 → 提交
    await _return_pick_eval_last_item(eval_page)
    await human_delay()
    try:
        _, eval_submit = await find_clickable(
            eval_page, QUOTE_SUBMIT_SELECTORS, ["提交"],
            "评价页 → 提交按钮", timeout=15000)
        await safe_click(eval_submit, "评价页 → 提交按钮")
    except RuntimeError as exc:
        raise RuntimeError(f"评价页未找到『提交』按钮：{exc}")
    await human_delay()

    # 5) 『保存成功！是否要关闭当前页？』→ 确定 → 评价页关闭
    box = await _return_wait_box_with([eval_page], "保存成功", timeout=10.0)
    if box is not None:
        try:
            async with eval_page.expect_close(timeout=10000):
                await _return_click_box_btn(box)
        except Exception:
            await _return_click_box_btn(box)
        await asyncio.sleep(0.8)
    try:
        if not eval_page.is_closed():
            await eval_page.close()
    except Exception:
        pass

    # 6) 回到报价页再次提交 → 『报价任务提交』→ 提交任务
    quote_page = context.get("quote_page") or page
    try:
        if quote_page.is_closed():
            cands = [p for p in page.context.pages
                     if not p.is_closed() and "/quote/" in (p.url or "")]
            quote_page = cands[-1] if cands else page
    except Exception:
        pass
    context["quote_page"] = quote_page
    await quote_page.bring_to_front()
    await asyncio.sleep(0.8)

    _, submit_btn2 = await find_clickable(
        quote_page, QUOTE_SUBMIT_SELECTORS, ["提交", "提交任务"],
        "报价处理 → 提交按钮（退回·二次提交）", timeout=15000)
    await safe_click(submit_btn2, "报价处理 → 提交按钮（退回·二次提交）")
    await human_delay()
    await handle_insurance_amount_dialogs(quote_page, timeout=3.0)

    dialog = None
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        dialog = await _quote_wait_submit_dialog(quote_page, timeout=0.5)
        if dialog is not None:
            break
        # 二次提交后可能先弹一个无标题确认框（close_alert），点掉后才是提交弹窗
        if await confirm_dialog(quote_page, timeout=0.5):
            await asyncio.sleep(0.8)
            continue
        await asyncio.sleep(0.4)
    if dialog is None:
        raise RuntimeError("退回流程：二次提交后未弹出『报价任务提交』对话框")
    print("  [✓] 报价任务提交对话框已弹出（退回）")

    await _quote_click_submit_task(quote_page, dialog)
    try:
        await dialog.wait_for(state="hidden", timeout=8000)
    except Exception:
        pass
    await _wait_page_result(
        quote_page,
        success_keywords=["工作流提交成功", "提交成功", "操作成功"],
        timeout=DEFAULT_TIMEOUT,
        strict=False,
        label="退回提交",
    )
    await asyncio.sleep(0.8)
    try:
        await dismiss_all_modals(quote_page, max_rounds=2, per_timeout=2.0)
    except Exception:
        pass
    print("  🎉 零件名称蓝色/纯橙色 → 退回流程已提交")
    # 记录退回历史：该报案号二次进入时默认资料已补齐，颜色视同正常
    try:
        from bot.pricing import get_price_service
        note = f"blue={len(blue)};orange={len(orange)}"
        if not get_price_service().mark_claim_color_returned(claim_no, note=note):
            print("  ⚠️ 退回历史写入失败（不影响本次提交）")
    except Exception as exc:
        print(f"  ⚠️ 退回历史写入异常: {type(exc).__name__}")


# ============================================================
# 补救分支 1：报价页 → 业务动作「提交上级」→ 意见「权限不够，报上级审核」→ 提交
# ============================================================
SUPERIOR_OPINION = "权限不够，报上级审核"


@register_step(desc="报价页 → 业务动作『提交上级』→ 提交（超出权限案件）",
               inputs=["claim_no"],
               outputs=["transfer_submit_path", "transfer_group_option"])
async def step_quote_submit_superior(page: Page, context: dict, **kwargs) -> dict:
    claim_no = kwargs.get("claim_no") or context.get("claim_no", "")
    # 参数：优先取流程编辑器节点里配置的 kwargs，未配置则回退全局默认值
    submit_path = kwargs.get("submit_path") or TRANSFER_SUBMIT_PATH
    group_option = kwargs.get("group_option") or TRANSFER_GROUP_OPTION
    group_code = kwargs.get("group_code") or TRANSFER_GROUP_CODE
    group_key = kwargs.get("group_key") or TRANSFER_GROUP_OPTION_KEY
    group_exclude = kwargs.get("group_exclude")
    if group_exclude is None:
        group_exclude = TRANSFER_GROUP_OPTION_EXCLUDE
    opinion = kwargs.get("opinion") or SUPERIOR_OPINION
    staff_code = kwargs.get("staff_code") or ""

    print(f"\n🔹 STEP 9a: 报价页 → 业务动作『提交上级』（报案号 {claim_no}）")
    print(f"  [参数] 提交路径={submit_path} | 组别代码={group_option} | 人员代码={staff_code or '（自动分配）'}")
    log_audit(
        f"超出权限补救：报案号 {claim_no} → 业务动作『提交上级』"
        f" | 路径={submit_path} | 组别={group_option}"
    )

    # 1) 业务动作 → 提交上级（el-select 下拉）
    await _quote_pick_biz_action(page, SELECTORS_PORTAL["quote_biz_superior_text"])

    # 2) 意见 → 「权限不够，报上级审核」（文本框优先；原生下拉只有在真有该选项时才用）
    #    生产页面意见是文本框/el-select，混用原生 select 会白等 30s 超时，导致提交上级失败
    opinion_section = page.locator(SELECTORS_PORTAL["quote_opinion_section"]).first
    if await opinion_section.count() > 0:
        try:
            await opinion_section.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass

    filled = False
    opinion_select = page.locator(SELECTORS_PORTAL["quote_opinion_select"]).first
    if await opinion_select.count() > 0 and await opinion_select.is_visible():
        try:
            opts = [t.strip() for t in await opinion_select.locator("option").all_inner_texts()]
        except Exception:
            opts = []
        if any(opinion == o or opinion in o for o in opts):
            try:
                await opinion_select.select_option(label=opinion, timeout=4000)
                print(f"  [✓] 意见下拉 → {opinion}")
                log_audit(f"意见下拉 → {opinion}")
                filled = True
            except Exception as exc:
                print(f"  [!] 意见下拉选择失败（改用文本框）: {type(exc).__name__}")
        else:
            print(f"  [i] 意见下拉无『{opinion}』选项 {opts[:5]} → 改用文本框")

    if not filled:
        opinion_input = page.locator(SELECTORS_PORTAL["quote_opinion_input"]).first
        if await opinion_input.count() > 0:
            try:
                await opinion_input.fill(opinion, timeout=5000)
                print(f"  [✓] 意见说明 → {opinion}")
                log_audit(f"意见说明 → {opinion}")
                filled = True
            except Exception as exc:
                print(f"  [!] 意见填写失败（不阻断提交上级）: {type(exc).__name__}")
        else:
            print("  [i] 报价页未找到意见输入框 → 跳过意见（不影响提交上级）")
    await human_delay()

    # 3) 提交 → 确认 → 等「工作流转交成功」（找不到提示就抛异常，不静默成功）
    try:
        _, submit_btn = await find_clickable(
            page,
            QUOTE_SUBMIT_SELECTORS,
            ["提交", "提交任务"], "报价处理 → 提交按钮", timeout=12000)
    except RuntimeError as exc:
        raise RuntimeError(f"报价页未找到提交按钮：{exc}")
    await safe_click(submit_btn, "报价处理 → 提交按钮（提交上级）")
    await human_delay()

    # 3.1) 保险金额类提示框（提交上级同样会弹，处理规则与报价完成一致）
    #      ① 报价总额超保险金额 70% 是否重新报价 → 取消
    #      ② 标的车报价合计大于保险金额 请确定是否提交下一节点 → 确定
    await handle_insurance_amount_dialogs(page)

    # 3.2) 已本地化换件超 5 万元提示框 → 点确认，触发分中心报价分支
    needs_branch = await _handle_localized_part_50k_dialog(page)

    # ========== 分支：分中心报价 ==========
    # 触发条件："已本地化换件的单项最大金额超过5万元,请提交远程操作平台处理!"
    # 改走：重新选"提交上级" → 提交 → 保险/本地化提示 → 报价任务提交弹窗
    #       提交路径默认"分中心报价"（已由系统选中），只需填组别代码 1 → 选总公司分中心报价组
    if needs_branch:
        print(f"  🔁 触发分中心报价分支 → 重新选『提交上级』 + 组别 {TRANSFER_GROUP_OPTION_BRANCH}")
        log_audit(
            f"分中心报价分支：报案号 {claim_no} → 路径={TRANSFER_SUBMIT_PATH_BRANCH}"
            f" | 组别={TRANSFER_GROUP_OPTION_BRANCH}"
        )

        # 重新选业务动作 → 提交上级
        await _quote_pick_biz_action(page, SELECTORS_PORTAL["quote_biz_superior_text"])
        await human_delay()

        # 重新点提交
        try:
            _, submit_btn2 = await find_clickable(
                page, QUOTE_SUBMIT_SELECTORS, ["提交", "提交任务"],
                "报价处理 → 提交按钮(分中心分支)", timeout=10000)
        except RuntimeError as exc:
            raise RuntimeError(f"分中心分支未找到提交按钮：{exc}")
        await safe_click(submit_btn2, "报价处理 → 提交按钮（分中心分支）")
        await human_delay()

        # 保险金额 + 本地化换件提示（可能再次弹出，清干净）
        await handle_insurance_amount_dialogs(page)
        await _handle_localized_part_50k_dialog(page, timeout=2.0)

        # 继续往下走 4) 等报价任务提交弹窗，但用分中心的参数
        submit_path = TRANSFER_SUBMIT_PATH_BRANCH
        group_option = TRANSFER_GROUP_OPTION_BRANCH
        group_code = TRANSFER_GROUP_CODE_BRANCH
        group_key = TRANSFER_GROUP_OPTION_KEY_BRANCH
        group_exclude = ""
        # 分中心例外：本次处理已经继续提交到分中心报价 → 本环节结束，不再平级移交
        context["branch_center_done"] = True
        # 分中心分支路径通常已默认选中，跳过 4.1 的选路径步骤（只填组别代码即可）

    # 4) 「报价任务提交」弹窗：提交路径=九级报价，组别代码=5175 → 市公司报价组 → 提交任务
    #    先过一层「确认提交」框（mock/部分环境有），再等真正的报价任务提交弹窗
    confirm = page.locator(SELECTORS_PORTAL["quote_submit_confirm"]).first
    if await confirm.count() > 0 and await confirm.is_visible():
        await safe_click(confirm, "报价提交确认（进入报价任务提交）")
        await human_delay()

    dialog = await _quote_wait_submit_dialog(page, timeout=15.0)
    if dialog is None:
        raise RuntimeError(
            "提交上级后未出现『报价任务提交』弹窗（选择器待真机确认："
            f"{SELECTORS_PORTAL['quote_task_dialog']}）"
        )

    # 4.1 提交路径选择 → submit_path（九级报价 / 分中心报价 等）
    #    生产要点：下拉列表很长，『九级报价』在最底部；必须滚到底按完全相等文本点中，
    #    否则会停在系统默认『二级报价』，组别代码/人员代码也不会自动带出。
    #    提交上级必须选对路径 → 选不中就中断（不能带着错误路径提交）。
    path_ok = await _quote_pick_submit_path(page, dialog, submit_path)
    if not path_ok:
        raise RuntimeError(
            f"『报价任务提交』弹窗里未能选中提交路径『{submit_path}』"
            "（下拉是长列表，需滚到底精确点击）→ 中断，避免用错误路径提交上级"
        )

    # 4.2 人员代码先行判定（关键分流）：
    #    已处理过的九级报价案件，选完『九级报价』后人员代码会自动带入上次九级报价员；
    #    此时**不需要也不能再填组别代码**，直接提交即可 → 之后才会出现带
    #    『快速链接专栏』的系统提示，点『处理』在当前页完成『报价完成』。
    #    首次接收的案件人员代码为空，才走『组别代码 → 平台联动带出人员』默认流程。
    staff_input = await _quote_field_scope(page, ("人员代码", "人员"))
    if staff_input is not None:
        _si = staff_input.locator("input").first
        staff_input = _si if await _si.count() > 0 else staff_input
    else:
        staff_input = page.locator(SELECTORS_PORTAL["quote_task_staff_input"]).first

    staff_val = ""
    try:
        if await staff_input.count() > 0:
            staff_val = (await staff_input.input_value() or "").strip()
    except Exception:
        pass

    group_input = None
    group_val = ""
    if staff_val:
        print(f"  [i] 人员代码已自动带入『{staff_val}』→ 已处理过的九级报价案件，"
              "跳过组别代码直接提交")
        log_audit(f"人员代码已自动带入『{staff_val}』→ 跳过组别代码，直接提交任务")
        context["quick_process_expected"] = True
    else:
        # 4.2.1 组别代码控件（首次接收）
        #    优先用稳定 placeholder 定位：弹窗重渲染后 data-bot-field 标记会丢，
        #    生产实测因此出现 input 解析不到、点击空转 15s 的情况。
        await _quote_clear_blocking_overlay(page, "组别代码")
        try:
            await _wait_no_loading(page, 3.0)
        except Exception:
            pass
        group_input = await _quote_visible_input(dialog, "组号或组名")
        if group_input is None or await group_input.count() == 0:
            group_input = await _quote_field_scope(page, ("组别代码", "组别"))
            if group_input is not None:
                _gi = group_input.locator("input").first
                group_input = _gi if await _gi.count() > 0 else group_input
            else:
                group_input = page.locator(SELECTORS_PORTAL["quote_task_group_input"]).first
        if await group_input.count() == 0:
            raise RuntimeError(
                "未找到『组别代码』控件（选择器待真机确认："
                f"{SELECTORS_PORTAL['quote_task_group_input']} 或 label=组别代码）"
            )

        # 选完提交路径后，系统可能自动带出组别代码；弹窗也可能异步重渲染。
        # → 先等自动带出稳定，未带出再远程填写，并做二次重填兜底。
        group_input, group_val = await _quote_ensure_group_selected(
            page, dialog, group_input, group_code, group_key, group_option)

        if _quote_group_ok(group_val, group_key):
            print(f"  [✓] 组别代码已选中：{group_val}"
                  f"（人员代码：{staff_val or '—'}）")
            log_audit(f"组别已选中：{group_val} | 人员：{staff_val or '—'}")
        else:
            # 二次兜底：重新定位输入框再填一遍（弹窗可能已重渲染）
            print(f"  [!] 组别代码仍为空/未选中（当前『{group_val or '空'}』）→ 再填一次")
            group_input, group_val = await _quote_ensure_group_selected(
                page, dialog, group_input, group_code, group_key, group_option)
            # 人员代码：生产实测是 disabled readonly，由组别代码联动自动带出 → 跳过

        # 4.2.2 提交前强校验：组别和人员至少一项“有效”
        #    注意：只输入短号（5175）时 input 有值但没选中，平台会判为空并弹提示，
        #    所以要用 _quote_group_selected 判断是否真的选成了完整选项。
        if not _quote_group_ok(group_val, group_key) and not staff_val:
            raise RuntimeError(
                f"『报价任务提交』中组别代码/人员代码均为空 → 中断"
                f"（避免平台弹『组或人员请至少录入一项!』）| 路径={submit_path}"
            )

    # 4.3 提交任务按钮
    await _quote_dismiss_group_required_popup(page, timeout=0.2)
    task_submit = dialog.locator("button:has-text('提交任务')").last
    try:
        await task_submit.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        raise RuntimeError(
            "『报价任务提交』弹窗内未找到『提交任务』按钮"
        ) from exc

    # 4.3.1 点击前紧邻复检：等待按钮期间弹窗异步重渲染可能清空组别 → 发现即重填
    #       已处理案件（人员代码已自动带入）不复检/重填组别，避免覆盖系统带入的报价员。
    if not staff_val:
        final_inp = await _quote_locate_group_input(page, dialog) or group_input
        final_val = await _quote_read_group_value(final_inp)
        if not _quote_group_ok(final_val, group_key):
            final_inp, final_val = await _quote_ensure_group_selected(
                page, dialog, final_inp, group_code, group_key, group_option)
            group_input = final_inp
        if not _quote_group_ok(final_val, group_key):
            s_val = ""
            try:
                if await staff_input.count() > 0:
                    s_val = (await staff_input.input_value() or "").strip()
            except Exception:
                pass
            if not s_val:
                raise RuntimeError(
                    f"提交任务前复检：组别代码/人员代码仍为空（当前『{final_val or '空'}』）"
                    f"→ 中断 | 路径={submit_path}"
                )

    # 4.4 点『提交任务』→ 若平台仍判组别为空（弹『组或人员请至少录入一项！』），
    #     关闭提示、重选组别后重试一次；再失败就明确中断，不空等超时。
    for attempt in (1, 2):
        if attempt == 2:
            if staff_val:
                raise RuntimeError(
                    f"提交上级：人员代码已自动带入『{staff_val}』，"
                    "平台仍提示『组或人员请至少录入一项！』→ 中断"
                )
            print("  [!] 平台判定组别为空 → 重新选择组别后再提交一次")
            log_audit("提交上级：平台判定组别为空 → 重选组别后重试")
            try:
                if not await dialog.is_visible():
                    raise RuntimeError("『报价任务提交』弹窗已关闭，无法重选组别")
            except Exception as exc:
                raise RuntimeError(
                    f"提交上级：组别未选中且弹窗不可用 → 中断: {exc}") from exc
            group_input, _ = await _quote_ensure_group_selected(
                page, dialog, group_input, group_code, group_key, group_option)
            await _quote_dismiss_group_required_popup(page, timeout=0.1)

        await _quote_click_submit_task(page, dialog)

        # 兜底：保险金额提示框若在『提交任务』之后才弹，也按同一规则处理
        await handle_insurance_amount_dialogs(page, timeout=2.0)

        if not await _quote_dismiss_group_required_popup(page):
            break
        if attempt == 2:
            raise RuntimeError(
                "提交上级：平台提示『组或人员请至少录入一项！』"
                "（组别代码未成功选中）→ 中断"
            )

    # 已处理过的九级报价案件：平台用『系统提示 + 快速链接专栏』代替工作流转交提示，
    # 所以快速链接专栏本身也视为提交上级成功的证据。
    if await _quick_link_present(page, timeout=2.0):
        print("  [✓] 提交上级成功：系统提示含快速链接专栏（已处理案件）")
        log_audit(f"提交上级成功（快速链接专栏）：报案号 {claim_no}")
    else:
        try:
            await _wait_page_result(
                page,
                success_keywords=["工作流转交成功", "转交成功", "提交成功"],
                error_keywords=("提交失败", "操作失败", "系统异常"),
                strict=True,
                label="提交上级",
            )
        except RuntimeError:
            # 成功提示文案可能不同：再确认一次快速链接专栏，仍无才按失败处理
            if not await _quick_link_present(page, timeout=6.0):
                raise
            print("  [✓] 提交上级成功：系统提示含快速链接专栏（已处理案件）")
            log_audit(f"提交上级成功（快速链接专栏）：报案号 {claim_no}")
    print(f"  [✓] 提交上级成功：任务已转出（{submit_path} / {group_option}）")
    log_audit(f"提交上级成功：报案号 {claim_no} 任务已转出 {group_option}")
    return {
        "transfer_submit_path": submit_path,
        "transfer_group_option": group_option,
    }


# ============================================================
# 补救分支 2：任务中心 → 平级移交 → 报案号查询 → 移交回自己
# 生产菜单：理赔作业门户 → 左侧『车险理赔』→『任务中心』→『平级移交』
# ============================================================
async def _live_page(page: Page) -> Page:
    """返回一个仍可用的 Page：原页面被关闭（提交上级后报价 tab 常自动关闭）时，
    从同一 context 里取其它 tab，全部关闭则新开一个。"""
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass
    try:
        ctx = page.context
    except Exception:
        return page
    for p in ctx.pages:
        if not p.is_closed():
            return p
    return await ctx.new_page()


async def _find_in_frames(page: Page, selectors: Sequence[str], placeholder: str = ""):
    """跨 iframe 找第一个可见元素（门户的平级移交页可能在 iframe 内）"""
    pages = [page]
    try:
        pages += [p for p in page.context.pages if p is not page and not p.is_closed()]
    except Exception:
        pass
    for pg in pages:
        if pg.is_closed():
            continue
        for fr in pg.frames:
            for sel in selectors:
                try:
                    loc = fr.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return loc
                except Exception:
                    continue
            if placeholder:
                try:
                    loc = fr.get_by_placeholder(placeholder).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return loc
                except Exception:
                    continue
    return None


async def _transfer_form_ready(page: Page) -> bool:
    try:
        loc = await _find_in_frames(
            page, [SELECTORS_PORTAL["task_transfer_claim_input"]], placeholder="报案号")
        return loc is not None
    except Exception:
        return False


async def _open_transfer_by_menu(page: Page):
    """左侧菜单打开『平级移交』，返回承载页（可能是新开的 tab）

    生产实测（2026-11-18 保存页）：
      · 『任务中心』是 li.el-submenu（不是 el-menu-item），标题在 .el-submenu__title 里；
      · 子菜单项『平级移交』是 li.el-menu-item > div.menu-item-text[data-url='/claimcar/taskTransfer/TaskTransferQuery']；
      · 点击后**新开一个 tab**（标题=平级移交）。
    """
    menu_item = (".menu-item-text[data-url*='taskTransfer']",
                 "span.menu-text:text-is('平级移交')",
                 ".el-menu-item:has-text('平级移交') .menu-text")
    chains = [
        [(".el-submenu__title:has-text('任务中心')",
          ".el-submenu__title span:text-is('任务中心')",
          "li.el-submenu:has-text('任务中心') .el-submenu__title", "任务中心"),
         (menu_item, "平级移交")],
        [(".el-menu-item:has-text('车险理赔')",
          "li:has-text('车险理赔') .menu-text", "车险理赔"),
         (".el-submenu__title:has-text('任务中心')",
          ".el-submenu__title span:text-is('任务中心')", "任务中心"),
         (menu_item, "平级移交")],
    ]
    for chain in chains:
        ok_chain = True
        for sels, label in chain:
            before = set()
            try:
                before = {id(p) for p in page.context.pages}
            except Exception:
                pass
            item = None
            hidden_first = None
            for s in sels:
                try:
                    loc = page.locator(s)
                    n = await loc.count()
                except Exception:
                    continue
                for i in range(min(n, 4)):
                    cand = loc.nth(i)
                    if hidden_first is None:
                        hidden_first = cand
                    try:
                        if await cand.is_visible():
                            item = cand
                            break
                    except Exception:
                        continue
                if item is not None:
                    break
            try:
                if item is None and hidden_first is not None:
                    # 侧边栏折叠成图标时菜单项不可见，但仍可强制点击展开
                    item = hidden_first
                    await item.click(timeout=4000, force=True)
                elif item is not None:
                    await item.scroll_into_view_if_needed(timeout=2000)
                    await item.click(timeout=5000)
                else:
                    raise RuntimeError("未找到菜单项")
                print(f"  [✓] 左侧菜单 → {label}")
                log_audit(f"左侧菜单 → {label}")
            except Exception as exc:
                print(f"  [!] 左侧菜单『{label}』点击失败：{type(exc).__name__}")
                ok_chain = False
                break
            await asyncio.sleep(1.0)
            # 点菜单可能新开 tab（平级移交就是新 tab）
            try:
                new_pages = [p for p in page.context.pages
                             if id(p) not in before and not p.is_closed()]
            except Exception:
                new_pages = []
            if new_pages:
                for np in new_pages:
                    try:
                        await np.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    for _ in range(8):
                        if await _transfer_form_ready(np):
                            return np
                        await asyncio.sleep(0.5)
                return new_pages[-1]
        if ok_chain:
            for _ in range(8):
                if await _transfer_form_ready(page):
                    return page
                await asyncio.sleep(0.5)
    return None


async def _open_transfer_page(page: Page, portal_url: str = "") -> Page:
    """打开『平级移交』页：先直连已知地址，再退回左侧菜单（任务中心 → 平级移交）"""
    import bot.config as _cfg

    page = await _live_page(page)

    if _cfg.USE_MOCK_PORTAL:
        await page.goto(MOCK_URLS["task_transfer"], wait_until="domcontentloaded")
        return page

    candidates = [TASK_TRANSFER_URL, getattr(cfg, "TASK_TRANSFER_URL_ALT", "")]
    candidates += list(getattr(cfg, "TASK_TRANSFER_URL_ALTS", ()) or ())
    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        page = await _live_page(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        except Exception as exc:
            print(f"  [!] 平级移交直连失败（{url[:70]}）：{type(exc).__name__}")
            continue
        for _ in range(10):
            if await _transfer_form_ready(page):
                print("  [✓] 已打开『平级移交』页（直连）")
                return page
            await asyncio.sleep(0.5)

    page = await _live_page(page)
    portal_url = portal_url or cfg.PORTAL_REAL_URL
    try:
        await page.goto(portal_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    # 等 SPA 把左侧菜单渲染出来（菜单折叠也不影响读取 data-url）
    for _ in range(20):
        try:
            if await page.locator(".menu-item-text[data-url*='taskTransfer']").count() > 0:
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    # ② 从门户菜单 DOM 读 data-url 直连（菜单即使折叠成图标也能读到；
    #    实测 data-url="/claimcar/taskTransfer/TaskTransferQuery"）
    menu_url = ""
    try:
        loc = page.locator(".menu-item-text[data-url*='taskTransfer']").first
        if await loc.count() > 0:
            menu_url = (await loc.get_attribute("data-url") or "").strip()
    except Exception:
        menu_url = ""
    if menu_url:
        from urllib.parse import urljoin
        full = urljoin(page.url or cfg.PORTAL_REAL_URL, menu_url)
        page = await _live_page(page)
        try:
            await page.goto(full, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            for _ in range(10):
                if await _transfer_form_ready(page):
                    print(f"  [✓] 已打开『平级移交』页（菜单 data-url: {menu_url}）")
                    log_audit(f"平级移交 data-url 打开成功: {menu_url}")
                    return page
                await asyncio.sleep(0.5)
        except Exception as exc:
            print(f"  [!] 菜单 data-url 打开失败：{type(exc).__name__}")

    found = await _open_transfer_by_menu(page)
    if found is not None:
        for _ in range(10):
            if await _transfer_form_ready(found):
                print("  [✓] 已打开『平级移交』页（菜单）")
                log_audit("平级移交页已打开（菜单）")
                return found
            await asyncio.sleep(0.5)
    # 最后兜底：任一已开 tab 里已有平级移交表单
    try:
        for p in page.context.pages:
            if not p.is_closed() and await _transfer_form_ready(p):
                return p
    except Exception:
        pass
    raise RuntimeError("未能打开『任务中心 → 平级移交』页面（直连与菜单均失败）")


async def _pick_transfer_task_type(page: Page, task_type: str) -> None:
    """平级移交查询条件 → 任务类型下拉 → 选『九级报价』（mock 没有该字段则跳过）"""
    sel = await _find_in_frames(page, [SELECTORS_PORTAL["task_transfer_tasktype"]])
    if sel is None:
        return
    cur = ""
    try:
        cur = (await sel.locator("input").first.input_value(timeout=1200) or "").strip()
    except Exception:
        pass
    if cur == task_type:
        print(f"  [i] 任务类型已是『{task_type}』")
        return
    try:
        await sel.click(timeout=4000)
    except Exception:
        print("  [!] 任务类型下拉打不开 → 保持默认")
        return
    await asyncio.sleep(0.4)
    # 下拉选项在保存页里是 li.el-select-dropdown__item > span；用 JS 按文本完全相等点击，
    # 不受长列表滚动/视口影响（任务类型共 50+ 项，九级报价在中后段）
    info = None
    for _ in range(10):
        try:
            info = await page.evaluate(
                r"""
                (want) => {
                  const norm = s => (s || '').replace(/\s+/g, '');
                  const vis = el => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden'
                           && st.display !== 'none';
                  };
                  const drops = Array.from(document.querySelectorAll('.el-select-dropdown')).filter(vis);
                  for (const d of drops) {
                    for (const it of d.querySelectorAll('.el-select-dropdown__item')) {
                      if (norm(it.innerText || it.textContent) !== want) continue;
                      it.scrollIntoView({block: 'center'});
                      it.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                      it.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                      it.click();
                      return {ok: true, text: (it.innerText || '').trim()};
                    }
                  }
                  return {ok: false};
                }
                """,
                "".join(task_type.split()),
            )
        except Exception:
            info = None
        if isinstance(info, dict) and info.get("ok"):
            break
        await asyncio.sleep(0.5)
    if isinstance(info, dict) and info.get("ok"):
        print(f"  [✓] 任务类型 → {info.get('text') or task_type}")
        log_audit(f"平级移交任务类型 → {info.get('text') or task_type}")
        await asyncio.sleep(0.5)
        return
    print(f"  [!] 任务类型下拉没找到『{task_type}』→ 保持默认")


def _split_transfer_target(target: str) -> tuple[str, str]:
    """拆分『工号-姓名』，兼容 - / － / — / –

    平台远程候选只认纯工号：多一个分隔符就没有下拉，输入不足会混入其他人员。
    因此这里只保留工号里的字母数字部分。
    """
    raw = (target or "").strip()
    parts = re.split(r"[-－—–]", raw, maxsplit=1)
    code_part = parts[0] if parts else raw
    name = parts[1].strip() if len(parts) > 1 else ""
    m = re.search(r"[A-Za-z0-9]+", code_part or "")
    code = m.group(0) if m else (code_part or "").strip()
    return code, name


async def _transfer_clear_input(target_input) -> None:
    """可靠清空目标人员输入框（Element UI 对 fill("") 可能保留旧值/分隔符）"""
    try:
        await target_input.click(timeout=3000)
    except Exception:
        try:
            await target_input.evaluate(
                "el => { el.removeAttribute('readonly'); el.focus(); }")
        except Exception:
            pass
    for action in ("Control+a", "Backspace"):
        try:
            await target_input.press(action)
        except Exception:
            pass
    try:
        await target_input.fill("")
    except Exception:
        pass
    await asyncio.sleep(0.1)
    val = ""
    try:
        val = (await target_input.input_value() or "").strip()
    except Exception:
        pass
    if val:
        try:
            await target_input.evaluate(
                "el => { el.value='';"
                " el.dispatchEvent(new Event('input',{bubbles:true}));"
                " el.dispatchEvent(new Event('change',{bubbles:true})); }")
        except Exception:
            pass
        await asyncio.sleep(0.1)


async def _transfer_pick_target(page: Page, target: str, timeout: float = 8.0) -> str:
    """在目标人员候选里点中目标；返回选中的候选文本，找不到返回空串"""
    code, name = _split_transfer_target(target)
    norm_code = re.sub(r"\s+", "", code or "")
    norm_name = re.sub(r"\s+", "", name or "")

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    deadline = time.monotonic() + max(1.0, timeout)
    seen: list[str] = []
    while time.monotonic() < deadline:
        for fr in page.frames:
            try:
                opts = fr.locator(
                    ".el-autocomplete-suggestion:visible li, "
                    ".el-select-dropdown:visible .el-select-dropdown__item")
                n = await opts.count()
            except Exception:
                continue
            for i in range(min(n, 30)):
                opt = opts.nth(i)
                try:
                    txt = ((await opt.inner_text(timeout=600)) or "").strip()
                except Exception:
                    continue
                if not txt:
                    continue
                if txt not in seen:
                    seen.append(txt)
                nt = _norm(txt)
                # 只认「以工号开头」的候选；短工号绝不做子串匹配，避免误选别人
                code_ok = bool(norm_code) and nt.startswith(norm_code)
                if not code_ok and norm_code and len(norm_code) >= 6:
                    code_ok = norm_code in nt
                if not code_ok:
                    continue
                if norm_name and norm_name not in nt:
                    continue
                clicked = False
                try:
                    await opt.click(timeout=2000)
                    clicked = True
                except Exception:
                    pass
                if not clicked:
                    try:
                        await opt.evaluate(
                            "el => {"
                            " const t = el.querySelector('span') || el;"
                            " t.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                            " t.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                            " t.click(); }")
                        clicked = True
                    except Exception:
                        clicked = False
                if clicked:
                    return txt
        await asyncio.sleep(0.25)
    if seen:
        print(f"  [!] 目标人员候选里没有『{target}』，可见 {len(seen)} 项：{seen[:8]}")
        log_audit(f"平级移交：目标人员候选无匹配，可见 {seen[:8]}")
    return ""


async def _transfer_fill_target(page: Page, target_input, target: str) -> None:
    """填写『任务提交』弹窗里的目标人员（远程搜索 el-select）

    生产实测：只输入完整工号（如 17247339）才有候选；
    输入少了会混入其他人员，多输入一个『-』则下拉直接变『无数据』。
    """
    code, name = _split_transfer_target(target)
    if not code:
        raise RuntimeError(f"目标人员配置『{target}』无法解析出工号")

    picked = ""
    val = ""
    for attempt in (1, 2):
        await _transfer_clear_input(target_input)
        try:
            await target_input.type(code, delay=60)
        except Exception as exc:
            raise RuntimeError(f"目标人员无法输入工号『{code}』: {exc}") from exc
        print(f"  [i] 目标人员 输入工号『{code}』（第 {attempt} 次），等待候选...")
        picked = await _transfer_pick_target(page, target, timeout=8.0)
        if not picked:
            continue
        await asyncio.sleep(0.4)
        try:
            val = (await target_input.input_value() or "").strip()
        except Exception:
            val = ""
        if val:
            break
        print("  [!] 目标人员已点选但输入框为空 → 清空后重试")
        log_audit("平级移交：目标人员点选后输入框为空 → 重试")
        picked = ""

    if not picked or not val:
        raise RuntimeError(
            f"目标人员『{target}』未能在候选里选中（需输入完整工号『{code}』，"
            "不能带『-』或只输入片段）")

    print(f"  [✓] 目标人员 → {picked}" + (f"（输入框：{val}）" if val != picked else ""))
    log_audit(f"平级移交：目标人员选中 {picked}")


@register_step(desc="任务中心 → 平级移交 → 报案号查询 → 移交回本人",
               inputs=["claim_no", "transfer_target"])
async def step_task_transfer_back(page: Page, context: dict, **kwargs) -> None:
    claim_no = kwargs.get("claim_no") or context.get("claim_no", "")
    target = (context.get("transfer_target") or TRANSFER_TARGET_DEFAULT).strip()
    task_type = kwargs.get("task_type") or TRANSFER_SUBMIT_PATH   # 九级报价
    print(f"\n🔹 STEP 9b: 任务中心 → 平级移交 → 报案号 {claim_no} → 目标人员 {target}")
    log_audit(f"平级移交：报案号 {claim_no} → 目标人员 {target}（任务类型 {task_type}）")

    page = await _open_transfer_page(page, context.get("portal_url", ""))
    await human_delay()

    # 1) 报案号 + 任务类型 + 查询
    claim_input = await _find_in_frames(
        page, [SELECTORS_PORTAL["task_transfer_claim_input"]], placeholder="报案号")
    if claim_input is None:
        raise RuntimeError(
            "任务中心-平级移交页未找到报案号输入框（选择器待真机确认："
            f"{SELECTORS_PORTAL['task_transfer_claim_input']}）"
        )
    await safe_fill(claim_input, claim_no, f"平级移交报案号 → {claim_no}")
    await _pick_transfer_task_type(page, task_type)
    await human_delay()

    query_btn = await _find_in_frames(
        page, [SELECTORS_PORTAL["task_transfer_query_btn"],
               "button:has-text('查询')", ".el-button:has-text('查询')"])
    if query_btn is None:
        raise RuntimeError(
            "任务中心-平级移交页未找到『查询』按钮（选择器待真机确认："
            f"{SELECTORS_PORTAL['task_transfer_query_btn']}）"
        )
    await safe_click(query_btn, "平级移交 → 查询按钮")
    await human_delay()

    # 2) 查询结果行 → 点「报案号」（生产是可点链接；mock/旧版有『移交』按钮）
    deadline = time.monotonic() + 15.0
    row = None
    tbody = None
    while time.monotonic() < deadline:
        tbody = await _find_in_frames(page, [SELECTORS_PORTAL["task_transfer_result_tbody"]])
        if tbody is not None:
            cand = tbody.locator(f"tr:has-text('{claim_no}')").first
            try:
                if await cand.count() > 0 and await cand.is_visible():
                    row = cand
                    break
            except Exception:
                pass
        # 结果区不是标准 table 时的兜底：直接找包含报案号的行/卡片
        cand = await _find_in_frames(page, [
            f"tr:has-text('{claim_no}')",
            f".el-table__row:has-text('{claim_no}')",
            f"li:has-text('{claim_no}')",
            f"[class*='row']:has-text('{claim_no}')",
            f"[class*='item']:has-text('{claim_no}')",
        ])
        if cand is not None:
            row = cand
            break
        await asyncio.sleep(0.5)
    if row is None:
        raise RuntimeError(
            f"平级移交查询结果中未找到报案号 {claim_no}（可能任务尚未转出，"
            f"或结果行选择器待真机确认：{SELECTORS_PORTAL['task_transfer_result_tbody']}）"
        )

    row_btn = row.locator(SELECTORS_PORTAL["task_transfer_row_btn"]).first
    # 2.1) 生产路径优先：勾选结果行 → 底部『批量平级移交』→ 任务提交弹窗
    modal = None
    try:
        cb = row.locator(".el-checkbox__input, .el-checkbox").first
        if await cb.count() > 0 and await cb.is_visible():
            await cb.click(timeout=2500)
            await asyncio.sleep(0.4)
            batch = await _find_in_frames(page, [
                "button:has-text('批量平级移交')",
                ".footer-btns button.el-button--default",
            ])
            if batch is not None:
                await safe_click(batch, "批量平级移交")
                for _ in range(8):
                    modal = await _find_in_frames(
                        page, [SELECTORS_PORTAL["task_transfer_modal"]])
                    if modal is not None:
                        break
                    await asyncio.sleep(0.5)
    except Exception:
        pass

    # 2.2) 没有复选框/批量按钮 → 点报案号（或行内『移交』按钮）
    if modal is None:
        if await row_btn.count() > 0 and await row_btn.is_visible():
            await safe_click(row_btn, f"查询结果行 → 移交（{claim_no}）")
        else:
            link = row.locator(SELECTORS_PORTAL["task_transfer_claim_link"]).filter(
                has_text=claim_no).first
            if await link.count() == 0:
                link = row.get_by_text(claim_no, exact=False).first
            if await link.count() == 0:
                raise RuntimeError(
                    f"查询结果行既没有『移交』按钮也没有可点的报案号 {claim_no}"
                )
            await safe_click(link, f"查询结果 → 点击报案号 {claim_no}")
        await human_delay()
        for _ in range(8):
            modal = await _find_in_frames(page, [SELECTORS_PORTAL["task_transfer_modal"]])
            if modal is not None:
                break
            await asyncio.sleep(0.5)

    # 2.3) 最后一轮：再试一次「勾选 + 批量平级移交」
    if modal is None:
        print("  [i] 仍未出现『任务提交』弹窗 → 再试勾选行 + 批量平级移交")
        try:
            cb = row.locator(".el-checkbox__input, .el-checkbox").first
            if await cb.count() > 0:
                await cb.click(timeout=2500)
                await asyncio.sleep(0.4)
        except Exception:
            pass
        batch = await _find_in_frames(page, [
            "button:has-text('批量平级移交')",
            ".footer-btns button.el-button--default",
        ])
        if batch is not None:
            await safe_click(batch, "批量平级移交")
        for _ in range(10):
            modal = await _find_in_frames(page, [SELECTORS_PORTAL["task_transfer_modal"]])
            if modal is not None:
                break
            await asyncio.sleep(0.5)
    if modal is None:
        raise RuntimeError(
            "『任务提交』弹窗未出现（已尝试 勾选+批量平级移交 与 点击报案号；"
            f"弹窗选择器：{SELECTORS_PORTAL['task_transfer_modal']}）"
        )

    # 弹窗外壳会先出现，目标人员输入框随后由 v-if/远程数据异步渲染；
    # 每轮重新获取弹窗，避免把刚出现弹窗时的空内容误判为流程不可处理。
    print("  [i] 『任务提交』弹窗已出现 → 等待目标人员输入框加载")
    log_audit("平级移交：任务提交弹窗已出现，等待目标人员输入框")
    target_input = None
    target_wait_seconds = 15.0
    target_deadline = time.monotonic() + target_wait_seconds
    while time.monotonic() < target_deadline:
        current_modal = await _find_in_frames(
            page, [SELECTORS_PORTAL["task_transfer_modal"]])
        if current_modal is not None:
            modal = current_modal
            target_input = await _find_in_frames(
                page, [SELECTORS_PORTAL["task_transfer_target_input"]], placeholder="员工代码")
            if target_input is None:
                # 兜底只取弹窗内可见、可编辑输入框；排除『组号/组员』，避免填进目标小组。
                try:
                    cand = modal.locator(
                        "input.el-input__inner:visible:not([readonly]):not([disabled])"
                        ":not([placeholder*='组号']):not([placeholder*='组员'])").first
                    if await cand.count() == 0:
                        cand = modal.locator(
                            "input:visible:not([readonly]):not([disabled])"
                            ":not([placeholder*='组号']):not([placeholder*='组员'])").first
                    if await cand.count() > 0 and await cand.is_visible():
                        target_input = cand
                except Exception:
                    target_input = None
            if target_input is not None:
                break
        await asyncio.sleep(0.5)
    if target_input is None:
        visible_fields: list[str] = []
        try:
            inputs = modal.locator("input:visible")
            for i in range(min(await inputs.count(), 8)):
                field = inputs.nth(i)
                visible_fields.append(
                    (await field.get_attribute("placeholder") or
                     await field.get_attribute("name") or
                     await field.get_attribute("type") or "未命名输入框")
                )
        except Exception:
            pass
        raise RuntimeError(
            f"『任务提交』弹窗未找到目标人员输入框（已等待 {target_wait_seconds:g} 秒，"
            f"当前选择器：{SELECTORS_PORTAL['task_transfer_target_input']}；"
            f"可见输入框：{visible_fields or '无'}）"
        )
    await _transfer_fill_target(page, target_input, target)
    target_val = ""
    try:
        target_val = (await target_input.input_value() or "").strip()
    except Exception:
        pass
    code, _ = _split_transfer_target(target)
    if not target_val or (code and code not in target_val):
        raise RuntimeError(
            f"目标人员未成功选中（输入框『{target_val or '空'}』，期望含工号 {code}）"
            " → 中断，避免提交空目标")
    await human_delay()

    submit_btn = await _find_in_frames(page, [SELECTORS_PORTAL["task_transfer_submit_btn"]])
    if submit_btn is None:
        submit_btn = modal.get_by_text("提交任务", exact=False).first
    if await submit_btn.count() == 0:
        raise RuntimeError(
            "『任务提交』弹窗未找到『提交任务』按钮（选择器待真机确认："
            f"{SELECTORS_PORTAL['task_transfer_submit_btn']}）"
        )
    await safe_click(submit_btn, "任务提交弹窗 → 提交任务")
    await human_delay()

    await _wait_page_result(
        page,
        success_keywords=["任务提交成功", "移交成功", "操作成功", "提交成功"],
        error_keywords=("移交失败", "提交失败", "操作失败", "无权限", "权限不足"),
        tip_key="task_transfer_tip",
        strict=True,
        label="平级移交",
    )
    print(f"  [✓] 平级移交成功：报案号 {claim_no} 已转回本人名下")
    log_audit(f"平级移交成功：报案号 {claim_no} 已转回本人名下")


# ============================================================
# 补救分支 3：任务转回后 → 重开报价处理页 → 报价完成 → 提交
# ============================================================
@register_step(desc="任务转回后重开报价处理页 → 报价完成 → 提交",
               inputs=["claim_no", "portal_url"])
async def step_quote_reopen_finalize(page: Page, context: dict, **kwargs) -> None:
    import bot.config as _cfg

    claim_no = kwargs.get("claim_no") or context.get("claim_no", "")
    portal_url = context.get("portal_url") or (
        MOCK_URLS["home"] if _cfg.USE_MOCK_PORTAL else "http://claim.internal.picc/portal/"
    )
    print(f"\n🔹 STEP 9c: 任务转回后重开报价处理页 → 报价完成（报案号 {claim_no}）")
    log_audit(f"重新进入报价处理：报案号 {claim_no}")

    # 提交上级后原报价 tab 常被平台自动关闭 → 换一个可用页面再打开门户
    page = await _live_page(page)
    await page.goto(portal_url, wait_until="networkidle")
    await asyncio.sleep(1)
    # 生产门户点报案号在新标签页打开报价处理 → 用返回的页面对象继续
    page = await step_home_click_claim(page, context, claim_no=claim_no)
    try:
        await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    submit_sels = list(QUOTE_SUBMIT_SELECTORS)
    try:
        await find_clickable(page, submit_sels, ["提交", "提交任务"],
                             "报价处理页提交按钮", timeout=6000)
    except RuntimeError:
        # 兜底：部分站点点报案号后先到案件详情页，需要再点「报价处理」
        entry = page.get_by_text("报价处理", exact=False).first
        if await entry.count() > 0:
            await safe_click(entry, "案件详情 → 报价处理（文本兜底）")
            await human_delay()
        try:
            await find_clickable(page, submit_sels, ["提交", "提交任务"],
                                 "报价处理页提交按钮", timeout=12000)
        except RuntimeError as exc:
            raise RuntimeError(f"未能进入报案号 {claim_no} 的报价处理页：{exc}")

    # 复用 STEP 9：报价完成 → 提交 → 校验（若仍权限不足会再次抛出，不做静默成功）
    await step_quote_finalize(page, context)
    print(f"  [✓] 任务转回后『报价完成』提交成功（报案号 {claim_no}）")
    log_audit(f"任务转回后报价完成提交成功：报案号 {claim_no}")
