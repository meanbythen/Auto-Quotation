"""
数据模型
"""
from dataclasses import dataclass, field
from typing import List, Optional


# ===== 价格方案代码常量（与定损平台 part_util.js 一致） =====
SCHEME_4S = "MS_RETAIL_PRICE"           # 4S价
SCHEME_MARKET = "MARKET_PRICE"          # 纯市场价
SCHEME_BRANK = "MARKET_BRANK_PRICE"     # 品牌市场价（本地价已打折）
SCHEME_RECYCLE = "MARKET_RECYCLE_PRICE" # 回收件市场价

SCHEME_DISPLAY = {
    SCHEME_4S: "4S价",
    SCHEME_MARKET: "市场价",
    SCHEME_BRANK: "市场价",
    SCHEME_RECYCLE: "市场价",
}


@dataclass
class PartItem:
    """换件信息 / 零件条目"""
    index: int                 # 序号
    code: str                  # 零件编码
    name: str                  # 零件名称
    qty: int                   # 数量
    price_type: str            # 价格类型代码（S/O/A/R 等，来自 data-lossobj 里的 estiSchemeCode 映射显示）
    local_price: float         # 本地价（定损平台当前展示的本地价，不可修改；A类方案下是打折后的）
    list_price: Optional[float] = None       # 定损价 = 平台当前报价（data-lossobj.estiPartPrice，修复公式分子）
    guide_price: Optional[float] = None      # 市场指导价（data-lossobj.guidePrice，仅备查，不参与公式）
    market_price: Optional[float] = None     # 市场价（一车一件查询到的）
    within_market: Optional[bool] = None     # 是否在市场价范围内
    fixed_price: Optional[float] = None      # 修复后的「报价」列值（不是本地价）
    direct_supply_price: Optional[float] = None  # 直供价（与编码/名称/本地价同行的那一列）
    # ===== 新增：价格方案 + S价格值 =====
    price_scheme: str = ""               # 价格方案显示名："4S价" / "市场价"
    price_type_code: str = ""            # 价格方案代码：MS_RETAIL_PRICE / MARKET_BRANK_PRICE / ...
    s_price_value: Optional[float] = None  # ★ S价格值 = msRetailPrice（用来跟一车一件对比的统一基准价）

    @property
    def suggested_fixed_price(self) -> Optional[float]:
        """
        自动修复公式（统一版）：报价 = rounddown(一车一件价 × 定损价 / S价格值)

        分母统一用 S 价格值（即 msRetailPrice，悬浮窗里显示的那个）：
          - 4S方案(S)：S值 = 本地价 = 4S价，跟一车一件直接比
          - 品牌方案(A)：本地价已打折，但 S 值仍是 4S 原价，比价用 S 值，
                         若 S > 一车一件价 + 容忍度，等比例降低报价

        字段对应：
          - 一车一件价 = market_price
          - 定损价     = list_price（同步定损后的报价列值），guide_price 兜底
          - S价格值    = s_price_value（msRetailPrice），local_price 兜底
        """
        denom = self.s_price_value or self.local_price
        numer = self.list_price or self.guide_price
        if (numer is None or self.market_price is None
                or not denom or denom <= 0):
            return None
        return round(self.market_price * numer / denom, 0)

    def check(self, tolerance_amount: float = 0.0) -> None:
        """
        比对 S价格值 vs 一车一件市场价

        容忍度 = 绝对金额，market_price + tolerance 以内视为合规。
        分母统一用 S 价格值（不是 local_price），因为 A 类方案的 local_price 已打折。
        """
        if self.market_price is None:
            self.within_market = None
            return
        compare_base = self.s_price_value or self.local_price
        threshold = self.market_price + max(0.0, tolerance_amount or 0.0)
        self.within_market = compare_base <= threshold

    @property
    def status_text(self) -> str:
        if self.within_market is True:
            return "✅ 合规"
        elif self.within_market is False:
            base = self.s_price_value or self.local_price
            base_label = "S价" if self.s_price_value else "本地价"
            return f"⚠️ 超市场价 {base_label}{base} > 市场价{self.market_price}"
        return "⏳ 待查询"


@dataclass
class VehicleInfo:
    """车辆信息"""
    claim_no: str = ""          # 报案号
    vin: str = ""               # 定损VIN
    plate: str = ""             # 车牌号
    brand: str = ""
    series: str = ""
    year: str = ""
    model: str = ""


@dataclass
class BotRunResult:
    """一次完整流程的结果"""
    vehicle: VehicleInfo = field(default_factory=VehicleInfo)
    parts: List[PartItem] = field(default_factory=list)
    over_market_parts: List[PartItem] = field(default_factory=list)
    market_queries_count: int = 0
    status: str = "running"     # running / done / failed
    messages: List[str] = field(default_factory=list)
