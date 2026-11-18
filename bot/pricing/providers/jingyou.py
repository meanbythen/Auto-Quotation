"""精友查询端口（占位，未启用）

接入步骤：
  1. 拿到精友账号后，在此实现 query_vin / query_parts
     - 若与一车一件共用同一个浏览器，直接用传入的 page
     - 若是独立站点，可自行打开页面并复用登录 profile
  2. 在 bot/config.py 的 PRICE_PROVIDER_ORDER 中加入 "jingyou"
  3. 不需要改 claim_flow / page_objects 的比价逻辑
"""
from __future__ import annotations

from typing import Sequence

from bot.pricing.providers.base import PriceProvider


class JingyouProvider(PriceProvider):
    name = "jingyou"
    label = "精友"
    enabled = False   # ← 未接入前保持关闭

    async def query_vin(self, page, context: dict) -> bool:
        raise RuntimeError("精友端口尚未接入")

    async def query_parts(self, page, context: dict, parts: Sequence) -> None:
        raise RuntimeError("精友端口尚未接入")
