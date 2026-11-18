"""本地配件价格库（SQLite）

只增不改：每次回源都插入新行，靠 expires_at 判断是否有效，天然保留历史。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from bot.pricing.models import CacheStats, PriceQuote

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle (
    vin         TEXT PRIMARY KEY,
    brand       TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    query_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS part_price (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vin        TEXT NOT NULL,
    part_code  TEXT NOT NULL,
    part_name  TEXT DEFAULT '',
    price      REAL NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    meta       TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_part_price_lookup
    ON part_price(vin, part_code, source, fetched_at DESC);

CREATE TABLE IF NOT EXISTS query_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    vin       TEXT DEFAULT '',
    part_code TEXT DEFAULT '',
    action    TEXT NOT NULL,
    source    TEXT DEFAULT '',
    price     REAL,
    detail    TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_query_log_ts ON query_log(ts DESC);
"""


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class PriceStore:
    """SQLite 读写封装；任何异常只影响当次操作，不向上抛"""

    def __init__(self, db_path: Path, retention_days: int = 730):
        self.db_path = Path(db_path)
        self.retention_days = max(30, int(retention_days or 730))
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    # ---------- 连接 ----------
    def _open(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=5.0)
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
        except sqlite3.DatabaseError:
            # 文件损坏 → 备份后重建，退化为空库（不阻断主流程）
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            try:
                bak = self.db_path.with_suffix(self.db_path.suffix + ".corrupt")
                self.db_path.replace(bak)
            except Exception:
                pass
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, timeout=5.0)
            with self._lock:
                self._conn.executescript(_SCHEMA)
                self._conn.commit()

    def close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    # ---------- 写入 ----------
    def touch_vehicle(self, vin: str, brand: str = "", model: str = "") -> None:
        if not vin or self._conn is None:
            return
        now = _iso(_now())
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO vehicle(vin, brand, model, first_seen, last_seen, query_count)
                       VALUES(?,?,?,?,?,1)
                       ON CONFLICT(vin) DO UPDATE SET
                         last_seen=excluded.last_seen,
                         query_count=vehicle.query_count+1,
                         brand=CASE WHEN excluded.brand<>'' THEN excluded.brand ELSE vehicle.brand END,
                         model=CASE WHEN excluded.model<>'' THEN excluded.model ELSE vehicle.model END""",
                    (vin, brand or "", model or "", now, now))
                self._conn.commit()
        except sqlite3.Error:
            pass

    def put(self, vin: str, part_code: str, price: Optional[float],
            source: str, valid_days: int, part_name: str = "",
            meta: Optional[dict] = None) -> bool:
        """写入一条价格；price 无效时不写，避免缓存空值"""
        if (not vin or not part_code or price is None or price <= 0
                or self._conn is None):
            return False
        fetched = _now()
        expires = fetched + timedelta(days=max(1, int(valid_days or 30)))
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO part_price
                       (vin, part_code, part_name, price, source, fetched_at, expires_at, meta)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (vin, part_code, part_name or "", float(price), source or "",
                     _iso(fetched), _iso(expires),
                     json.dumps(meta or {}, ensure_ascii=False)))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    def log(self, vin: str, part_code: str, action: str,
            source: str = "", price: Optional[float] = None,
            detail: str = "") -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO query_log(ts, vin, part_code, action, source, price, detail)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (_iso(_now()), vin or "", part_code or "", action,
                     source or "", price, detail or ""))
                self._conn.commit()
        except sqlite3.Error:
            pass

    # ---------- 读取 ----------
    def _row_to_quote(self, row) -> PriceQuote:
        meta = {}
        if row[6]:
            try:
                meta = json.loads(row[6])
            except Exception:
                meta = {}
        return PriceQuote(part_code=row[0], price=row[1], source=row[2],
                          part_name=row[3] or "", fetched_at=row[4] or "",
                          expires_at=row[5] or "", meta=meta)

    def get_fresh(self, vin: str, part_code: str,
                  sources: Optional[Iterable[str]] = None) -> Optional[PriceQuote]:
        """取仍在有效期内的最新价格"""
        if not vin or not part_code or self._conn is None:
            return None
        sql = ("SELECT part_code, price, source, part_name, fetched_at, expires_at, meta"
               " FROM part_price WHERE vin=? AND part_code=? AND expires_at>?")
        args: list = [vin, part_code, _iso(_now())]
        srcs = [s for s in (sources or []) if s]
        if srcs:
            sql += " AND source IN (%s)" % ",".join("?" * len(srcs))
            args.extend(srcs)
        sql += " ORDER BY fetched_at DESC LIMIT 1"
        try:
            with self._lock:
                row = self._conn.execute(sql, args).fetchone()
            return self._row_to_quote(row) if row else None
        except sqlite3.Error:
            return None

    def get_latest(self, vin: str, part_code: str) -> Optional[PriceQuote]:
        """取最近一次价格（无论是否过期），仅用于展示/降级参考"""
        if not vin or not part_code or self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT part_code, price, source, part_name, fetched_at, expires_at, meta"
                    " FROM part_price WHERE vin=? AND part_code=?"
                    " ORDER BY fetched_at DESC LIMIT 1",
                    (vin, part_code)).fetchone()
            return self._row_to_quote(row) if row else None
        except sqlite3.Error:
            return None

    def stats(self) -> CacheStats:
        st = CacheStats()
        if self._conn is None:
            return st
        try:
            with self._lock:
                st.vehicles = self._conn.execute(
                    "SELECT COUNT(*) FROM vehicle").fetchone()[0]
                st.prices = self._conn.execute(
                    "SELECT COUNT(*) FROM part_price").fetchone()[0]
                st.fresh_prices = self._conn.execute(
                    "SELECT COUNT(*) FROM part_price WHERE expires_at>?",
                    (_iso(_now()),)).fetchone()[0]
                st.hit_count = self._conn.execute(
                    "SELECT COUNT(*) FROM query_log WHERE action='cache_hit'").fetchone()[0]
                st.remote_count = self._conn.execute(
                    "SELECT COUNT(*) FROM query_log WHERE action='remote_query'").fetchone()[0]
        except sqlite3.Error:
            pass
        return st

    def purge(self) -> int:
        """清理超过保留期的历史行"""
        if self._conn is None:
            return 0
        cutoff = _iso(_now() - timedelta(days=self.retention_days))
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM part_price WHERE expires_at<?", (cutoff,))
                self._conn.commit()
            return cur.rowcount or 0
        except sqlite3.Error:
            return 0
