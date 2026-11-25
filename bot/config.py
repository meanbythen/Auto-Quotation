"""
全局配置 —— 单一事实来源
"""
from pathlib import Path
import os
import sys

# ============ 路径（兼容 PyInstaller 打包后 sys._MEIPASS 解包临时目录） ============
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：mock_web 在 sys._MEIPASS/mock_web
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent

_PROJECT = _base_dir()
PROJECT_ROOT = _PROJECT
MOCK_WEB_DIR = _PROJECT / "mock_web"
BOT_DIR = _PROJECT / "bot"

# Playwright 持久化浏览器 profile 目录（独立于你日常用的 Chrome，不抢锁）
# 打包后放到 exe 同目录下的 data/ 里（sys._MEIPASS 是临时目录不能持久化）
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).resolve().parent
    BOT_PROFILE_DIR = _exe_dir / "data" / "chrome_bot_profile"
else:
    BOT_PROFILE_DIR = BOT_DIR / "chrome_bot_profile"

# ============ 智能意见判断（P0：只采集 + 建议，不改变执行） ============
# off     完全不读意见，行为与旧版一致
# advise  读意见并记录判断依据，但不改变执行（灰度第一阶段）
# confirm 中置信度弹窗确认（P1 启用）
# auto    高置信度自动执行（P2 启用）
INTELLIGENCE_MODE = os.environ.get("BOT_INTELLIGENCE_MODE", "advise").strip().lower()
INTELLIGENCE_AUTO_THRESHOLD = 0.80
INTELLIGENCE_CONFIRM_THRESHOLD = 0.55
# 模型输出的概率尺度与规则不同，auto 模式单独设阈值
INTELLIGENCE_MODEL_AUTO_THRESHOLD = float(os.environ.get("BOT_INTELLIGENCE_MODEL_AUTO", "0.70"))
# auto 模式是否允许「等待资料 / 无需处理」直接跳过本案件
INTELLIGENCE_AUTO_SKIP_HOLD = os.environ.get("BOT_INTELLIGENCE_AUTO_SKIP", "1").strip() == "1"
INTELLIGENCE_COLLECT = True          # 挂机随单采集语料
INTELLIGENCE_SOURCE = "hang"
# 意见列表通常异步渲染，读取采用有界轮询
INTELLIGENCE_READ_TIMEOUT = float(os.environ.get("BOT_INTELLIGENCE_READ_TIMEOUT", "8"))

# 扫描报案号：列表为空时默认只等待异步加载，不点击『查询』触发整页重新查询
SCAN_ALLOW_QUERY_CLICK = os.environ.get("BOT_SCAN_QUERY_CLICK", "0").strip() == "1"

# ============ 本地模拟服务器（理赔门户内网限制，只能 mock） ============
MOCK_SERVER_HOST = "127.0.0.1"
MOCK_SERVER_PORT = 8765
BASE_URL = f"http://{MOCK_SERVER_HOST}:{MOCK_SERVER_PORT}"

MOCK_URLS = {
    "home":          f"{BASE_URL}/index.html",
    "quote_process": f"{BASE_URL}/quote_process.html",
    "loss_assess":   f"{BASE_URL}/loss_assessment.html",
    "market_home":   f"{BASE_URL}/market_home.html",
    "market_detail": f"{BASE_URL}/market_detail.html",
    "task_transfer": f"{BASE_URL}/task_center_transfer.html",
}

# ============ 真实内网：理赔作业门户 ============
PORTAL_REAL_URL = "http://10.164.128.92/claimOper/"
# 真实门户复用已打开的浏览器：填写 CDP 地址（需先以 --remote-debugging-port=9222 启动）
# 例如 "http://127.0.0.1:9222"；留空 = 程序自己新开窗口（独立 profile，登录一次复用）
PORTAL_CDP_URL = ""
# 一车一件也复用同一个已打开浏览器时，填自己的 CDP 地址（留空 = 跟门户用同一个）
MARKET_CDP_URL = ""

# 真实门户独立 profile 目录（与一车一件分开，登录一次永久复用）
if getattr(sys, "frozen", False):
    _exe_dir2 = Path(sys.executable).resolve().parent
    PORTAL_PROFILE_DIR = _exe_dir2 / "data" / "portal_profile"
