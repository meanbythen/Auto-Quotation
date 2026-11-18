"""
技能库存储层 —— data/skills/*.json

一个 Skill 的 JSON 结构：
{
  "id":              "fix-clm-btn-001",
  "name":            "报案号带省内气泡 → claim-no 被叠层挡住",
  "created":         "2026-11-11T15:00:00",
  "ver":             1,

  "trigger": {                         // 触发条件（任一命中即匹配，必须同时命中 step+url）
    "step_any":       ["STEP 1"],      // 在哪个 step 失败（任一）
    "error_pattern":  ["未检测到报价页", "not clickable"],  // 错误信息正则（任一）
    "url_pattern":    ["claimOperate", "claimcar/32160/quote"],  // URL 必须包含（任一）
    "sample_text":    ["省内", "省间", "超60天", "集"],  // 页面可见文本（任一，可选）
  },

  "context": {
    "claim_no":       "RDAA2026152500N0018420",   // 第一次保存时的上下文（可选，纯记录）
    "failed_step":    "STEP 1",
    "failed_error":   "首页未找到报案号链接",
    "failed_attempts": 5,
  },

  "actions": [                          // 人工手动做了什么（重放引擎用）
    {"action": "click", "selector": "[data-bot-claim-pick='1']", "text": "", "ts": "..."},
    {"action": "wait",  "ms": 3000, "note": "等新标签打开"},
    {"action": "click", "selector": "button:has-text('编辑')", "text": "编辑", "ts": "..."}
  ],

  "verification": {                     // 重放成功判据（必须全部命中）
    "url_pattern": ["quote/index"],
    "text_any":    ["报价处理"],
  },

  "success_count": 3,
  "failure_count": 0,
}

置信度计算：confidence = success_count / (success_count + failure_count + 1)
   —— 初始 0.0，第一次成功 = 0.5，第二次成功 = 0.67，连续失败会快速趋近 0
   —— 自动匹配只选 confidence >= 0.3 的技能
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bot.logging_system import app_data_dir


def skill_store_dir() -> Path:
    d = app_data_dir() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Skill:
    id: str
    name: str
    created: str
    ver: int = 1
    trigger: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    success_count: int = 0
    failure_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            created=d.get("created", datetime.now().isoformat(timespec="seconds")),
            ver=int(d.get("ver", 1)),
            trigger=d.get("trigger", {}),
            context=d.get("context", {}),
            actions=d.get("actions", []),
            verification=d.get("verification", {}),
            success_count=int(d.get("success_count", 0)),
            failure_count=int(d.get("failure_count", 0)),
        )


def _safe(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s or "").strip("_")[:60] or "skill"


def _new_id(name: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{_safe(name)}"


def save_skill(skill: Skill) -> Path:
    """保存到 data/skills/<id>.json"""
    path = skill_store_dir() / f"{skill.id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(skill.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load_skill(id_or_path: str) -> Optional[Skill]:
    """按 id 或文件路径加载"""
    p = Path(id_or_path)
    if not p.is_absolute():
        p = skill_store_dir() / id_or_path
    if not p.exists() and not p.suffix:
        p = p.with_suffix(".json")   # 只给了 id（record_outcome 就是这么调的）
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return Skill.from_dict(json.load(f))


def load_all() -> list[Skill]:
    out = []
    for p in sorted(skill_store_dir().glob("*.json")):
        try:
            out.append(load_skill(str(p)))
        except Exception:
            continue
    return out


def delete_skill(skill_id: str) -> bool:
    p = skill_store_dir() / f"{skill_id}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def confidence(skill: Skill) -> float:
    tot = skill.success_count + skill.failure_count
    if tot == 0:
        return 0.0
    return skill.success_count / (tot + 1)  # 分母+1 做平滑，初始 0→0.5 跳得别太猛


# ============================================================
# 场景匹配
# ============================================================
def match_skill(step: str, error_text: str, url: str,
                page_text: str = "", min_confidence: float = 0.3) -> Optional[Skill]:
    """按 step + 错误正则 + URL 正则 匹配一个合适的技能

    返回 confidence 最高的那个；置信度不够或没命中返回 None。
    匹配规则（必须同时满足 step + url）：
      · trigger.step_any 任一与 step 相等（不区分大小写）
      · trigger.url_pattern 任一 regex 能匹配 url
    error_pattern / sample_text 是加分项（至少命中一个再用置信度排序）
    """
    step_upper = (step or "").upper()
    url_lower = (url or "").lower()
    error_lower = (error_text or "").lower()
    page_lower = (page_text or "").lower()

    candidates = []
    for sk in load_all():
        if confidence(sk) < min_confidence:
            continue
        trig = sk.trigger or {}

        # step 必须命中
        step_any = [str(s).upper() for s in trig.get("step_any", []) if s]
        step_hit = (not step_any) or step_upper in step_any
        if not step_hit:
            continue

        # url 必须命中
        url_pats = [str(p).lower() for p in trig.get("url_pattern", []) if p]
        url_hit = True
        if url_pats:
            url_hit = any(re.search(p, url_lower) for p in url_pats)
        if not url_hit:
            continue

        # error_pattern / sample_text 命中 → 加分
        bonus = 0
        err_pats = [str(p).lower() for p in trig.get("error_pattern", []) if p]
        if err_pats and any(re.search(p, error_lower) for p in err_pats):
            bonus += 0.2
        samp_pats = [str(p).lower() for p in trig.get("sample_text", []) if p]
        if samp_pats and any(p in page_lower for p in samp_pats):
            bonus += 0.15

        candidates.append((confidence(sk) + bonus, sk))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def record_outcome(skill_id: str, success: bool) -> Optional[Skill]:
    """重放了一个技能后，成功=True/False，更新计数并保存"""
    sk = load_skill(skill_id)
    if sk is None:
        return None
    if success:
        sk.success_count += 1
    else:
        sk.failure_count += 1
    save_skill(sk)
    return sk


# ============================================================
# 从 RunRecord / actions.jsonl 创建一个 Skill
# ============================================================
def build_skill_from_actions(
    actions: list[dict],
    *,
    name: str,
    trigger_step: str = "",
    trigger_url_patterns: list[str] | None = None,
    trigger_error_patterns: list[str] | None = None,
    trigger_sample_texts: list[str] | None = None,
    verification_url_patterns: list[str] | None = None,
    verification_text_any: list[str] | None = None,
    context: dict | None = None,
) -> Skill:
    """把录下来的 actions.jsonl 转换成一个 Skill 并保存。"""
    # actions 里可能有系统事件（response/navigate/dialog），重放时要过滤掉
    SKIP_ACTIONS = {"response", "navigate", "open", "close", "dialog", "submit"}
    clean_actions = [a for a in actions if a.get("action") not in SKIP_ACTIONS]

    # 相邻 click/input 的间隔 → 转成 wait 动作
    final_actions: list[dict] = []
    prev_ts: Optional[float] = None
    for a in clean_actions:
        ts_str = a.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = None
        if prev_ts is not None and ts is not None:
            delta_ms = int((ts - prev_ts) * 1000)
            if 300 <= delta_ms <= 20000:  # 300ms ~ 20s 的 gap 记成 wait
                final_actions.append({"action": "wait", "ms": delta_ms})
        prev_ts = ts
        final_actions.append({k: a.get(k, "") for k in ("action", "selector", "text", "value", "tag") if a.get(k, "")})

    skill = Skill(
        id=_new_id(name),
        name=name,
        created=datetime.now().isoformat(timespec="seconds"),
        trigger={
            "step_any": [trigger_step] if trigger_step else [],
            "url_pattern": trigger_url_patterns or [],
            "error_pattern": trigger_error_patterns or [],
            "sample_text": trigger_sample_texts or [],
        },
        context=context or {},
        actions=final_actions,
        verification={
            "url_pattern": verification_url_patterns or [],
            "text_any": verification_text_any or [],
        },
    )
    save_skill(skill)
    return skill


# ============================================================
# 统计
# ============================================================
def skill_stats() -> dict:
    skills = load_all()
    return {
        "count": len(skills),
        "with_confidence": sum(1 for s in skills if confidence(s) >= 0.3),
        "total_success": sum(s.success_count for s in skills),
        "total_failure": sum(s.failure_count for s in skills),
    }
