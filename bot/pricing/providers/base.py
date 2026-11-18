"""查询端口抽象

新增端口（精友等）只需继承 PriceProvider 并实现 query_vin / query_parts，
再在 bot/config.PRICE_PROVIDER_ORDER 里启用，无需改动 claim_flow。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class PriceProvider(ABC):
    name: str = ""
    label: str = ""
    enabled: bool = True

    async def query_vin(self, page, context: dict) -> bool:
        """建立车辆会话（不需要 VIN 会话的端口可直接返回 True）"""
        return True

    @abstractmethod
    async def query_parts(self, page, context: dict, parts: Sequence) -> None:
        """逐个查询零件价格，把结果写回 part.market_price"""
        raise NotImplementedError