else:
    PORTAL_PROFILE_DIR = BOT_DIR / "portal_profile"

# ============ 真实内网：任务中心 - 平级移交 ============
# 菜单：理赔作业门户 → 左侧『任务中心』→『平级移交』
# 生产实测 data-url = /claimcar/taskTransfer/TaskTransferQuery；实际页面版本前缀为 32180
TASK_TRANSFER_URL = "http://10.164.128.92/claimcar/32180/taskTransfer/TaskTransferQuery"
# 兜底直连地址（菜单 data-url 原始值 / 旧版本号）
TASK_TRANSFER_URL_ALT = "http://10.164.128.92/claimcar/taskTransfer/TaskTransferQuery"
TASK_TRANSFER_URL_ALTS = (
    "http://10.164.128.92/claimcar/taskTransfer/TaskTransferQuery",
    "http://10.164.128.92/claimcar/32170/taskTransfer/TaskTransferQuery",
    "http://10.164.128.92/claimcar/32160/taskTransfer/TaskTransferQuery",
)

# ============ 一级报价权限：报价汇总『换件金额』超过该值 → 业务动作『提交上级』 ============
HIGH_AMOUNT_THRESHOLD = 40000.0

# ============ 定损平台『零件名称』字体颜色规则 ============
# 橙色带下划线 = 通过标准点选方式添加 → 视同黑色，正常自动处理
PART_NAME_UNDERLINE_AS_BLACK = True
# 需要跳过的精确颜色，归一化后如 ("rgb(28,97,252)", "rgb(255,153,0)")
# 留空 = 使用内置启发式：蓝色、无下划线的橙色 视为需要人工处理的颜色
PART_NAME_SKIP_COLORS: tuple = ()

# ============ 超出权限补救：平级移交的目标人员（可配置，GUI 输入框覆盖） ============
TRANSFER_TARGET_DEFAULT = "17247339-胡晨光"

# 提交上级弹窗「报价任务提交」：提交路径 + 组别代码
TRANSFER_SUBMIT_PATH = "九级报价"
TRANSFER_GROUP_CODE = "5175"
TRANSFER_GROUP_OPTION = "1100000000005175-市公司报价组"
TRANSFER_GROUP_OPTION_KEY = "市公司报价组"
TRANSFER_GROUP_OPTION_EXCLUDE = "废弃"

# ============ 补救分支 2：已本地化换件超 5 万元 → 重新走分中心报价 ============
# 触发场景：提交上级后弹"已本地化换件的单项最大金额超过5万元,请提交远程操作平台处理!"
# 点"确认"后 → 再次选"提交上级" → 报价任务提交弹窗里提交路径默认就是"分中心报价"，
# 只需填组别代码 1 → 下拉选 "1000000000000127-总公司分中心报价组" → 提交任务
TRANSFER_SUBMIT_PATH_BRANCH = "分中心报价"
TRANSFER_GROUP_CODE_BRANCH = "1"
TRANSFER_GROUP_OPTION_BRANCH = "1000000000000127-总公司分中心报价组"
TRANSFER_GROUP_OPTION_KEY_BRANCH = "总公司分中心报价组"
TRANSFER_GROUP_OPTION_EXCLUDE_BRANCH = ""

# ============ 一车一件 真实外网 ============
MARKET_REAL_URL = "https://web.dataenlighten.com/AiMingJue/index.html#/"

# ============ 浏览器选择（多浏览器支持） ============
# auto / chrome / edge / 360 / bundled / custom
# 打包后的 exe 不带 Playwright 内置 Chromium，必须用本机已装的浏览器
BOT_BROWSER_PREFERENCE = "auto"
BOT_BROWSER_PATH = ""       # preference=custom 时的 chrome.exe 绝对路径

# ============ 超时 / 延迟 ============
DEFAULT_TIMEOUT = 15000
STEP_DELAY_MIN  = 400
STEP_DELAY_MAX  = 900

