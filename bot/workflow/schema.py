"""
工作流核心 —— DAG 数据模型 + 序列化

节点类型:
  - action       单个原子动作（click/fill/select/...），走 registry 调用
  - call_step    调用已注册的 step_* 函数（带 selector 自动适配）
  - condition    条件分支 → when → then / else
  - loop         循环遍历列表变量 → body 节点序列
  - dialog       弹窗（alert/confirm）处理
  - navigate     页面跳转
  - wait         等待（固定时间 / 元素出现 / 文本变化）

节点通用字段:
  id, type, desc, goto (next), when (条件表达式), then, else, loop_over, loop_body
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Union
from pathlib import Path


NODE_TYPES = {"action", "call_step", "condition", "loop", "dialog", "navigate", "wait"}


@dataclass
class WorkflowNode:
    id: str
    type: str
    desc: str = ""
    # action / call_step
    action: str = ""                       # click / fill / select / call_step 名
    selector: str = ""                     # CSS selector / data-testid
    value: str = ""                        # fill 的值 / select 的选项
    # call_step 专用：给 step_* 函数传 kwargs（如 tolerance, parts 等）
    kwargs: Dict[str, Any] = field(default_factory=dict)

    # condition
    when: str = ""                         # Python 表达式，可访问 context 变量
    then: Optional[str] = None             # → 节点 id
    else_: Optional[str] = None            # → 节点 id

    # loop
    loop_over: str = ""                    # context 中列表变量名
    loop_var: str = "item"                 # 每次迭代的变量名
    loop_body: List[str] = field(default_factory=list)

    # dialog
    dialog_action: str = "accept"          # accept / dismiss / reply

    # wait
    wait_ms: int = 500                     # wait 节点固定等待毫秒
    wait_selector: str = ""                # 等元素出现

    # navigate
    url: str = ""

    # 通用跳转
    goto: Optional[str] = None

    # 元信息（编辑器用）
    enabled: bool = True
    retry: int = 0                         # 出错后重试次数

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Python 关键字冲突：else_ → else
        if "else_" in d:
            d["else"] = d.pop("else_")
        return {k: v for k, v in d.items() if v != "" and v is not None and v != []}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowNode":
        d = dict(d)
        if "else" in d:
            d["else_"] = d.pop("else")
        # 补全默认值（保证旧 JSON 能加载）
        for k in ["kwargs", "loop_body"]:
            if k not in d or d[k] is None:
                d[k] = [] if k == "loop_body" else {}
        return cls(**d)


@dataclass
class Workflow:
    name: str
    version: str = "2.0"
    desc: str = ""
    nodes: List[WorkflowNode] = field(default_factory=list)
    entry: str = ""                         # 入口节点 id（空则 nodes[0]）
    meta: Dict[str, Any] = field(default_factory=dict)

    # ---------- API ----------
    def add(self, node: WorkflowNode) -> WorkflowNode:
        self.nodes.append(node)
        return node

    def remove(self, node_id: str) -> bool:
        before = len(self.nodes)
        self.nodes = [n for n in self.nodes if n.id != node_id]
        return len(self.nodes) < before

    def find(self, node_id: str) -> Optional[WorkflowNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def index_of(self, node_id: str) -> int:
        for i, n in enumerate(self.nodes):
            if n.id == node_id:
                return i
        return -1

    def insert_after(self, node_id: str, new_node: WorkflowNode):
        idx = self.index_of(node_id)
        if idx >= 0:
            self.nodes.insert(idx + 1, new_node)
        else:
            self.nodes.append(new_node)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "desc": self.desc,
            "entry": self.entry or (self.nodes[0].id if self.nodes else ""),
            "meta": self.meta,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Workflow":
        nodes = [WorkflowNode.from_dict(n) for n in d.get("nodes", [])]
        return cls(
            name=d.get("name", "untitled"),
            version=d.get("version", "2.0"),
            desc=d.get("desc", ""),
            entry=d.get("entry", "") or (nodes[0].id if nodes else ""),
            meta=d.get("meta", {}),
            nodes=nodes,
        )

    # ---------- 持久化 ----------
    @staticmethod
    def default_dir() -> Path:
        import sys
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent.parent.parent
        d = base / "data" / "workflows"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, path: Optional[Path | str] = None) -> Path:
        if path is None:
            path = self.default_dir() / f"{re.sub(r'[^\w\-_]', '_', self.name)}.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path_or_name: str) -> "Workflow":
        p = Path(path_or_name)
        if not p.exists() and not p.suffix:
            p = cls.default_dir() / f"{path_or_name}.json"
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    @staticmethod
    def list_all() -> List[str]:
        d = Workflow.default_dir()
        if not d.exists():
            return []
        return sorted([f.stem for f in d.glob("*.json")])

    # ---------- 反向从现有 step 生成 ----------
    @classmethod
    def from_registered_steps(cls, name: str = "main_flow") -> "Workflow":
        """根据 registry 里已注册的 step_* 函数自动生成骨架 workflow"""
        from bot.workflow.registry import STEP_REGISTRY
        wf = cls(name=name, desc="从 registry 自动生成")
        ids = sorted(STEP_REGISTRY.keys())

        prev_id: Optional[str] = None
        for sid in ids:
            info = STEP_REGISTRY[sid]
            nid = f"n_{sid}"
            node = WorkflowNode(
                id=nid,
                type="call_step",
                action=sid,
                desc=info.get("desc", sid),
                goto=None,  # 最后一个后面设
            )
            if prev_id:
                prev = wf.find(prev_id)
                if prev and prev.type not in ("condition", "loop"):
                    prev.goto = nid
            wf.add(node)
            prev_id = nid

        if wf.nodes:
            wf.entry = wf.nodes[0].id
        return wf
