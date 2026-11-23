"""价格缓存服务：本地优先、过期回源、端口编排"""
from __future__ import annotations

from datetime import datetime
from typing import List, Sequence, Tuple

from bot.pricing.models import CacheStats
from bot.pricing.store import PriceStore

_SOURCE_LABELS = {
    "yicheyijian": "一车一件",
    "jingyou": "精友",
    "manual": "手工录入",
}


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _days_between(start: str, end: str) -> tuple[int, int]:
    """返回 (已缓存天数, 剩余有效天数)；解析失败返回 (0, 0)"""
    a, b = _parse_dt(start), _parse_dt(end)
    if a is None or b is None:
        return 0, 0
    now = datetime.now()
    return max(0, (now - a).days), max(0, (b - now).days)


class PriceService:
    def __init__(self, store: PriceStore, valid_days: int, enabled: bool = True):
        self.store = store
        self.valid_days = max(1, int(valid_days or 30))
        self.enabled = bool(enabled)

    # ---------- 车辆 ----------
    def touch_vin(self, vin: str, brand: str = "", model: str = "") -> None:
        if self.enabled:
            self.store.touch_vehicle(vin, brand, model)

    # ---------- 查价 ----------
    def resolve(self, vin: str, parts: Sequence,
                tolerance: float = 0.0,
                sources: Sequence[str] = ()) -> Tuple[List, List]:
        """把命中新鲜缓存的零件直接写回 market_price

        返回 (需要远程查询的零件, 命中缓存的零件)
        """
        remote: List = []
        cached: List = []
        if not self.enabled:
            return list(parts), []

        total = len(parts)
        for idx, p in enumerate(parts, 1):
            code = getattr(p, "code", "") or ""
            quote = self.store.get_fresh(vin, code, sources=sources)
            if quote and quote.valid_price:
                p.market_price = float(quote.price)
                try:
                    p.check(tolerance)
                except Exception:
                    pass
                cached.append(p)
                age, remain = _days_between(quote.fetched_at, quote.expires_at)
                label = _SOURCE_LABELS.get(quote.source, quote.source or "本地")
                status = getattr(p, "status_text", "") or ""
                print(f"  💾 [{idx}/{total}] 本地价格库命中: {code}  "
                      f"{getattr(p, 'name', '') or quote.part_name} → 市场价 ¥{quote.price:g}"
                      f"（{label}，缓存 {age} 天，剩余 {remain} 天）"
                      + (f"  {status}" if status else ""))
                self.store.log(vin, code, "cache_hit", source=quote.source,
                               price=quote.price,
                               detail=f"fetched_at={quote.fetched_at} "
                                      f"cached_days={age} remain_days={remain}")
            else:
                if quote is not None:
                    self.store.log(vin, code, "cache_expired",
                                   source=quote.source,
                                   detail=f"expires_at={quote.expires_at}")
                remote.append(p)
        return remote, cached

    # ---------- 落库 ----------
    def save(self, vin: str, parts: Sequence, source: str = "yicheyijian") -> int:
        """把本次远程查到的有效价格写库，返回写入条数"""
        if not self.enabled:
            return 0
        n = 0
        for p in parts:
            price = getattr(p, "market_price", None)
            if price is None or price <= 0:
                continue
            ok = self.store.put(
                vin, getattr(p, "code", "") or "", float(price),
                source=source, valid_days=self.valid_days,
                part_name=getattr(p, "name", "") or "",
                meta={"local_price": getattr(p, "local_price", None),
                      "price_scheme": getattr(p, "price_scheme", "")})
            if ok:
                n += 1
                self.store.log(vin, getattr(p, "code", ""), "remote_query",
                               source=source, price=price)
        return n

    # ---------- 报案零件改价历史（防重复下调） ----------
    _LEDGER_IMPORT_KEY = "claim_quote_fix_ledger_imported"

    def _ensure_quote_fix_history_imported(self) -> bool:
        if self.store.get_setting(self._LEDGER_IMPORT_KEY, "") == "1":
            return True
        try:
            from bot.ledger import iter_fixed_quote_records
            records = list(iter_fixed_quote_records())
        except Exception as exc:
            print(f"  ⚠️ 读取历史 Excel 改价记录失败: "
                  f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            return False
        if not self.store.add_claim_quote_fixes(records):
            print("  ⚠️ 历史改价记录写入 SQLite 失败")
            return False
        if not self.store.set_setting(self._LEDGER_IMPORT_KEY, "1"):
            print("  ⚠️ 历史改价记录导入标记保存失败")
            return False
        print(f"  [i] 已合并历史 Excel 改价记录 {len(records)} 条（仅首次执行）")
        return True

    def get_claim_quote_fixes(self, claim_no: str) -> dict[str, dict] | None:
        """None 表示历史不可用；调用方不得在无护栏状态下继续自动改价"""
        if not self._ensure_quote_fix_history_imported():
            return None
        return self.store.get_claim_quote_fixes(claim_no)

    def record_claim_quote_fix(self, claim_no: str, part_code: str,
                               quote_price: float, part_name: str = "") -> bool:
        return self.store.add_claim_quote_fix(
            claim_no, part_code, quote_price, part_name=part_name)

    # ---------- 本工号处理过的报案号（决定提交上级后的收回路径） ----------
    _CLAIM_HISTORY_IMPORT_KEY = "claim_process_history_imported"

    def _ensure_claim_history_imported(self) -> bool:
        if self.store.get_setting(self._CLAIM_HISTORY_IMPORT_KEY, "") == "1":
            return True
        try:
            from bot.ledger import iter_claim_numbers
            records = list(iter_claim_numbers())
        except Exception as exc:
            print(f"  ⚠️ 读取历史工作记录报案号失败: "
                  f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            return False
        if not self.store.add_claim_process_history(records):
            print("  ⚠️ 历史报案号写入 SQLite 失败")
            return False
        if not self.store.set_setting(self._CLAIM_HISTORY_IMPORT_KEY, "1"):
            print("  ⚠️ 历史报案号导入标记保存失败")
            return False
        print(f"  [i] 已合并历史工作记录报案号 {len(records)} 条（仅首次执行）")
        return True

    def claim_processed_before(self, claim_no: str) -> bool:
        """本工号之前是否处理过该报案号；历史不可用时按首次接收处理"""
        if not self._ensure_claim_history_imported():
            return False
        return self.store.claim_processed_before(claim_no)

    def mark_claim_processed(self, claim_no: str) -> bool:
        return self.store.mark_claim_processed(claim_no)

    # ---------- 因蓝色/纯橙色退回过的报案号（二次进入按正常案件处理） ----------
    def claim_color_returned_before(self, claim_no: str) -> bool:
        return self.store.claim_color_returned_before(claim_no)

    def mark_claim_color_returned(self, claim_no: str, note: str = "") -> bool:
        return self.store.mark_claim_color_returned(claim_no, note=note)

    # ---------- 统计 ----------
    def stats(self) -> CacheStats:
        return self.store.stats()

    def purge(self) -> int:
        return self.store.purge() if self.enabled else 0
