"""查询端口注册表：按配置顺序返回启用的端口"""
from __future__ import annotations

from typing import List

from bot.pricing.providers.base import PriceProvider
from bot.pricing.providers.jingyou import JingyouProvider
from bot.pricing.providers.yicheyijian import YicheyijianProvider

_REGISTRY = {
    "yicheyijian": YicheyijianProvider,
    "jingyou": JingyouProvider,
}


def get_providers() -> List[PriceProvider]:
    """按 PRICE_PROVIDER_ORDER 返回启用的端口实例（第一个为主端口）"""
    from bot.config import PRICE_PROVIDER_ORDER

    providers: List[PriceProvider] = []
    for name in PRICE_PROVIDER_ORDER:
        cls = _REGISTRY.get(name)
        if cls is None:
            continue
        inst = cls()
        if getattr(inst, "enabled", False):
            providers.append(inst)
    if not providers:
        providers.append(YicheyijianProvider())
    return providers
