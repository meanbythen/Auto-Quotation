"""本地配件价格缓存库

用法：
    from bot.pricing import get_price_service
    svc = get_price_service()
    remote_parts, cached_parts = svc.resolve(vin, parts, tolerance)
    ... 远程查询 ...
    svc.save(vin, remote_parts, source="yicheyijian")
"""
from __future__ import annotations

from bot.pricing.models import CacheStats, PriceQuote
from bot.pricing.service import PriceService
from bot.pricing.store import PriceStore

__all__ = ["PriceService", "PriceStore", "PriceQuote", "CacheStats",
           "get_price_service"]

_SERVICE = None


def get_price_service() -> PriceService:
    """全局单例（首次调用时建库）"""
    global _SERVICE
    if _SERVICE is None:
        from bot.config import (PRICE_CACHE_DB_NAME, PRICE_CACHE_ENABLED,
                                PRICE_CACHE_RETENTION_DAYS,
                                PRICE_CACHE_VALID_DAYS)
        from bot.logging_system import app_data_dir

        store = PriceStore(app_data_dir() / PRICE_CACHE_DB_NAME,
                           retention_days=PRICE_CACHE_RETENTION_DAYS)
        _SERVICE = PriceService(store,
                                valid_days=PRICE_CACHE_VALID_DAYS,
                                enabled=PRICE_CACHE_ENABLED)
        try:
            _SERVICE.purge()
        except Exception:
            pass
    return _SERVICE
