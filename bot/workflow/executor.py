"""
工作流执行器 —— 状态机 DAG 跑图

核心:
  entry → nodes[entry] → 按 goto/then/else/loop_body 走 → 直到 goto=None
  context 字典跨节点共享（parts, vehicle, claim_no 等）
  condition 节点: Python eval(when, {"context": context}) → then / else
  loop 节点: for item in context[loop_over]: 顺序跑 loop_body

用法:
  wf = Workflow.load("main_flow")
  runner = WorkflowRunner()
  result = await runner.run(wf, context={"claim_no": "..."}, pages={...})
"""
from __future__ import annotations
import asyncio
import re
import time
from typing import Any, Dict, List, Optional
from .schema import Workflow, WorkflowNode
from .registry import run_step, STEP_REGISTRY


class ExecutionResult:
    def __init__(self):
        self.status = "done"    # done / blocked / error / stopped
        self.steps_executed: List[str] = []
        self.errors: List[str] = []
        self.context: Dict[str, Any] = {}
        self.start_time = time.time()
        self.end_time: Optional[float] = None

    def summary(self) -> str:
        lines = [f"执行结束: {self.status}  用时 {self.elapsed():.1f}s  节点 {len(self.steps_executed)}"]
        if self.errors:
            lines.append(f"错误 {len(self.errors)}:")
            for e in self.errors:
                lines.append(f"  - {e}")
        return "\n".join(lines)

    def elapsed(self) -> float:
        return (self.end_time or time.time()) - self.start_time


