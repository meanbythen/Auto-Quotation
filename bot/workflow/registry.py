"""
step 函数注册中心

每个 bot/page_objects.py 里的 step_* 函数用 @register_step 装饰器
就能被 workflow executor 按名字调用，也能被 schema.from_registered_steps() 反向生成骨架。

用法:
  @register_step(desc="首页点击报案号", inputs=["claim_no"], outputs=["claim_url"])
  async def step_home_click_claim(page, context, **kwargs):
      ...
      return {"claim_url": url}
"""
from __future__ import annotations
import asyncio
import inspect
from typing import Any, Callable, Dict, Optional

# step_ 注册表: step_name -> {fn, desc, inputs, outputs}
STEP_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_step(
    desc: str = "",
    inputs: Optional[list] = None,
    outputs: Optional[list] = None,
    aliases: Optional[list] = None,
):
    """装饰器：把 async def step_xxx(page, context, **kwargs) 注册到 STEP_REGISTRY"""
    def deco(fn: Callable):
        name = fn.__name__
        if name.startswith("step_"):
            name = name[5:]  # step_home_click_claim → home_click_claim
        STEP_REGISTRY[name] = {
            "fn": fn,
            "desc": desc or (fn.__doc__ or "").strip().split("\n")[0] or name,
            "inputs": inputs or [],
            "outputs": outputs or [],
            "is_async": asyncio.iscoroutinefunction(fn),
        }
        # 别名也注册（如 step_home → home）
        if aliases:
            for a in aliases:
                STEP_REGISTRY[a] = STEP_REGISTRY[name]
        return fn
    return deco


async def run_step(name: str, page=None, context: Optional[Dict[str, Any]] = None, **kwargs) -> Any:
    """
    执行一个已注册的 step

    context: 跨节点共享的变量字典（如 parts, vehicle, claim_no）
    """
    if name not in STEP_REGISTRY:
        raise KeyError(f"未注册的 step: {name}\n已注册: {list(STEP_REGISTRY.keys())}")
    info = STEP_REGISTRY[name]
    fn = info["fn"]
    if info["is_async"]:
        result = await fn(page, context or {}, **kwargs)
    else:
        result = fn(page, context or {}, **kwargs)
    return result
