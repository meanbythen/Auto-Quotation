"""
bot.skills —— 「失败 → 人工干预 → 自动学习 → 下次自己处理」闭环

核心思路：
  程序在某个 step 卡住（STEP 1~9 任何一步） → 把当时的「场景特征」（在哪一步、
  URL 长什么样、报了什么错）和「页面快照」存下来；你手动解决一遍问题 → 程序
  记录你做了什么动作（click/fill/wait） → 打包成 Skill（本质是一段带触发条件
  的动作序列 JSON）。下次遇到同样场景自动匹配 → 自动重放动作 → 成功自动加分
  置信度，失败自动减分（衰减机制）。

安全护栏：
  · 自动匹配重放最多 1 次，失败就回退到原有跳过流程，不无限重试
  · 置信度 < 0.3 的技能不会自动套用（只建议人工审核）
  · 重放成功的判据必须验 URL 正则（quote/index / diagnosis-modal 之类）
  · 匹配时必须同时命中 step + url_pattern，不能只靠错误文本（防误匹配）

见 bot/skills/store.py  —— 技能库 JSON 格式 + CRUD + 匹配
    bot/skills/replay.py —— 把人工录的动作序列转成 Playwright 调用
    bot/skills/hooks.py  —— 挂在 claim_flow 里的失败 / 学习钩子
"""
from .store import (
    Skill, skill_store_dir, load_all, save_skill, delete_skill,
    match_skill, record_outcome, confidence,
)
from .replay import replay_skill

__all__ = [
    "Skill", "skill_store_dir", "load_all", "save_skill", "delete_skill",
    "match_skill", "record_outcome", "confidence", "replay_skill",
]
