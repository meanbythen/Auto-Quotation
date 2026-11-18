"""
页面结构采集 —— 把当前（生产）页面的页面代码 / 表单控件 / 下拉选项 / 表格结构抓下来

用途：
  1) 真实内网 DOM 不透明时，用 snapshot.json + page.html 对着改 mock_web
  2) 生产采集 + mock 采集 → diff_snapshots() 生成结构差异报告，
     直接指出 mock 少了哪些控件 / 下拉选项 / 表格表头

落盘：data/capture/<ts>-<name>/
  snapshot.json   结构化摘要（控件 / 下拉 / 表格）
  page.html       完整页面代码
  page.png        整页截图（可选）
  report.md       可读报告（生产页报告 / 生产 vs mock 差异）

注意：只读页面，不点击任何按钮。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

PAGES_REPORT = "report.md"

# 单次采集上限，避免超大生产页面把报告撑爆
MAX_CONTROLS = 300
MAX_SELECTS = 30
MAX_TABLES = 8
MAX_ROWS = 5


# ============================================================
# 页面探测 JS（一次性返回结构化摘要）
# ============================================================
PROBE_JS = r"""
() => {
  const MAXC = 300, MAXS = 30, MAXT = 8, MAXR = 5;
  const clip = (s, n) => (s === null || s === undefined ? "" : String(s))
                        .replace(/\s+/g, " ").trim().slice(0, n || 120);

  function selectorOf(el) {
    try {
      if (el.dataset && el.dataset.testid) return `[data-testid="${el.dataset.testid}"]`;
      if (el.id) return "#" + CSS.escape(el.id);
      const nm = el.getAttribute && el.getAttribute("name");
      if (nm && el.tagName !== "BUTTON") return el.tagName.toLowerCase() + `[name="${nm}"]`;
      const parts = [];
      let e = el;
      while (e && e.nodeType === 1 && parts.length < 5) {
        let s = e.tagName.toLowerCase();
        const p = e.parentElement;
        if (p) {
          const same = Array.from(p.children).filter(c => c.tagName === e.tagName);
          if (same.length > 1) s += `:nth-of-type(${same.indexOf(e) + 1})`;
        }
        parts.unshift(s);
        e = e.parentElement;
      }
      return parts.join(" > ");
    } catch (err) { return ""; }
  }

  function labelOf(el) {
    try {
      if (el.labels && el.labels.length) return clip(el.labels[0].innerText, 80);
      const aria = el.getAttribute("aria-label");
      if (aria) return clip(aria, 80);
      const wrap = el.closest("label, .form-item, .el-form-item, .el-form-item__label, td");
      return wrap ? clip(wrap.innerText, 80) : "";
    } catch (err) { return ""; }
  }

  function visible(el) {
    try {
      if (el.offsetParent !== null) return true;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    } catch (err) { return false; }
  }

  const controls = [];
  const selects = [];
  const tables = [];

  const nodes = document.querySelectorAll(
    "input, select, textarea, button, a[role='button'], [role='button'], [onclick]");
  for (const el of nodes) {
    if (controls.length >= MAXC) break;
    const tag = el.tagName.toLowerCase();
    controls.push({
      tag: tag,
      type: (el.getAttribute("type") || "").toLowerCase(),
      id: el.id || "",
      name: el.getAttribute("name") || "",
      placeholder: el.getAttribute("placeholder") || "",
      value: clip(el.value, 80),
      label: labelOf(el),
      text: clip(el.innerText, 80),
      required: !!(el.required || el.getAttribute("required")),
      disabled: !!(el.disabled || el.getAttribute("disabled")),
      visible: visible(el),
      selector: selectorOf(el),
    });
  }

  for (const sel of document.querySelectorAll("select")) {
    if (selects.length >= MAXS) break;
    const opts = [];
    for (const o of sel.options) {
      opts.push({ value: o.value, text: clip(o.text, 80), selected: o.selected });
      if (opts.length >= 60) break;
    }
    selects.push({
      id: sel.id || "",
      name: sel.getAttribute("name") || "",
      label: labelOf(sel),
      selector: selectorOf(sel),
      multiple: !!sel.multiple,
      options: opts,
    });
  }

  for (const tbl of document.querySelectorAll("table")) {
    if (tables.length >= MAXT) break;
    const headers = [];
    const headRow = tbl.querySelector("thead tr") || tbl.querySelector("tr");
    if (headRow) {
      for (const th of headRow.children) {
        headers.push(clip(th.innerText, 60));
        if (headers.length >= 40) break;
      }
    }
    const rows = [];
    const bodyRows = tbl.querySelectorAll("tbody tr");
    const src = bodyRows.length ? bodyRows : tbl.querySelectorAll("tr");
    for (const tr of src) {
      if (rows.length >= MAXR) break;
      if (headRow && tr === headRow) continue;
      const cells = [];
      for (const td of tr.children) {
        cells.push(clip(td.innerText, 60));
        if (cells.length >= 40) break;
      }
      if (cells.length) rows.push(cells);
    }
    tables.push({
      id: tbl.id || "",
      headers: headers,
      rows: rows,
      row_count: Math.max(0, src.length - (headRow ? 1 : 0)),
      selector: selectorOf(tbl),
    });
  }

  return {
    title: document.title || "",
    url: location.href,
    ready_state: document.readyState,
    controls: controls,
    selects: selects,
    tables: tables,
    body_text: clip(document.body ? document.body.innerText : "", 400),
  };
}
"""


# ============================================================
# 路径与工具
# ============================================================
def capture_dir() -> Path:
    from bot.logging_system import app_data_dir
    d = app_data_dir() / "capture"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", name or "capture", flags=re.UNICODE)
    return s.strip("_")[:40] or "capture"


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ============================================================
# 采集单页
# ============================================================
async def capture_page_snapshot(page, name: str, *, out_dir: Path | None = None,
                                screenshot: bool = True,
                                max_controls: int = MAX_CONTROLS) -> dict:
    """采集一个页面 → 返回结构化摘要（同时落盘 json/html/png）"""
    folder = Path(out_dir) if out_dir else capture_dir() / f"{_ts()}-{_safe(name)}"
    folder.mkdir(parents=True, exist_ok=True)

    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        title = await page.title()
    except Exception:
        title = ""

    probe: dict = {"title": title, "url": url, "controls": [], "selects": [], "tables": []}
    try:
        raw = await page.evaluate(PROBE_JS)
        if isinstance(raw, dict):
            probe.update(raw)
    except Exception as exc:
        probe["probe_error"] = str(exc).splitlines()[0][:200]

    controls = (probe.get("controls") or [])[:max_controls]
    snap = {
        "name": name,
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "url": probe.get("url") or url,
        "title": probe.get("title") or title,
        "ready_state": probe.get("ready_state", ""),
        "controls": controls,
        "selects": (probe.get("selects") or [])[:MAX_SELECTS],
        "tables": (probe.get("tables") or [])[:MAX_TABLES],
        "body_text": probe.get("body_text", ""),
        "dir": str(folder),
        "files": {},
    }
    if probe.get("probe_error"):
        snap["probe_error"] = probe["probe_error"]

    # 完整页面代码
    try:
        html = await page.content()
        p = folder / "page.html"
        p.write_text(html, encoding="utf-8")
        snap["files"]["html"] = p.name
        snap["html_chars"] = len(html)
    except Exception as exc:
        snap["html_error"] = str(exc).splitlines()[0][:200]

    # 子 frame：真实门户的定损/报价表单常嵌在 iframe 里（底部「返回/暂存/提交」操作条就在其中）
    frames_info = []
    for i, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        item: dict = {"index": i, "url": "", "name": ""}
        try:
            item["url"] = (frame.url or "")[:200]
            item["name"] = frame.name or ""
        except Exception:
            pass
        try:
            raw = await frame.evaluate(PROBE_JS)
            if isinstance(raw, dict):
                item["controls"] = (raw.get("controls") or [])[:60]
                item["body_text"] = raw.get("body_text") or ""
        except Exception as exc:
            item["probe_error"] = str(exc).splitlines()[0][:160]
        try:
            fhtml = await frame.content()
            fp = folder / f"frame-{i}.html"
            fp.write_text(fhtml, encoding="utf-8")
            item["html"] = fp.name
            item["html_chars"] = len(fhtml)
        except Exception as exc:
            item["html_error"] = str(exc).splitlines()[0][:160]
        frames_info.append(item)
    if frames_info:
        snap["frames"] = frames_info

    # 整页截图
    if screenshot:
        try:
            png = folder / "page.png"
            await page.screenshot(path=str(png), full_page=True)
            snap["files"]["png"] = png.name
        except Exception as exc:
            snap["png_error"] = str(exc).splitlines()[0][:200]

    meta = folder / "snapshot.json"
    meta.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    snap["files"]["json"] = meta.name
    return snap


# ============================================================
# 采集整套环境（门户多页 + 一车一件）
# ============================================================
async def capture_environment(env, name: str, *, screenshot: bool = True,
                              out_dir: Path | None = None) -> dict:
    """采集门户所有页面 + 一车一件页面，返回 {dir, snapshots}"""
    folder = Path(out_dir) if out_dir else capture_dir() / f"{_ts()}-{_safe(name)}"
    folder.mkdir(parents=True, exist_ok=True)

    snapshots: list[dict] = []

    async def _one(page, label: str):
        try:
            snap = await capture_page_snapshot(page, label, out_dir=folder,
                                               screenshot=screenshot)
            snapshots.append(snap)
            print(f"  📸 已采集 {label}  {snap.get('url','')}")
            return snap
        except Exception as exc:
            print(f"  ⚠️ 采集 {label} 失败: {exc}")
            return None

    portal_pages: list = []
    try:
        portal_pages = list(env.portal_pages)
    except Exception:
        pass
    if not portal_pages:
        try:
            portal_pages = [env.portal_page]
        except Exception:
            portal_pages = []

    main = None
    try:
        main = env.portal_page
    except Exception:
        pass

    # 门户主页面优先，其余 popup 依次编号
    if main is not None:
        portal_pages = [main] + [p for p in portal_pages if p != main]
    for i, pg in enumerate(portal_pages, 1):
        label = "portal-main" if i == 1 else f"portal-popup-{i}"
        await _one(pg, label)

    try:
        await _one(env.market_page, "market-main")
    except Exception:
        pass

    result = {"dir": str(folder), "snapshots": snapshots}
    write_report(snapshots, folder, title=f"页面结构采集 · {name}")
    return result


# ============================================================
# 结构差异（生产 vs mock）
# ============================================================
def _control_key(c: dict) -> tuple:
    return (c.get("tag", ""), c.get("id", ""), c.get("name", ""),
            c.get("placeholder", ""), c.get("type", ""))


def _control_label(c: dict) -> str:
    ident = c.get("id") or (f"{c.get('tag')}[name={c.get('name')}]" if c.get("name") else c.get("selector",""))
    extra = c.get("label") or c.get("text") or c.get("placeholder") or ""
    return f"{c.get('tag','')} {ident} {extra}".strip()


def diff_snapshots(prod: dict, mock: dict) -> dict:
    """对比两页结构，返回 {missing_in_mock, extra_in_mock, select_diffs, table_diffs}"""
    prod_keys = {_control_key(c): c for c in prod.get("controls", [])}
    mock_keys = {_control_key(c): c for c in mock.get("controls", [])}
    missing = [_control_label(prod_keys[k]) for k in prod_keys if k not in mock_keys]
    extra = [_control_label(mock_keys[k]) for k in mock_keys if k not in prod_keys]

    def _sel_key(s):
        return s.get("id") or s.get("name") or s.get("selector", "")

    mock_sels = {_sel_key(s): s for s in mock.get("selects", [])}
    select_diffs = []
    for s in prod.get("selects", []):
        m = mock_sels.get(_sel_key(s))
        if not m:
            select_diffs.append({
                "select": _sel_key(s) or s.get("label", ""),
                "missing_options": [o.get("text") for o in s.get("options", [])],
                "extra_options": [],
                "note": "mock 无对应下拉",
            })
            continue
        p_vals = {(o.get("value"), o.get("text")) for o in s.get("options", [])}
        m_vals = {(o.get("value"), o.get("text")) for o in m.get("options", [])}
        if p_vals != m_vals:
            select_diffs.append({
                "select": _sel_key(s) or s.get("label", ""),
                "missing_options": [t for (v, t) in p_vals - m_vals],
                "extra_options": [t for (v, t) in m_vals - p_vals],
                "note": "选项不一致",
            })

    table_diffs = []
    mock_tables = mock.get("tables", [])
    for i, t in enumerate(prod.get("tables", [])):
        mt = mock_tables[i] if i < len(mock_tables) else None
        p_h = t.get("headers") or []
        m_h = (mt or {}).get("headers") or []
        if p_h != m_h:
            table_diffs.append({
                "table": t.get("id") or t.get("selector") or f"第 {i+1} 张表",
                "missing_headers": [h for h in p_h if h not in m_h],
                "extra_headers": [h for h in m_h if h not in p_h],
                "note": "表头不一致" if mt else "mock 无对应表格",
            })

    return {
        "url_prod": prod.get("url", ""),
        "url_mock": mock.get("url", ""),
        "missing_in_mock": missing,
        "extra_in_mock": extra,
        "select_diffs": select_diffs,
        "table_diffs": table_diffs,
    }


# ============================================================
# Markdown 报告
# ============================================================
def _render_page(snap: dict) -> list[str]:
    out = [f"### {snap.get('name','')} — {snap.get('title','') or '(无标题)'}", ""]
    out.append(f"- URL: `{snap.get('url','')}`")
    out.append(f"- 采集时间: {snap.get('captured_at','')}")
    out.append(f"- 控件 {len(snap.get('controls', []))} 个 · 下拉 {len(snap.get('selects', []))} 个 · 表格 {len(snap.get('tables', []))} 张")
    files = snap.get("files") or {}
    if files:
        out.append(f"- 文件: {', '.join(f'{k}={v}' for k, v in files.items())}")
    out.append("")

    controls = snap.get("controls", [])
    if controls:
        out += ["| 标签 | 类型 | id | name | 占位符 | 名称/文本 | 选择器 |",
                "|---|---|---|---|---|---|---|"]
        for c in controls:
            out.append(
                f"| {c.get('tag','')} | {c.get('type','')} | {c.get('id','')} | "
                f"{c.get('name','')} | {c.get('placeholder','')} | "
                f"{(c.get('label') or c.get('text') or '')[:30]} | `{c.get('selector','')[:60]}` |")
        out.append("")

    for s in snap.get("selects", []):
        opts = " / ".join(f"{o.get('text')}({o.get('value')})" for o in s.get("options", [])[:20])
        out.append(f"- 下拉 `{s.get('selector','')}` {('['+s.get('label','')+']') if s.get('label') else ''}：{opts}")
    if snap.get("selects"):
        out.append("")

    for t in snap.get("tables", []):
        out.append(f"- 表格 `{t.get('selector','')}` 行数≈{t.get('row_count',0)} 表头: "
                   f"{' | '.join(t.get('headers', []))}")
        for row in t.get("rows", [])[:3]:
            out.append(f"  - {' | '.join(row)}")
    if snap.get("tables"):
        out.append("")
    return out


def write_report(snapshots: list[dict], out_dir, *, title: str = "页面结构采集",
                 mock_snapshots: list[dict] | None = None) -> Path:
    """写 Markdown 报告（若给了 mock 快照，则追加「生产 vs mock」结构差异）"""
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)

    lines = [f"# {title}", "",
             f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 页面数: {len(snapshots)}", ""]

    for snap in snapshots:
        lines += _render_page(snap)

    if mock_snapshots:
        lines += ["## 生产 vs mock 结构差异", "",
                  "> 目标：按下面差异补 mock_web，让模拟环境贴合生产环境。", ""]
        mock_by_name = {m.get("name", ""): m for m in mock_snapshots}
        for snap in snapshots:
            mock = mock_by_name.get(snap.get("name", ""))
            if not mock:
                lines += [f"### {snap.get('name','')}", "",
                          "- mock 中未采集到同名页面（mock 缺少该页面）", ""]
                continue
            diff = diff_snapshots(snap, mock)
            lines += [f"### {snap.get('name','')}", ""]
            if not (diff["missing_in_mock"] or diff["extra_in_mock"]
                    or diff["select_diffs"] or diff["table_diffs"]):
                lines += ["- ✅ 结构一致（控件 / 下拉 / 表头均匹配）", ""]
                continue
            if diff["missing_in_mock"]:
                lines.append("- mock 缺少的控件:")
                lines += [f"  - `{x}`" for x in diff["missing_in_mock"][:40]]
            if diff["extra_in_mock"]:
                lines.append("- mock 多出的控件（生产没有，可清理）:")
                lines += [f"  - `{x}`" for x in diff["extra_in_mock"][:40]]
            for sd in diff["select_diffs"]:
                lines.append(f"- 下拉 `{sd['select']}` {sd['note']}: "
                             f"缺 {sd['missing_options'][:10]} / 多 {sd['extra_options'][:10]}")
            for td in diff["table_diffs"]:
                lines.append(f"- 表格 `{td['table']}` {td['note']}: "
                             f"缺表头 {td['missing_headers'][:10]} / 多表头 {td['extra_headers'][:10]}")
            lines.append("")

    path = folder / PAGES_REPORT
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