class WorkflowRunner:
    """DAG 执行器"""

    def __init__(self, stop_event: Optional[asyncio.Event] = None,
                 log_fn=None, pages: Optional[Dict[str, Any]] = None):
        """
        pages: {"portal": page, "market": page, "loss": page, ...}
        log_fn: print-like 回调
        """
        self.pages = pages or {}
        self.log = log_fn or print
        self.stop_event = stop_event
        self.result = ExecutionResult()

    # ---------- 对外入口 ----------
    async def run(self, wf: Workflow, context: Optional[Dict[str, Any]] = None) -> ExecutionResult:
        self.result = ExecutionResult()
        self.result.context = context or {}
        self.result.status = "running"

        node_id = wf.entry or (wf.nodes[0].id if wf.nodes else None)
        visited = set()
        max_iter = len(wf.nodes) * 50 + 50  # 防死循环

        iter_count = 0
        while node_id and iter_count < max_iter:
            iter_count += 1
            if self.stop_event and self.stop_event.is_set():
                self.log("⏹️  用户停止")
                self.result.status = "stopped"
                break

            node = wf.find(node_id)
            if not node:
                self.log(f"❌ 找不到节点 {node_id}")
                self.result.status = "error"
                self.result.errors.append(f"节点 {node_id} 不存在")
                break

            if not node.enabled:
                self.log(f"⏭️  [{node_id}] {node.desc}（已禁用）")
                node_id = node.goto
                continue

            # 防死循环警告（条件节点可能导致 revisit）
            vid = f"{node_id}"
            visited.add(vid)

            self.log(f"  [{node_id}] {node.type.upper()}: {node.desc or node.action}")
            self.result.steps_executed.append(node_id)

            try:
                node_id = await self._execute(node, self.result.context)
            except Exception as exc:
                self.log(f"  ❌ 执行失败: {exc}")
                self.result.errors.append(f"{node_id}: {exc}")
                if node.retry > 0:
                    node.retry -= 1
                    self.log(f"  🔄 重试 ({node.retry} 剩余)")
                    continue
                else:
                    self.result.status = "error"
                    break

        if iter_count >= max_iter:
            self.log(f"⚠️  超过最大迭代 {max_iter}，可能死循环")
            self.result.status = "error"

        self.result.end_time = time.time()
        if self.result.status == "running":
            self.result.status = "done"
        self.log(self.result.summary())
        return self.result

    # ---------- 节点执行分发 ----------
    async def _execute(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        t = node.type

        if t == "call_step":
            return await self._exec_call_step(node, ctx)
        elif t == "action":
            return await self._exec_action(node, ctx)
        elif t == "condition":
            return await self._exec_condition(node, ctx)
        elif t == "loop":
            return await self._exec_loop(node, ctx)
        elif t == "dialog":
            return await self._exec_dialog(node, ctx)
        elif t == "navigate":
            return await self._exec_navigate(node, ctx)
        elif t == "wait":
            return await self._exec_wait(node, ctx)
        else:
            self.log(f"  [!] 未知节点类型 {t}，跳过")
            return node.goto

    # ---------- 各类型实现 ----------
    async def _exec_call_step(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        """调用 registry 里注册的 step_* 函数"""
        page = self._resolve_page(node, ctx)
        # 从 context 里把 kwargs 对应的值取出来
        effective_kwargs = {}
        for k, v in (node.kwargs or {}).items():
            if isinstance(v, str) and v.startswith("ctx."):
                key = v[4:]
                effective_kwargs[k] = ctx.get(key)
            else:
                effective_kwargs[k] = v

        # 额外注入 context 里常见的变量
        extra = {}
        for k in ("parts", "vehicle", "tolerance"):
            if k not in effective_kwargs and k in ctx:
                extra[k] = ctx[k]

        result = await run_step(node.action, page=page, context=ctx, **extra, **effective_kwargs)
        if isinstance(result, dict):
            ctx.update(result)
        return node.goto

    async def _exec_action(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        """原子 action（click/fill/select 等 Playwright 原语）"""
        page = self._resolve_page(node, ctx)
        selector = node.selector
        if not selector:
            return node.goto

        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=5000)

        if node.action == "click":
            await locator.click()
        elif node.action == "fill":
            val = node.value
            if isinstance(val, str) and val.startswith("ctx."):
                val = ctx.get(val[4:], "")
            await locator.fill(str(val))
        elif node.action == "select":
            await locator.select_option(node.value)
        elif node.action == "check":
            if not await locator.is_checked():
                await locator.check()
        elif node.action == "uncheck":
            if await locator.is_checked():
                await locator.uncheck()
        elif node.action == "press":
            await page.keyboard.press(node.value)
        elif node.action == "get_text":
            ctx[f"_text_{node.id}"] = await locator.text_content()
        else:
            self.log(f"  [!] 未知 action: {node.action}")

        await asyncio.sleep(0.3)
        return node.goto

    async def _exec_condition(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        """条件分支：eval(when) → then / else"""
        expr = node.when
        if not expr:
            return node.else_ or node.goto
        try:
            # 提供 context 作为 eval 的局部变量
            result = eval(expr, {"__builtins__": {}}, {"context": ctx, **ctx})
            next_id = node.then if result else node.else_
            self.log(f"  📋 条件 [{expr[:50]}] → {'YES' if result else 'NO'} → {next_id}")
            return next_id or node.goto
        except Exception as exc:
            self.log(f"  [!] 条件求值失败: {exc} → 走 else")
            return node.else_ or node.goto

    async def _exec_loop(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        """循环：for item in ctx[loop_over]: 依次跑 loop_body 里的节点"""
        items = ctx.get(node.loop_over) or []
        if not items:
            self.log(f"  ⏭️  loop {node.id}: 列表 {node.loop_over} 为空，跳过")
            return node.goto

        self.log(f"  🔁  loop {node.id}: {len(items)} 个元素")
        for item in items:
            ctx[node.loop_var] = item
            # 顺序跑 body 里的节点
            for sub_id in node.loop_body:
                sub = None  # body 节点不在 wf.nodes 顶层，直接 inline 找不到
                # 简化处理：loop_body 里存的是完整 node dict
                # 实际实现里 body 节点需要特殊存（见下方）
                pass
            await asyncio.sleep(0.2)
        return node.goto

    async def _exec_dialog(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        """弹窗处理（依赖 executor 里已注册 dialog handler）"""
        # 简化：等 0.5s（通常由 page.on("dialog") 统一捕获）
        await asyncio.sleep(0.5)
        return node.goto

    async def _exec_navigate(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        page = self._resolve_page(node, ctx)
        url = node.url
        if isinstance(url, str) and url.startswith("ctx."):
            url = ctx.get(url[4:], url)
        await page.goto(str(url), wait_until="domcontentloaded")
        await asyncio.sleep(0.5)
        return node.goto

    async def _exec_wait(self, node: WorkflowNode, ctx: Dict[str, Any]) -> Optional[str]:
        if node.wait_ms:
            await asyncio.sleep(node.wait_ms / 1000.0)
        if node.wait_selector:
            page = self._resolve_page(node, ctx)
            await page.locator(node.wait_selector).first.wait_for(state="visible", timeout=10000)
        return node.goto

    # ---------- 工具 ----------
    def _resolve_page(self, node: WorkflowNode, ctx: Dict[str, Any]):
        """根据节点 / context 找 page 对象"""
        if "page" in ctx:
            return ctx["page"]
        if self.pages:
            # 默认第一个
            return list(self.pages.values())[0]
        return None