# ============ 一车一件（外网）反爬保护 ============
# 实测：手机号"触发防爬机制，禁用15分钟" —— 把速率降到人类操作节奏
# 理赔门户是公司内网，无反爬；这里的参数只给一车一件用
# ⚠️ 下面这几个减速/配额参数可在控制台『减速参数 / 查询配额』直接改，
#    点『开始扫描』时写入本模块（运行时生效），无需重新打包。
MARKET_VIN_DELAY_MIN     = 8.0    # 两次 VIN 查询之间的最小间隔（秒）
MARKET_VIN_DELAY_MAX     = 14.0
MARKET_PART_DELAY_MIN    = 5.0    # 同一 VIN 下两次零件查询之间（秒）
MARKET_PART_DELAY_MAX    = 9.0
MARKET_CLAIM_COOLDOWN    = 30.0   # 处理完一个报案号后到下一个 VIN 查询之间（秒，实际 ±30% 抖动）

# 反爬 / 验证码 / 封禁关键词（页面 body 出现任一即触发）
MARKET_BAN_KEYWORDS = (
    "防爬", "禁用", "封禁", "封号",
    "验证码", "人机验证", "滑块", "行为验证",
    "今日额度", "次数已达上限", "频率过高", "操作频繁", "过于频繁",
    "账号状态异常", "账号异常", "状态异常", "请稍后再试", "账号已锁定",
    "429", "Too Many Requests", "rate limit",
)
# 页面出现 [9016] 这类四位错误码 + 上述任一「异常/稍后再试」即视为封禁（9016 = 账号状态异常）
MARKET_BAN_CODE_PATTERN = r"\[\s*9\d{3}\s*\]"

# 滑动窗口配额（单位时间内的查询次数上限）—— 站点风控看的是累计量，不只看单次间隔
MARKET_QUOTA_WINDOW_SHORT = 10 * 60    # 10 分钟
MARKET_QUOTA_MAX_SHORT    = 20         # 10 分钟内最多 20 次查询（VIN + 零件合计）
MARKET_QUOTA_WINDOW_LONG  = 60 * 60    # 60 分钟
MARKET_QUOTA_MAX_LONG     = 80         # 60 分钟内最多 80 次查询

# 触发反爬后的自动休眠时间（秒）。2026-11-12 实测封禁后 30 分钟仍不可用 → 30 分钟
MARKET_BAN_COOLDOWN = 30 * 60

