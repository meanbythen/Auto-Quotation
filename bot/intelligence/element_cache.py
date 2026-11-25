"""UI 元素缓存

把「某页面范围内某操作成功命中的 CSS 选择器」记下来，下次优先尝试，
省掉多轮选择器遍历与文字扫描的开销。

正确性保证：
- 缓存只是「先试一次」，失败立刻回退原有扫描，不影响定位结果
- TTL（默认 30 天）内未成功过的记录不再使用
- 每次未命中都累加 misses，便于回查失效的选择器
"""
from __future__ import annotations

from urllib.parse import urlparse
from typing import Optional

import bot.config as cfg


def enabled() -> bool:
    return bool(cfg.ELEMENT_CACHE_ENABLED)


def scope_for(page) -> str:
    """页面范围：域名 + 前两段路径（SPA 下避免按每个报案号切碎缓存）"""
    try:
        u = urlparse(getattr(page, "url", "") or "")
    except Exception:
        return ""
    if u.scheme == "file":
        name = (u.path or "").replace("\\", "/").rstrip("/").split("/")[-1]
        return f"file/{name}" if name else ""
    if not u.netloc:
        return ""
    segs = [s for s in (u.path or "").split("/") if s][:2]
    return f"{u.netloc}/{'/'.join(segs)}" if segs else u.netloc


def _store():
    from bot.pricing import get_price_service
    return get_price_service().store


def candidates(scope: str, key: str, limit: int = 2) -> list[str]:
    if not enabled() or not scope or not key:
        return []
    try:
        return _store().get_ui_selectors(scope, key, limit, cfg.ELEMENT_CACHE_TTL_DAYS)
    except Exception:
        return []


def remember(scope: str, key: str, selector: str) -> None:
    """记录一次正常扫描的成功命中（不计入缓存命中数）"""
    if not enabled() or not (scope and key and selector):
        return
    try:
        _store().touch_ui_success(scope, key, selector, reused=0)
    except Exception:
        pass


def hit(scope: str, key: str, selector: str) -> None:
    """记录一次「直接复用缓存选择器」的成功（计入命中率）"""
    if not enabled() or not (scope and key and selector):
        return
    try:
        _store().touch_ui_success(scope, key, selector, reused=1)
    except Exception:
        pass


def miss(scope: str, key: str, selector: str) -> None:
    if not enabled():
        return
    try:
        _store().touch_ui_miss(scope, key, selector)
    except Exception:
        pass


def stats() -> dict:
    try:
        return _store().ui_cache_stats()
    except Exception:
        return {"entries": 0, "hits": 0, "misses": 0, "hit_rate": 0.0}


def prune() -> int:
    try:
        return _store().prune_ui_cache(cfg.ELEMENT_CACHE_TTL_DAYS)
    except Exception:
        return 0


def main() -> int:
    s = stats()
    print(f"元素缓存：{s['entries']} 条 | 命中 {s['hits']} 次 | 未命中 {s['misses']} 次"
          f" | 命中率 {s['hit_rate'] * 100:.1f}%")
    removed = prune()
    if removed:
        print(f"已清理过期 {removed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
