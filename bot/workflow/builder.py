"""
builder —— 把已注册 step 串成完整的报价主流程 workflow

顺序（2026-11-17 更新：含分中心报价分支 + 多类弹窗自动消化 + S 价格值比价）:

  home_click_claim → quote_open_loss_edit
  → loss_sync（打开定损平台第一步：有换件→全选+同步 / 无换件→直接同步）
  → loss_extract_data（★ 现在同时提取 msRetailPrice = S 价格值、price_scheme = 价格方案）
  → [条件分支 A: 有换件吗？]
      NO → loss_submit → quote_finalize
      YES → market_vin_search → market_query_parts → [条件分支 B: any(p.within_market==False)]
          YES → loss_fix_over_market（★ 现在用 S 价格值 / 定损价 统一改价公式）
          NO → 直接到 loss_submit
        → loss_submit → quote_finalize

  quote_finalize 内部（已注册为单一步骤，内部消化多级分支，编辑器只显式展开最关键的 4 层）:
    ├─ [前置判断] 换件金额 > 4 万？
    │   YES → 直接进 quote_submit_superior（不等后续权限拦截，省一次来回）
    │   NO  → 选"报价完成" → 点"提交"
    │        ├─ 弹窗自动处理（用户点击前就已消化，不进入流程图）:
    │        │  ① 报价总额超保险金额 70% + 是否重新报价 → 点"取消"
    │        │  ② 标的车报价合计大于保险金额 + 请确定是否提交下一节点 → 点"确定"
    │        │  ③ 已本地化换件超 5 万 → 点"确认"（quote_submit_superior 里处理）
    │        └─ 报价任务提交弹窗 → 点"提交任务"
    │             └─ 成功？→ 结束
    │                 NO（权限拦截）→ quote_submit_superior → task_transfer_back → quote_reopen_finalize
    └─ quote_submit_superior 内部（显式展开，因为路径/组别参数不同）:
         ├─ 选"提交上级" → 点"提交"
         ├─ 保险金额提示框 → 自动处理（同上）
         ├─ [条件分支 C: 弹『已本地化换件超 5 万』？]
         │   YES → 改走"分中心报价"：submit_path=分中心报价 / group_option=总公司分中心报价组
         │   NO  → 保持原参数：submit_path=九级报价 / group_option=市公司报价组
         └─ 报价任务提交弹窗 → 填路径+组别 → 点"提交任务"

  * 实际运行走 bot.main._finalize_with_fallback（异常驱动），
    workflow 仅用于流程编辑器可视化 + 条件分支表达；
    quote_finalize 和 quote_submit_superior 注册为单一 step，
    内部多级弹窗处理与分支逻辑已内嵌在函数中。
"""
from __future__ import annotations
from .schema import Workflow, WorkflowNode
from bot.config import (
    TRANSFER_SUBMIT_PATH, TRANSFER_GROUP_CODE,
    TRANSFER_GROUP_OPTION, TRANSFER_GROUP_OPTION_KEY, TRANSFER_GROUP_OPTION_EXCLUDE,
    TRANSFER_SUBMIT_PATH_BRANCH, TRANSFER_GROUP_CODE_BRANCH,
    TRANSFER_GROUP_OPTION_BRANCH, TRANSFER_GROUP_OPTION_KEY_BRANCH,
    HIGH_AMOUNT_THRESHOLD,
)


