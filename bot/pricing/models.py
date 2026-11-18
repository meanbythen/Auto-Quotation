"""价格端口数据结构"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PriceQuote:
    """一条零件价格记录"""
    part_code: str
    price: Optional[float]
    source: str = ""
    part_name: str = ""
    fetched_at: str = ""
    expires_at: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def valid_price(self) -> bool:
        return self.price is not None and self.price > 0


@dataclass
class CacheStats:
    vehicles: int = 0
    prices: int = 0
    fresh_prices: int = 0
    hit_count: int = 0
    remote_count: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hit_count + self.remote_count
        return (self.hit_count / total) if total else 0.0
