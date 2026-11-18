"""一车一件查询端口（现有逻辑的适配器，行为等价）"""
from __future__ import annotations

from typing import Sequence

from bot.pricing.providers.base import PriceProvider


class YicheyijianProvider(PriceProvider):
    name = "yicheyijian"
    label = "一车一件"
    enabled = True

    async def query_vin(self, page, context: dict) -> bool:
        from bot.page_objects import step_market_vin_search
        await step_market_vin_search(page, context)
        return True

    async def query_parts(self, page, context: dict, parts: Sequence) -> None:
        from bot.page_objects import step_market_query_parts
        await step_market_query_parts(page, context, parts=parts)