# ============ 选择器 ============
SELECTORS_PORTAL = {
    "claim_table":         "table#claim-table",
    "claim_no":            ".claim-no",
    "open_quote":          ".open-quote-btn",
    "edit_parts":          "#open-loss-edit",
    "vin_display":         "#vin-display",
    "loss_vin":            "#loss-vin, [data-field='vin'], [data-field='VIN'], [id*='vin'], [id*='Vin'], [class*='vin'], [class*='Vin'], .vin-value, #tb-vin, span.vin, div.vin",
    "parts_table":         "#partListTable, #parts-table, .bootstrap-table .table, .parts-table",
    "parts_tbody":         "#partListTable tbody, .bootstrap-table .table tbody, #parts-tbody, .parts-table tbody",
    "sync_loss_btn":       "#tbdsBtn, #sync-loss-btn, button:has-text('同步定损')",
    "select_all_parts":    "#btSelectAllD-0, #btSelectAll, thead input[type='checkbox']",
    "submit_loss":         "#submit-loss-btn, #carSubmit, button:has-text('提交定损'), button:has-text('提交')",
    "diagnosis_modal":     "#diagnosis-modal, .el-message-box, .el-dialog, .modal.in, .modal.show, .modal",
    "submit_loss_order":   "#submit-loss-order, button:has-text('提交定损单'), button:has-text('提交定损'), .el-message-box button:has-text('确定'), .modal button:has-text('确定')",

    # --- 报价页：业务动作 / 意见 / 提交按钮 / 结果提示 ---
    # 真实站点 DOM 实测（2026-11-03）：业务动作是 Element UI el-select 下拉框，
    # 选项文字为「报价完成 / 退回 / 提交上级」，不是 radio
    "quote_biz_action_select":   ".quote-opinion .el-select, .el-form-item:has-text('业务动作') .el-select",
    "quote_biz_action_trigger":  ".quote-opinion .el-select .el-input, .el-form-item:has-text('业务动作') .el-select .el-input",
    "quote_biz_dropdown_item":   ".el-select-dropdown:visible .el-select-dropdown__item",
    "quote_biz_done_text":       "报价完成",
    "quote_biz_superior_text":   "提交上级",
    "quote_biz_back_text":       "退回",
    "quote_opinion_section":     "#quote-opinion-section, .quote-opinion",
    "quote_opinion_select":      "#quote-opinion-select, select[name='quote-opinion'], select[name='opinion']",
    "quote_opinion_input":       "#quote-opinion-input, textarea[name='quote-opinion'], textarea[name='opinion'], textarea",
    "quote_submit_btn":          "#submit-quote-btn, button:has-text('提交')",
    "quote_submit_confirm":      "#confirm-quote-submit, button:has-text('提交任务')",
    "quote_result_tip":          "#quote-toast, .toast, .el-message, .el-notification",

    # --- 报价处理意见列表（P0 采集：容器/行/空态，真实站点未知时用文本定位兜底） ---
    "opinion_list_container":    ("#quote-opinion-list, .quote-opinion-list, "
                                  ".el-table:has-text('发表意见时间'), "
                                  ".el-table:has-text('意见说明'), "
                                  "table:has-text('意见说明')"),
    "opinion_list_row":          ("#quote-opinion-list tbody tr, .quote-opinion-list tbody tr, "
                                  ".el-table__body tr, table tbody tr"),
    "opinion_list_empty":        ".el-table__empty-text, .empty-text, .no-data",

    # --- 提交上级弹窗「报价任务提交」：提交路径 / 组别代码 / 人员代码 / 提交任务 ---
    "quote_task_dialog":         "#quote-task-dialog, .modal[data-modal='quote-task'], .el-dialog:has-text('报价任务提交')",
    "quote_task_path_input":     "#task-path-input, input[name='submitPath'], input[placeholder*='提交路径']",
    "quote_task_path_option":    "#task-path-options, .el-select-dropdown__item, .dropdown-item",
    "quote_task_group_input":    ("#task-group-input, input[name='groupCode'], "
                                  "input[placeholder='组号或组名'], input[placeholder*='组别代码']"),
    "quote_task_group_option":   "#task-group-options, .el-select-dropdown__item, .dropdown-item",
    "quote_task_staff_input":    "#task-staff-input, input[name='staffCode'], input[placeholder*='人员代码']",
    "quote_task_submit_btn":     "#quote-task-submit",
    # 当前任务环节（一级报价 / 九级报价）：九级报价说明已提过上级，不再重复提交
    "quote_task_stage_input":    "#task-stage-input, input[name='taskStage'], .el-form-item:has-text('当前任务环节') input",

    # --- 任务中心：平级移交（生产实测 2026-11-18 保存页 /claimcar/32180/taskTransfer/TaskTransferQuery）---
    # 报案号：label[for=registNo] → 同 form-item 内的 el-input（placeholder 只有“请输入”）
    "task_transfer_claim_input":  (".taskTransferQuery label[for='registNo'] + .el-form-item__content input, "
                                   ".taskTransferQuery .el-form-item:has(label[for='registNo']) input, "
                                   "label[for='registNo'] + .el-form-item__content input"),
    # 任务类型：label[for=nodeType] → el-select（下拉选项含 九级报价 / 分中心报价）
    "task_transfer_tasktype":     (".taskTransferQuery label[for='nodeType'] + .el-form-item__content .el-select, "
                                   ".taskTransferQuery .el-form-item:has(label[for='nodeType']) .el-select"),
    "task_transfer_query_btn":    ".footer-btns button.el-button--primary, button:has-text('查询')",
    "task_transfer_result_tbody": "#transfer-result-tbody, table tbody, .el-table__body-wrapper tbody",
    # 生产结果表里没有『移交』按钮，报案号本身是蓝色可点链接
    "task_transfer_row_btn":      ".transfer-btn, button:has-text('移交')",
    "task_transfer_claim_link":   "a, .el-link, span[class*='link']",
    # 任务提交弹窗（v-if 渲染，保存页里是空的）：div.el-dialog__wrapper.taskTransferDom
    "task_transfer_modal":        ".taskTransferDom .el-dialog, .el-dialog[aria-label='任务提交'], .el-dialog:has-text('任务提交')",
    # 生产实测（2026-11-18 录制）：目标人员输入框 placeholder 是『请输入员工代码或名称』，
    # 目标小组是『请输入组号或组员』——两个都是可编辑 input，
    # 旧选择器只认『人员/工号/姓名』会匹配不到而落到第一个 input（=目标小组），填错框。
    "task_transfer_target_input": ("input[placeholder*='员工代码'], input[placeholder*='员工名称'], "
                                   "input[placeholder*='员工'], "
                                   ".taskTransferDom input[placeholder*='人员'], "
                                   ".taskTransferDom input[placeholder*='工号'], "
                                   ".taskTransferDom input[placeholder*='姓名'], "
                                   "input[name='targetUser'], input[placeholder*='目标人员'], "
                                   "input[placeholder*='人员工号']"),
    "task_transfer_submit_btn":   (".taskTransferDom button.el-button--primary, .taskTransferDom button:has-text('提交'), "
                                   "#submit-task-btn, button:has-text('提交任务')"),
    "task_transfer_tip":          "#transfer-toast, .toast, .el-message",
}

