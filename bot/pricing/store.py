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

CREATE TABLE IF NOT EXISTS app_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_part_quote_fix (
    claim_no    TEXT NOT NULL,
    part_code   TEXT NOT NULL,
    part_name   TEXT DEFAULT '',
    quote_price REAL,
    fixed_at    TEXT NOT NULL,
    PRIMARY KEY (claim_no, part_code)
);

CREATE TABLE IF NOT EXISTS claim_process_history (
    claim_no            TEXT PRIMARY KEY,
    first_processed_at  TEXT NOT NULL,
    last_processed_at   TEXT NOT NULL,
    passes              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS claim_color_return_history (
    claim_no    TEXT PRIMARY KEY,
    returned_at TEXT NOT NULL,
    pass_count  INTEGER NOT NULL DEFAULT 1,
    note        TEXT DEFAULT ''
);

-- 报价处理意见语料（P0：只采集，不改变执行）
CREATE TABLE IF NOT EXISTS opinion_corpus (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_no     TEXT NOT NULL,
    staff_code   TEXT DEFAULT '',
    org_name     TEXT DEFAULT '',
    publish_time TEXT DEFAULT '',
    opinion      TEXT DEFAULT '',
    opinion_note TEXT DEFAULT '',
    return_eval  TEXT DEFAULT '',
    submit_path  TEXT DEFAULT '',
    prev_action  TEXT DEFAULT '',
    source       TEXT DEFAULT '',
    raw_json     TEXT DEFAULT '',
    captured_at  TEXT NOT NULL,
    dedup_hash   TEXT NOT NULL,
    UNIQUE(claim_no, dedup_hash)
);

-- 人工/规则标签（P1 使用，P0 先建表）
CREATE TABLE IF NOT EXISTS opinion_label (
    corpus_id  INTEGER PRIMARY KEY,
    action     TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    labeler    TEXT DEFAULT 'human',
    labeled_at TEXT NOT NULL,
    note       TEXT DEFAULT ''
);

-- 每次判断的预测记录（P0 起写入，供灰度对比）
CREATE TABLE IF NOT EXISTS opinion_prediction (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_no       TEXT NOT NULL,
    corpus_id      INTEGER,
    action         TEXT NOT NULL,
    confidence     REAL NOT NULL,
    model_version  TEXT DEFAULT '',
    decided_action TEXT DEFAULT '',
    overridden_by  TEXT DEFAULT '',
    executed       INTEGER DEFAULT 0,
    ts             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opinion_corpus_claim ON opinion_corpus(claim_no);
CREATE INDEX IF NOT EXISTS idx_opinion_prediction_ts ON opinion_prediction(ts DESC);
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

    # ---------- 价格有效期设置 ----------
    def get_valid_days(self, default: int = 90) -> int:
        """读取用户设置的有效期（天），未设置返回默认值"""
        if self._conn is None:
            return default
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM app_setting WHERE key='price_valid_days'").fetchone()
        except sqlite3.Error:
            return default
        if not row:
            return default
        try:
            return max(1, min(3650, int(row[0])))
        except (TypeError, ValueError):
            return default

    def set_valid_days(self, days: int) -> int:
        """保存有效期，并按 fetched_at 重算已有记录的到期时间；返回重算行数"""
        days = max(1, min(3650, int(days)))
        if self._conn is None:
            return 0
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO app_setting(key, value) VALUES('price_valid_days', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),))
                cur = self._conn.execute(
                    "UPDATE part_price SET expires_at ="
                    " strftime('%Y-%m-%dT%H:%M:%S', datetime(fetched_at, ?))",
                    (f"+{days} days",))
                self._conn.commit()
        except sqlite3.Error:
            return 0
        return cur.rowcount or 0

    # ---------- 通用设置 ----------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if self._conn is None:
            return default
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default
        except sqlite3.Error:
            return default

    def set_setting(self, key: str, value: str) -> bool:
        if self._conn is None:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO app_setting(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ---------- 报案零件改价历史（幂等护栏，不受价格缓存开关影响） ----------
    def get_claim_quote_fixes(self, claim_no: str) -> Optional[dict[str, dict]]:
        """返回零件键 → 首次改价信息；None 表示读取失败，调用方应停止自动改价"""
        claim_no = (claim_no or "").strip().upper()
        if not claim_no or self._conn is None:
            return None
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT part_code, part_name, quote_price, fixed_at"
                    " FROM claim_part_quote_fix WHERE claim_no=?", (claim_no,)).fetchall()
            return {r[0]: {"part_name": r[1] or "", "quote_price": r[2],
                           "fixed_at": r[3] or ""} for r in rows}
        except sqlite3.Error:
            return None

    def add_claim_quote_fix(self, claim_no: str, part_code: str,
                            quote_price: Optional[float], part_name: str = "",
                            fixed_at: str = "") -> bool:
        return self.add_claim_quote_fixes([{
            "claim_no": claim_no, "part_code": part_code,
            "quote_price": quote_price, "part_name": part_name,
            "fixed_at": fixed_at,
        }])

    def add_claim_quote_fixes(self, records: Iterable[dict]) -> bool:
        """只登记首次改价；已存在记录保持原值，空集合也视为成功"""
        if self._conn is None:
            return False
        rows = []
        for rec in records:
            claim_no = str(rec.get("claim_no") or "").strip().upper()
            part_code = str(rec.get("part_code") or "").strip().upper()
            if not claim_no or not part_code:
                continue
            price = rec.get("quote_price")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            fixed_at = str(rec.get("fixed_at") or "") or _iso(_now())
            rows.append((claim_no, part_code, str(rec.get("part_name") or ""),
                         price, fixed_at))
        try:
            with self._lock:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO claim_part_quote_fix"
                    "(claim_no, part_code, part_name, quote_price, fixed_at)"
                    " VALUES(?,?,?,?,?)", rows)
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ---------- 本工号处理过的报案号（决定提交上级后的收回路径） ----------
    def claim_processed_before(self, claim_no: str) -> bool:
        claim_no = (claim_no or "").strip().upper()
        if not claim_no or self._conn is None:
            return False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM claim_process_history WHERE claim_no=?",
                    (claim_no,)).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def mark_claim_processed(self, claim_no: str,
                             processed_at: str = "") -> bool:
        claim_no = (claim_no or "").strip().upper()
        if not claim_no or self._conn is None:
            return False
        ts = processed_at or _iso(_now())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO claim_process_history"
                    "(claim_no, first_processed_at, last_processed_at, passes)"
                    " VALUES(?,?,?,1)"
                    " ON CONFLICT(claim_no) DO UPDATE SET"
                    " last_processed_at=excluded.last_processed_at,"
                    " passes=claim_process_history.passes+1",
                    (claim_no, ts, ts))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    def add_claim_process_history(self, records: Iterable[dict]) -> bool:
        """历史工作记录导入：只补充不存在的报案号，空集合也视为成功"""
        if self._conn is None:
            return False
        rows = []
        for rec in records:
            claim_no = str(rec.get("claim_no") or "").strip().upper()
            if not claim_no:
                continue
            ts = str(rec.get("processed_at") or "") or _iso(_now())
            rows.append((claim_no, ts, ts, 1))
        if not rows:
            return True
        try:
            with self._lock:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO claim_process_history"
                    "(claim_no, first_processed_at, last_processed_at, passes)"
                    " VALUES(?,?,?,?)", rows)
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ---------- 报价处理意见语料 / 判断记录 ----------
    def add_opinion_corpus(self, record: dict) -> int:
        """写入一条意见语料；重复（同报案号+同意见）返回已有 id，失败返回 0"""
        if self._conn is None:
            return 0
        claim_no = str(record.get("claim_no") or "").strip().upper()
        dedup = str(record.get("dedup_hash") or "").strip()
        if not claim_no or not dedup:
            return 0
        ts = str(record.get("captured_at") or "") or _iso(_now())
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO opinion_corpus"
                    "(claim_no, staff_code, org_name, publish_time, opinion,"
                    " opinion_note, return_eval, submit_path, prev_action,"
                    " source, raw_json, captured_at, dedup_hash)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (claim_no,
                     str(record.get("staff_code") or ""),
                     str(record.get("org_name") or ""),
                     str(record.get("publish_time") or ""),
                     str(record.get("opinion") or ""),
                     str(record.get("opinion_note") or ""),
                     str(record.get("return_eval") or ""),
                     str(record.get("submit_path") or ""),
                     str(record.get("prev_action") or ""),
                     str(record.get("source") or ""),
                     str(record.get("raw_json") or ""),
                     ts, dedup))
                self._conn.commit()
                if cur.rowcount:
                    return int(cur.lastrowid or 0)
                row = self._conn.execute(
                    "SELECT id FROM opinion_corpus WHERE claim_no=? AND dedup_hash=?",
                    (claim_no, dedup)).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def count_opinion_corpus(self) -> int:
        if self._conn is None:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM opinion_corpus").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def iter_opinion_corpus(self) -> list[dict]:
        """导出用：按采集时间倒序返回全部语料"""
        if self._conn is None:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, claim_no, staff_code, org_name, publish_time,"
                    " opinion, opinion_note, return_eval, submit_path,"
                    " prev_action, source, captured_at FROM opinion_corpus"
                    " ORDER BY captured_at DESC").fetchall()
        except sqlite3.Error:
            return []
        keys = ("id", "claim_no", "staff_code", "org_name", "publish_time",
                "opinion", "opinion_note", "return_eval", "submit_path",
                "prev_action", "source", "captured_at")
        return [dict(zip(keys, r)) for r in rows]

    def add_opinion_prediction(self, record: dict) -> bool:
        if self._conn is None:
            return False
        ts = str(record.get("ts") or "") or _iso(_now())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO opinion_prediction"
                    "(claim_no, corpus_id, action, confidence, model_version,"
                    " decided_action, overridden_by, executed, ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(record.get("claim_no") or ""),
                     int(record.get("corpus_id") or 0),
                     str(record.get("action") or "unknown"),
                     float(record.get("confidence") or 0.0),
                     str(record.get("model_version") or ""),
                     str(record.get("decided_action") or ""),
                     str(record.get("overridden_by") or ""),
                     int(record.get("executed") or 0),
                     ts))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ---------- 因蓝色/纯橙色退回过的报案号（二次进入按正常案件处理） ----------
    def claim_color_returned_before(self, claim_no: str) -> bool:
        claim_no = (claim_no or "").strip().upper()
        if not claim_no or self._conn is None:
            return False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM claim_color_return_history WHERE claim_no=?",
                    (claim_no,)).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def mark_claim_color_returned(self, claim_no: str, note: str = "") -> bool:
        claim_no = (claim_no or "").strip().upper()
        if not claim_no or self._conn is None:
            return False
        ts = _iso(_now())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO claim_color_return_history"
                    "(claim_no, returned_at, pass_count, note)"
                    " VALUES(?,?,1,?)"
                    " ON CONFLICT(claim_no) DO UPDATE SET"
                    " returned_at=excluded.returned_at,"
                    " pass_count=claim_color_return_history.pass_count+1,"
                    " note=excluded.note",
                    (claim_no, ts, note))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    # ---------- 数据管理（综合查询 / 统计） ----------
    def _part_filters(self, part_code: str = "", part_name: str = "",
                      source: str = "", status: str = "",
                      vin: str = "") -> tuple[str, list, str]:
        """拼 WHERE：status=fresh 有效 / expired 已过期 / 空 全部"""
        now = _iso(_now())
        sql = " FROM part_price p LEFT JOIN vehicle v ON v.vin=p.vin WHERE 1=1"
        args: list = []
        if vin.strip():
            sql += " AND p.vin LIKE ?"
            args.append(f"%{vin.strip()}%")
        if part_code.strip():
            sql += " AND p.part_code LIKE ?"
            args.append(f"%{part_code.strip()}%")
        if part_name.strip():
            sql += " AND p.part_name LIKE ?"
            args.append(f"%{part_name.strip()}%")
        if source:
            sql += " AND p.source=?"
            args.append(source)
        if status == "fresh":
            sql += " AND p.expires_at>?"
            args.append(now)
        elif status == "expired":
            sql += " AND p.expires_at<=?"
            args.append(now)
        return sql, args, now

    def search_parts(self, *, part_code: str = "", part_name: str = "",
                     source: str = "", status: str = "", vin: str = "",
                     limit: int = 500, offset: int = 0) -> tuple[list[dict], int]:
        """条件查询零件价格记录，返回 (当前页行, 总条数)"""
        if self._conn is None:
            return [], 0
        where, args, now = self._part_filters(part_code, part_name, source, status, vin)
        sql = ("SELECT p.vin, p.part_code, p.part_name, p.price, p.source,"
               " p.fetched_at, p.expires_at, v.brand, v.model,"
               " CASE WHEN p.expires_at>? THEN 1 ELSE 0 END" + where +
               " ORDER BY p.fetched_at DESC, p.id DESC LIMIT ? OFFSET ?")
        try:
            with self._lock:
                total = self._conn.execute(
                    "SELECT COUNT(*)" + where, args).fetchone()[0]
                rows = self._conn.execute(
                    sql, [now] + args + [max(1, int(limit)), max(0, int(offset))]).fetchall()
        except sqlite3.Error:
            return [], 0
        out = []
        for r in rows:
            out.append({
                "vin": r[0] or "", "part_code": r[1] or "", "part_name": r[2] or "",
                "price": r[3], "source": r[4] or "",
                "fetched_at": r[5] or "", "expires_at": r[6] or "",
                "brand": r[7] or "", "model": r[8] or "", "fresh": bool(r[9]),
            })
        return out, total

    def iter_parts(self, *, part_code: str = "", part_name: str = "",
                   source: str = "", status: str = "", vin: str = ""):
        """按条件惰性遍历全部命中记录（导出 Excel 用，不一次性载入内存）"""
        if self._conn is None:
            return
        where, args, now = self._part_filters(part_code, part_name, source, status, vin)
        sql = ("SELECT p.vin, p.part_code, p.part_name, p.price, p.source,"
               " p.fetched_at, p.expires_at, v.brand, v.model,"
               " CASE WHEN p.expires_at>? THEN 1 ELSE 0 END" + where +
               " ORDER BY p.fetched_at DESC, p.id DESC")
        try:
            with self._lock:
                for r in self._conn.execute(sql, [now] + args):
                    yield {
                        "vin": r[0] or "", "part_code": r[1] or "",
                        "part_name": r[2] or "", "price": r[3], "source": r[4] or "",
                        "fetched_at": r[5] or "", "expires_at": r[6] or "",
                        "brand": r[7] or "", "model": r[8] or "", "fresh": bool(r[9]),
                    }
        except sqlite3.Error:
            return

    def source_stats(self) -> list[tuple[str, int]]:
        """按来源统计记录数（数据管理页来源筛选/分布用）"""
        if self._conn is None:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT source, COUNT(*) FROM part_price"
                    " GROUP BY source ORDER BY COUNT(*) DESC").fetchall()
        except sqlite3.Error:
            return []
        return [(r[0] or "", int(r[1])) for r in rows]