def build_default_workflow(name: str = "main_flow") -> Workflow:
    """构建默认主流程（2026-11-17 更新，含分中心报价分支 + S 价格值比价）"""
    wf = Workflow(
        name=name,
        desc=(
            f"理赔报价机器人默认主流程（同步定损前置 + 无换件分支 + 自动修复超市场价）"
            f" | 换件金额 ≥ {HIGH_AMOUNT_THRESHOLD:,.0f} 前置提上级"
            f" | 已本地化换件超 5 万 → 分中心报价分支"
        ),
    )

    n1 = wf.add(WorkflowNode(id="n1", type="call_step",
                  action="home_click_claim",
                  desc="首页点击报案号",
                  goto="n2"))

    n2 = wf.add(WorkflowNode(id="n2", type="call_step",
                  action="quote_open_loss_edit",
                  desc="报价处理点编辑 → 定损平台 popup",
                  goto="n3"))

    n3 = wf.add(WorkflowNode(id="n3", type="call_step",
                  action="loss_sync",
                  desc="同步定损（有换件先全选 / 无换件直接点）",
                  goto="n4"))

    n4 = wf.add(WorkflowNode(id="n4", type="call_step",
                  action="loss_extract_data",
                  desc="定损平台提取 VIN + 换件 + ★S价格值(msRetailPrice) + 直供价",
                  goto="n_ds_branch"))

    # 条件分支 A0：直供价非 0？（进平台会弹「是否拟采购」→ 点否）
    n_ds_branch = wf.add(WorkflowNode(id="n_ds_branch", type="condition",
                         when="context.get('skip_direct_supply', False)",
                         desc="📦 直供价有非 0 值？",
                         then="n_ds_fix", else_="n_parts_branch"))

    n_ds_fix = wf.add(WorkflowNode(id="n_ds_fix", type="call_step",
                      action="step_loss_cancel_direct_supply",
                      desc=(
                          "取消直供价（把它变成正常案件）"
                          "\n  ① 全选零件"
                          "\n  ② 点表头『价格类型』→ 选 R → 确定"
                          "\n  ③ 再点『价格类型』→ 切回原类型（原 A→A / 原 S→S，混合按行分组）→ 确定"
                          "\n  ④ 点零件列表『保存』→ 回读校验直供价已清空"
                          "\n  ⑤ 提交行输入框填『接受系统点选』"
                          "\n  ⑥ 重新提取零件（本地价/定损价已刷新）"
                      ),
                      goto="n_parts_branch"))

    # 条件分支 A：有换件吗？（无换件 → 跳过价格比对，直接提交）
    n_parts_branch = wf.add(WorkflowNode(id="n_parts_branch", type="condition",
                            when="len(context.get('parts', [])) > 0",
                            desc="📋 有换件零件？",
                            then="n5", else_="n_submit"))

    n5 = wf.add(WorkflowNode(id="n5", type="call_step",
                  action="market_vin_search",
                  desc="一车一件 VIN 查询 → 跳详情页",
                  goto="n6"))

    n6 = wf.add(WorkflowNode(id="n6", type="call_step",
                  action="market_query_parts",
                  desc="逐个零件查市场价（按 S 价格值比价）",
                  goto="n_branch"))

    # 条件分支 B：any 零件超市场价（按 S 价格值算，不是 local_price）？
    n_branch = wf.add(WorkflowNode(id="n_branch", type="condition",
                        when="any(p.within_market is False for p in context.get('parts', []))",
                        desc="📋 有超市场价零件（S价 > 一车一件价+容忍度）？",
                        then="n_fix", else_="n_submit"))

    n_fix = wf.add(WorkflowNode(id="n_fix", type="call_step",
                      action="loss_fix_over_market",
                      desc="自动修复超市场价零件 + 暂存（★ 公式: 报价=rounddown(一车一件×定损价/S价)）",
                      goto="n_submit"))

    n_submit = wf.add(WorkflowNode(id="n_submit", type="call_step",
                        action="loss_submit",
                        desc="定损平台 → 提交定损单",
                        goto="n_amount_branch"))

    # ============================================================
    # ★ 新增：换件金额 ≥ 4 万前置分支（比权限拦截更早判断，省一次来回）
    # 条件分支 C：换件汇总金额 ≥ 40000？
    #   YES → 直接走提交上级（九级报价 / 市公司报价组）
    #   NO  → 走正常 quote_finalize（报价完成 → 可能被权限拦截再补救）
    # ============================================================
    n_amount_branch = wf.add(WorkflowNode(id="n_amount_branch", type="condition",
                            when=f"context.get('quote_high_amount', 0) >= {HIGH_AMOUNT_THRESHOLD}",
                            desc=f"💰 换件汇总 ≥ ¥{HIGH_AMOUNT_THRESHOLD:,.0f}？（直接提上级）",
                            then="n_superior_v1", else_="n_final"))

    # —— 路径 1（小金额 / 一级报价能过）：正常报价完成 ——
    n_final = wf.add(WorkflowNode(id="n_final", type="call_step",
                      action="quote_finalize",
                      desc=(
                          "报价完成 → 点提交 → 弹窗自动处理 → 报价任务提交弹窗"
                          "\n  弹窗自动消化（内部 handler，不进流程图）："
                          "\n    ①报价总额超保险金额70%+是否重新报价 → 点『取消』"
                          "\n    ②标的车报价合计>保险金额+提交下一节点 → 点『确定』"
                          "\n    ③已本地化换件超5万 → 点『确认』（交给 quote_submit_superior 分支）"
                          "\n  ★提交路径 [条件]：改过报价 / 取消过直供价 /"
                          "\n    报价汇总换件金额 ≠ 定损汇总换件金额 → 选『车辆定损』（回定损环节同步金额）"
                          "\n    否则：无核损汇总 → 『一级车辆核损』；有核损汇总 → 保持系统默认"
                      ),
                      goto="n_perm_check"))

    n_perm_check = wf.add(WorkflowNode(id="n_perm_check", type="condition",
                            when="context.get('quote_finalize_permission_error', False)",
                            desc="🛡 报价完成是否被权限拦截？",
                            then="n_superior_v1", else_=None))

    # ============================================================
    # ★ 新增：quote_submit_superior 内部显式展开（因为有两种参数组合：
    #   九级报价/市公司报价组  vs  分中心报价/总公司分中心报价组）
    # ============================================================

    # —— 路径 A：九级报价 / 市公司报价组（换件 ≥4 万前置 或 权限拦截补救） ——
    n_superior_v1 = wf.add(WorkflowNode(id="n_superior_v1", type="call_step",
                            action="quote_submit_superior",
                            desc=(
                                "提交上级 → 九级报价 / 市公司报价组"
                                "\n  内部流程：选『提交上级』→ 点提交"
                                "\n    → 保险金额弹窗自动处理（同上）"
                                "\n    → [分支 D] 弹『已本地化换件超 5 万』？"
                                "\n      YES → 改走 路径 B（下方）"
                                "\n      NO  → 保持本参数"
                                "\n    → 报价任务提交弹窗 → 填路径/组别 → 提交任务"
                            ),
                            kwargs={
                                "submit_path": TRANSFER_SUBMIT_PATH,
                                "group_option": TRANSFER_GROUP_OPTION,
                                "group_code": TRANSFER_GROUP_CODE,
                                "group_key": TRANSFER_GROUP_OPTION_KEY,
                                "group_exclude": TRANSFER_GROUP_OPTION_EXCLUDE,
                            },
                            goto="n_transfer"))

    # —— 路径 B：分中心报价 / 总公司分中心报价组（已本地化换件超 5 万触发） ——
    #   由 n_superior_v1 内部 _handle_localized_part_50k_dialog 检测到后，
    #   自动重走 step_quote_submit_superior 的后半段（参数换成这套）。
    #   这里单独列出来方便流程编辑器看到完整逻辑。
    n_superior_v2 = wf.add(WorkflowNode(id="n_superior_v2", type="call_step",
                            action="quote_submit_superior",
                            desc=(
                                "提交上级 ★分中心报价分支"
                                "\n  触发条件：已本地化换件的单项最大金额超 5 万"
                                "\n  内部流程（由 v1 检测到提示框后自动改走本参数）："
                                "\n    重新选『提交上级』→ 点提交 → 弹窗清理"
                                "\n    → 报价任务提交弹窗（提交路径已默认选中『分中心报价』）"
                                "\n    → 组别代码填 1 → 下拉选 总公司分中心报价组"
                                "\n    → 点『提交任务』"
                            ),
                            kwargs={
                                "submit_path": TRANSFER_SUBMIT_PATH_BRANCH,
                                "group_option": TRANSFER_GROUP_OPTION_BRANCH,
                                "group_code": TRANSFER_GROUP_CODE_BRANCH,
                                "group_key": TRANSFER_GROUP_OPTION_KEY_BRANCH,
                                "group_exclude": "",
                            },
                            goto="n_transfer"))

    n_transfer = wf.add(WorkflowNode(id="n_transfer", type="call_step",
                        action="task_transfer_back",
                        desc="任务中心 → 平级移交 → 转回本人名下",
                        goto="n_reopen"))

    n_reopen = wf.add(WorkflowNode(id="n_reopen", type="call_step",
                      action="quote_reopen_finalize",
                      desc="重开报价处理页 → 报价完成 → 提交",
                      goto=None))

    # ★ 显式画"本地化 5 万"分支线（编辑器里看到完整图）
    # n_superior_v1 内部检测到提示框 → 自动用 v2 参数再走一遍后半段
    # 真实执行时一个 quote_submit_superior step 内完成，流程编辑器里只是两条线的可视化
    wf.entry = n1.id
    return wf


def ensure_default_saved(force: bool = False) -> str:
    """确保 default workflow.json 存在，返回 path"""
    bot_dir = __import__("pathlib").Path(__file__).resolve().parent.parent
    data_dir = bot_dir.parent / "data" / "workflows"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "main_flow.json"
    if force or not target.exists():
        wf = build_default_workflow()
        wf.save(target)
    return str(target)
