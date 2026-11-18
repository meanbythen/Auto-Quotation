"""
录制引擎 —— 在浏览器里注入 JS 监听用户交互，输出结构化 step JSON

设计:
  1. page.add_init_script() 在所有页面（包括 popup/新标签）注入 recorder.js
  2. JS 侧把 click/input/select/navigation 事件推到 window.__recorder_buffer__
  3. Python 侧定时 page.evaluate() 拉走 buffer，整理成 step 列表
  4. 录制结束后保存到 data/workflows/<name>.json

每个 step 结构:
  {
    "index": 1,
    "ts": "2026-10-29T16:00:00.123",
    "url": "http://127.0.0.1:8765/quote_process.html",
    "action": "click" | "fill" | "select" | "press" | "navigate" | "dialog",
    "selector": "#submit-loss-btn",     # 优先 data-testid → id → css path
    "text": "提交定损单",                # 元素可见文本
    "value": "",                        # fill/select 的值
    "tag": "button",                    # 元素标签
    "data_testid": "submit-loss-btn",   # data-testid（如果有）
    "confidence": "high" | "medium" | "low"  # selector 稳定度
  }
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from playwright.async_api import Page, BrowserContext


# ================================================================
# 注入页面的 JS 脚本 —— 核心录制逻辑
# ================================================================
RECORDER_JS = r"""
(() => {
  if (window.__recorder_installed__) return;
  window.__recorder_installed__ = true;

  const KEY = "__recorder_persist__";
  // 跨页面跳转保留已录动作（点击报案号/翻页常导致整页刷新，内存 buffer 会被清空）
  let buf = [];
  try { buf = JSON.parse(sessionStorage.getItem(KEY) || "[]") || []; } catch (e) { buf = []; }
  window.__recorder_buffer__ = buf;
  window.__recorder_active__ = true;

  function persist() {
    try { sessionStorage.setItem(KEY, JSON.stringify(buf)); } catch (e) {}
  }

  // 供 Python 侧一次性取走（同时清空内存与 sessionStorage，避免重复）
  window.__recorder_drain__ = function () {
    const out = buf.splice(0, buf.length);
    try { sessionStorage.removeItem(KEY); } catch (e) {}
    return out;
  };

  function now() { return new Date().toISOString(); }

  // 更宽的“可点击元素”判定：生产门户大量用 div / span / li / td 当按钮（没有 button/a 标签）
  const CLICK_SEL = 'button, a, input, select, textarea, label, summary, ' +
                    '[role="button"], [role="tab"], [role="menuitem"], [role="option"], ' +
                    '[onclick], [onmousedown], [data-action]';
  function clickableOf(t) {
    if (!t || t.nodeType !== 1) return null;
    let el = t;
    for (let i = 0; i < 8 && el && el.nodeType === 1; i++, el = el.parentElement) {
      if (el.matches && el.matches(CLICK_SEL)) return el;
      let st;
      try { st = getComputedStyle(el); } catch (e) { st = null; }
      const cls = (el.className && el.className.toString ? el.className.toString() : '') || '';
      const txt = (el.innerText || el.textContent || '').trim();
      if (!st) continue;
      if (st.cursor === 'pointer' && txt.length <= 40 && el.children.length <= 6) return el;
      if (/btn|button|link|click|tab|menu|submit/i.test(cls) && txt.length <= 40) return el;
    }
    return null;
  }

  // 生成 selector —— 优先 data-testid，其次 id，最后 nth css path
  function genSelector(el) {
    if (!(el instanceof Element)) return "";
    // 1) data-testid（最稳定）
    if (el.dataset.testid) return `[data-testid="${el.dataset.testid}"]`;
    // 2) id
    if (el.id) return `#${CSS.escape(el.id)}`;
    // 3) name + tag
    if (el.name && el.tagName !== 'BUTTON') return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
    // 4) css path（dompath）
    const parts = [];
    let e = el;
    while (e && e.nodeType === 1 && parts.length < 6) {
      let sel = e.tagName.toLowerCase();
      const parent = e.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter(c => c.tagName === e.tagName);
        if (sameTag.length > 1) {
          const idx = sameTag.indexOf(e) + 1;
          sel += `:nth-of-type(${idx})`;
        }
      }
      parts.unshift(sel);
      e = e.parentElement;
    }
    return parts.join(' > ');
  }

  function push(action, el, extra = {}) {
    if (!window.__recorder_active__) return;
    const selector = el ? genSelector(el) : "";
    buf.push({
      index: buf.length + 1,
      ts: now(),
      url: location.href,
      action,
      selector,
      text: el ? (el.innerText || el.value || "").toString().trim().slice(0, 80) : "",
      tag: el ? el.tagName.toLowerCase() : "",
      data_testid: el ? (el.dataset.testid || "") : "",
      html: el && el.outerHTML ? el.outerHTML.slice(0, 200) : "",
      ...extra,
    });
    persist();
  }

  // ---- click（按钮 / 链接 / 单选复选 / 手型 div-span-li）----
  document.addEventListener('click', (ev) => {
    const el = clickableOf(ev.target);
    if (el) push('click', el);
    else if (ev.target && ev.target.nodeType === 1) {
      // 兜底：未知元素也记一条（生产门户的奇怪控件）
      const txt = (ev.target.innerText || '').trim();
      if (txt && txt.length <= 40) push('click', ev.target);
    }
  }, true);

  // ---- input/fill（输入防抖，失焦时 change 再补一条；同值不重复）----
  const fillTimers = new WeakMap();
  const lastFill = new WeakMap();
  function pushFill(el) {
    const v = (el.value === null || el.value === undefined ? "" : String(el.value)).slice(0, 120);
    if (lastFill.get(el) === v) return;
    lastFill.set(el, v);
    push('fill', el, { value: v });
  }

  document.addEventListener('input', (ev) => {
    const el = ev.target;
    if (!el || !el.matches || !el.matches('input, select, textarea')) return;
    clearTimeout(fillTimers.get(el));
    fillTimers.set(el, setTimeout(() => pushFill(el), 600));
  }, true);

  document.addEventListener('change', (ev) => {
    const el = ev.target;
    if (!el || !el.matches || !el.matches('input, select, textarea')) return;
    clearTimeout(fillTimers.get(el));
    pushFill(el);
  }, true);

  // ---- keydown (Enter 等特殊键) ----
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === 'Escape' || (ev.ctrlKey && ev.key === 'Enter')) {
      push('press', document.activeElement, { value: ev.key });
    }
  }, true);

  // ---- dialog (alert/confirm/prompt) ----
  ['alert', 'confirm', 'prompt'].forEach((fn) => {
    const orig = window[fn];
    if (typeof orig !== 'function') return;
    window[fn] = function(msg) {
      push('dialog', null, { value: fn + ': ' + String(msg).slice(0, 200) });
      return orig.apply(this, arguments);
    };
  });

  // ---- 表单提交 ----
  document.addEventListener('submit', (ev) => push('submit', ev.target), true);

  // ---- window.open（生产门户常用它开新窗口）----
  const _origOpen = window.open;
  if (typeof _origOpen === 'function') {
    window.open = function(url) {
      push('open', null, { value: String(url || '') });
      return _origOpen.apply(this, arguments);
    };
  }

  // ---- SPA hash change (navigation) ----
  window.addEventListener('hashchange', () => {
    push('navigate', null, { value: location.hash || location.pathname });
  });

  window.addEventListener('popstate', () => {
    push('navigate', null, { value: location.href });
  });
})();
"""


# ================================================================
# Python 侧录制引擎
# ================================================================
class WorkflowStep:
    """单步动作的结构化表示"""
    def __init__(self, raw: Dict[str, Any]):
        self.index = raw.get("index", 0)
        self.ts = raw.get("ts", "")
        self.url = raw.get("url", "")
        self.action = raw.get("action", "")
        self.selector = raw.get("selector", "")
        self.text = raw.get("text", "")
        self.value = raw.get("value", "")
        self.tag = raw.get("tag", "")
        self.data_testid = raw.get("data_testid", "")
        self.html = raw.get("html", "")

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @property
    def canonical(self) -> str:
        """canonical form 用于 diff：action + 核心标识"""
        key = self.data_testid or self.selector or self.text
        # 归一化：报案号里的时间戳、URL query 参数都去掉
        key = re.sub(r'RD[A-Za-z]{2}[0-9A-Za-z]{16,20}', '<CLAIM>', key)
        key = re.sub(r'\?.*$', '', key)
        return f"{self.action}|{self.tag}|{key}"

    def __repr__(self):
        return f"[{self.index:02d}] {self.action:8s} {self.tag or '':8s} {self.selector[:50] or self.text[:30]}"


class Recorder:
    """
    Playwright 录制器

    用法:
      async with async_playwright() as p:
          browser = await p.chromium.launch(headless=False)
          context = await browser.new_context()
          recorder = Recorder()
          await recorder.attach(context)   # 注入所有 page
          # ... 用户手动操作 ...
          steps = await recorder.collect() # 拉走所有 buffer
          recorder.stop()                  # 停止注入
          await recorder.save(steps, "flow_A")
    """

    def __init__(self, save_dir: Path | str = "data/workflows"):
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._pages: List[Page] = []
        self._context: Optional[BrowserContext] = None
        self._recording = False
        self._start_time: Optional[float] = None
        self._urls: Dict[Any, str] = {}
        self._events: List[Dict[str, Any]] = []   # 系统响应 / 页面跳转 / 新窗口 / 关闭

    # ---------------- 系统响应事件（页面跳转、XHR、新开窗口） ----------------
    def _add_event(self, action: str, url: str = "", text: str = "", value: str = ""):
        self._events.append({
            "ts": datetime.now().isoformat(),
            "url": url,
            "action": action,
            "selector": "",
            "text": text,
            "tag": "",
            "value": value,
            "html": "",
            "data_testid": "",
        })
        if len(self._events) > 4000:
            del self._events[: len(self._events) - 4000]

    def _on_response(self, resp):
        """记录 XHR / fetch / 页面文档响应（生产环境的接口调用与状态码）"""
        try:
            if resp.request.resource_type not in ("xhr", "fetch", "document"):
                return
            u = resp.url
            if not u.startswith("http"):
                return
            self._add_event("response", u,
                            text=f"{resp.request.method} {resp.status}",
                            value=str(resp.status))
        except Exception:
            pass

    def _watch(self, page: Page):
        if page in self._pages:
            return
        self._pages.append(page)
        self._urls[page] = ""
        try:
            page.on("response", self._on_response)
            page.on("close", lambda p=page: self._add_event(
                "close", self._urls.get(p, ""), text="窗口关闭"))
        except Exception:
            pass

    async def _inject_page(self, page: Page):
        """注入主页面 + 所有 iframe（生产门户常用 frameset/iframe）"""
        try:
            await page.add_init_script(RECORDER_JS)
        except Exception:
            pass
        for frame in page.frames:
            try:
                await frame.evaluate(RECORDER_JS)
            except Exception:
                pass

    async def attach(self, context: BrowserContext):
        """给 context 里所有 page/frame（包括未来新建的 popup）注入 recorder JS"""
        self._context = context
        for page in list(context.pages):
            self._watch(page)
            await self._inject_page(page)

        context.on("page", lambda p: self._on_new_page(p))
        self._recording = True
        self._start_time = time.time()

    def _on_new_page(self, page: Page):
        self._watch(page)
        self._add_event("open", "", text="新窗口/新标签页")

        async def _inject():
            try:
                await page.add_init_script(RECORDER_JS)
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
                await self._inject_page(page)
            except Exception:
                pass

        try:
            asyncio.create_task(_inject())
        except Exception:
            pass

    async def status(self) -> Dict[str, Any]:
        """录制诊断：窗口数 / frame 数 / 已注入数（0 注入说明录制没生效）"""
        pages = [p for p in self._pages if not p.is_closed()]
        frames = []
        for p in pages:
            frames.extend(p.frames)
        installed = 0
        for f in frames:
            try:
                if await f.evaluate("() => !!window.__recorder_installed__"):
                    installed += 1
            except Exception:
                pass
        return {"pages": len(pages), "frames": len(frames), "installed": installed}

    async def collect(self) -> List[WorkflowStep]:
        """从所有 page / frame 拉取动作，并合并页面跳转、XHR 等系统响应"""
        collected: List[Dict[str, Any]] = []

        for page in list(self._pages):
            if page.is_closed():
                continue
            # 页面跳转（整页刷新 / 新地址）也算一条动作，便于看清流程
            cur = page.url or ""
            prev = self._urls.get(page, "")
            if cur and cur != prev:
                self._urls[page] = cur
                self._add_event("navigate", cur, text="页面跳转", value=cur)

            for frame in page.frames:
                try:
                    raw = await frame.evaluate("""
                        () => {
                            if (!window.__recorder_installed__) return null;
                            if (window.__recorder_drain__) return window.__recorder_drain__();
                            const buf = window.__recorder_buffer__ || [];
                            window.__recorder_buffer__ = [];
                            return buf;
                        }
                    """)
                except Exception:
                    raw = None
                if raw is None:
                    try:
                        await frame.evaluate(RECORDER_JS)
                    except Exception:
                        pass
                    continue
                collected.extend(raw or [])

        collected.extend(self._events)
        self._events = []

        # 去重（同 ts + action + selector + value 视为同一条）
        seen = set()
        steps: List[Dict[str, Any]] = []
        for s in collected:
            key = (s.get("ts"), s.get("action"), s.get("selector"), s.get("value"))
            if key in seen:
                continue
            seen.add(key)
            steps.append(s)

        steps.sort(key=lambda x: x.get("ts", ""))
        for i, s in enumerate(steps, 1):
            s["index"] = i

        return [WorkflowStep(s) for s in steps]

    def stop(self):
        self._recording = False

    async def save(self, steps: List[WorkflowStep], name: str, meta: Optional[Dict] = None) -> Path:
        """保存录制到 JSON 文件"""
        out = self.save_dir / f"{name}.json"
        payload = {
            "name": name,
            "recorded_at": datetime.now().isoformat(),
            "duration_sec": round(time.time() - (self._start_time or time.time()), 1),
            "step_count": len(steps),
            "meta": meta or {},
            "steps": [s.to_dict() for s in steps],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    @staticmethod
    def load(name_or_path: str) -> Dict[str, Any]:
        """加载已保存的录制"""
        p = Path(name_or_path)
        if not p.exists():
            p = Path("data/workflows") / f"{name_or_path}.json"
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def list_recordings(save_dir: Path | str = "data/workflows") -> List[str]:
        d = Path(save_dir)
        if not d.exists():
            return []
        return sorted([f.stem for f in d.glob("*.json")])


# ================================================================
# 把现有 bot step 也转成 canonical 形式用于比对
# ================================================================
def get_expected_steps() -> List[Dict[str, Any]]:
    """
    返回机器人"理论上应该执行的"步骤 canonical 列表。
    用于和用户录制的流程做 diff。

    真实实现：让每个 step_* 函数在执行时 yield 一条 canonical step。
    这里先硬编码一份"黄金标准"，后续改成 decorator 自动采集。
    """
    return [
        {"action": "click",     "selector": "[data-testid='claim-item']",        "tag": "button",  "text": "报案号列表项"},
        {"action": "click",     "selector": "#open-loss-edit",                   "tag": "button",  "text": "编辑"},
        {"action": "click",     "selector": "#select-all-parts",                 "tag": "input",   "text": "全选零件"},
        {"action": "click",     "selector": ".table-toolbar button:nth-child(2)","tag": "button",  "text": "同步定损"},
        {"action": "fill",      "selector": "#vin-input",                        "tag": "input",   "text": "VIN输入框"},
        {"action": "click",     "selector": "#vin-search-btn",                   "tag": "button",  "text": "VIN查询"},
        {"action": "fill",      "selector": "#part-input",                       "tag": "input",   "text": "零件编码输入"},
        {"action": "click",     "selector": "#part-search-btn",                  "tag": "button",  "text": "零件查询"},
        {"action": "click",     "selector": "#save-draft-btn",                   "tag": "button",  "text": "暂存"},
        {"action": "click",     "selector": "#submit-loss-btn",                  "tag": "button",  "text": "提交定损单"},
        {"action": "click",     "selector": "#submit-loss-order",                "tag": "button",  "text": "提交定损单弹窗确认"},
        {"action": "click",     "selector": "input[name='biz-action'][value='quote-done']", "tag": "input", "text": "报价完成"},
        {"action": "click",     "selector": "#submit-quote-btn",                  "tag": "button",  "text": "提交报价"},
        {"action": "click",     "selector": "#confirm-quote-submit",              "tag": "button",  "text": "提交任务确认"},
    ]