# --- Mock 一车一件 ---
SELECTORS_MARKET_MOCK = {
    "vin_input":           "#vin-input",
    "vin_search":          "#vin-search-btn",
    "current_vin":         "#current-vin",
    "part_input":          "#part-input",
    "part_search":         "#part-search-btn",
    "result_tbody":        "#result-tbody",
    "market_price_cell":   "td.market-price-cell",
}

# ============ 本地配件价格缓存库 ============
# 目标：同一 VIN + 零件编码在有效期内只查一次一车一件
PRICE_CACHE_ENABLED = True          # 总开关（False = 完全回到无缓存行为）
PRICE_CACHE_VALID_DAYS = 90         # 价格有效期（天），过期强制回源；可在『数据管理』页修改
PRICE_CACHE_RETENTION_DAYS = 730    # 过期历史保留天数（用于回溯/趋势）
PRICE_CACHE_DB_NAME = "price_cache.db"   # 存放于 data/ 目录

# 查询端口优先级：第一个查不到再试下一个；未接入的端口会自动跳过
PRICE_PROVIDER_ORDER = ("yicheyijian", "jingyou")


# --- 真实外网一车一件（已按 一车一件.html 源码核对） ---
SELECTORS_MARKET_REAL = {
    # VIN 首页
    "vin_input":           "input.el-input__inner[placeholder='请输入17位VIN']",
    "vin_search":          "button.btn-search",
    # 零件查询详情页（VIN 查询成功后出现）
    "current_vin":         "body",
    "part_input":          "input[placeholder='请输入配件名称／OE号'], input.el-input__inner[placeholder*='配件']",
    # ⚠️ 零件查询按钮是 .btn-search；.btn-research.ev_btn 是『重新查询』（会退回 VIN 首页）
    "part_search":         "button.btn-search",
    # 查询结果区（section.part-list，div 布局，不是 <table>）
    "result_tbody":        ".part-list main ul",
    "market_price_cell":   ".cell.price-oe .price",
}

# ============ 策略（运行时可被 main.py 覆盖） ============
USE_MOCK_MARKET = True   # main.py 会根据 --market-real 覆盖
USE_MOCK_PORTAL = True

# 价格容忍度：绝对金额（元）；如 2 表示「超出市场价 2 元以内不算超」
PRICE_TOLERANCE_AMOUNT = 0.0

# 循环挂机：列表没有新案件时的刷新轮询间隔（秒），会一直等到有新案件或手动停止
LOOP_POLL_INTERVAL = 10.0


def get_market_selectors() -> dict:
    """运行时根据 USE_MOCK_MARKET 返回正确的 selector 集合"""
    return SELECTORS_MARKET_MOCK if USE_MOCK_MARKET else SELECTORS_MARKET_REAL
