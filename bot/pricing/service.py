"""价格缓存服务：本地优先、过期回源、端口编排"""
from __future__ import annotations

from typing import List, Sequence, Tuple

from bot.pricing.models import CacheStats
from bot.pricing.store import PriceStore


class PriceService:
    def __init__(self, store: PriceStore, valid_days: int, enabled: bool = True):
        self.store = store
        self.valid_days = max(1, int(valid_days or 30))
        self.enabled = bool(enabled)

    # ---------- 车辆 ----------
    def touch_vin(self, vin: str, brand: str = "", model: str = "") -> None:
        if self.enabled:
            self.store.touch_vehicle(vin, brand, model)

    # ---------- 查价 ----------
    def resolve(self, vin: str, parts: Sequence,
                tolerance: float = 0.0,
                sources: Sequence[str] = ()) -> Tuple[List, List]:
        """把命中新鲜缓存的零件直接写回 market_price

        返回 (需要远程查询的零件, 命中缓存的零件)
        """
        remote: List = []
        cached: List = []
        if not self.enabled:
            return list(parts), []

        for p in parts:
            code = getattr(p, "code", "") or ""
            quote = self.store.get_fresh(vin, code, sources=sources)
            if quote and quote.valid_price:
                p.market_price = float(quote.price)
                try:
                    p.check(tolerance)
                except Exception:
                    pass
                cached.append(p)
                self.store.log(vin, code, "cache_hit", source=quote.source,
                               price=quote.price,
                               detail=f"fetched_at={quote.fetched_at}")
            else:
                if quote is not None:
                    self.store.log(vin, code, "cache_expired",
                                   source=quote.source,
                                   detail=f"expires_at={quote.expires_at}")
                remote.append(p)
        return remote, cached

    # ---------- 落库 ----------
    def save(self, vin: str, parts: Sequence, source: str = "yicheyijian") -> int:
        """把本次远程查到的有效价格写库，返回写入条数"""
        if not self.enabled:
            return 0
        n = 0
        for p in parts:
            price = getattr(p, "market_price", None)
            if price is None or price <= 0:
                continue
            ok = self.store.put(
                vin, getattr(p, "code", "") or "", float(price),
                source=source, valid_days=self.valid_days,
                part_name=getattr(p, "name", "") or "",
                meta={"local_price": getattr(p, "local_price", None),
                      "price_scheme": getattr(p, "price_scheme", "")})
            if ok:
                n += 1
                self.store.log(vin, getattr(p, "code", ""), "remote_query",
                               source=source, price=price)
        return n

    # ---------- 统计 ----------
    def stats(self) -> CacheStats:
        return self.store.stats()

    def purge(self) -> int:
        return self.store.purge() if self.enabled else 0
