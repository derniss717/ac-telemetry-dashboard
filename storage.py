# -*- coding: utf-8 -*-
"""SQLite 存储：sessions / laps / frames。

frames.payload 为 float32 全通道 BLOB（顺序见 ac_udp.CHANNELS），
关键列（t/dist/speed/gear/rpms/lap_ms/sector）冗余为索引列方便查询。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_ts REAL,
    end_ts REAL,
    car TEXT,
    track TEXT,
    best_lap_ms INT,
    channels_json TEXT
);
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INT,
    lap_no INT,
    start_frame INT,
    end_frame INT,
    total_ms INT,
    s1_ms INT,
    s2_ms INT,
    s3_ms INT,
    top_speed_kmh REAL,
    is_valid INT,
    is_inlap INT,
    meta_json TEXT,
    tyre_compound TEXT,
    outlap INT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INT,
    lap_id INT,
    t REAL,
    dist REAL,
    speed_kmh REAL,
    gear INT,
    rpms INT,
    lap_ms INT,
    sector INT,
    payload BLOB
);
CREATE INDEX IF NOT EXISTS idx_frames_sess ON frames(session_id, id);
CREATE INDEX IF NOT EXISTS idx_frames_lap  ON frames(session_id, lap_id);
"""

_FRAME_INSERT = (
    "INSERT INTO frames (session_id, lap_id, t, dist, speed_kmh, gear, rpms,"
    " lap_ms, sector, payload) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


class Storage:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        # 旧库兼容：补缺的列（老库可能缺 tyre_compound/outlap，缺了会 SQL 报错 → 圈列表空/close 失败）
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(laps)")}
        if "tyre_compound" not in cols:
            self.conn.execute("ALTER TABLE laps ADD COLUMN tyre_compound TEXT")
        if "outlap" not in cols:
            self.conn.execute("ALTER TABLE laps ADD COLUMN outlap INT DEFAULT 0")
        self.conn.commit()
        self._frame_stmt = self.conn.executemany  # 预编译见 insert_frames

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.conn.close()

    # ---------------- sessions ----------------
    def open_session(self, name: str, channels_json: str,
                     car: str = "", track: str = "") -> int:
        # RETURNING 读新 ID（lastrowid 是连接级，多线程并发写会被覆盖）
        cur = self.conn.execute(
            "INSERT INTO sessions (name, start_ts, car, track, channels_json)"
            " VALUES (?,?,?,?,?) RETURNING id",
            (name, time.time(), car, track, channels_json),
        )
        row = cur.fetchone()
        self.conn.commit()
        return int(row[0])

    def close_session(self, session_id: int, best_lap_ms: Optional[int] = None) -> None:
        if best_lap_ms is None:
            row = self.conn.execute(
                "SELECT MIN(total_ms) FROM laps WHERE session_id=? AND is_valid=1",
                (session_id,),
            ).fetchone()
            best_lap_ms = row[0] if row and row[0] else 0
        self.conn.execute(
            "UPDATE sessions SET end_ts=?, best_lap_ms=? WHERE id=?",
            (time.time(), best_lap_ms or 0, session_id),
        )
        self.conn.commit()

    def delete_session(self, session_id: int) -> None:
        """永久删除会话：帧 + 圈记录 + 会话（不可恢复）。"""
        self.conn.execute("DELETE FROM frames WHERE session_id=?", (session_id,))
        self.conn.execute("DELETE FROM laps WHERE session_id=?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.conn.commit()

    def update_session_info(self, session_id: int, car: str = "", track: str = "") -> None:
        if car or track:
            self.conn.execute(
                "UPDATE sessions SET car=COALESCE(NULLIF(?, ''), car),"
                " track=COALESCE(NULLIF(?, ''), track) WHERE id=?",
                (car, track, session_id),
            )
            self.conn.commit()

    # ---------------- 自动清理（防数据库膨胀导致变慢） ----------------
    def total_frame_count(self) -> int:
        # 用 id 跨度 (MAX-MIN+1) 近似行数：走主键索引 O(1)，避免周期性全表扫几十万行。
        # 删除旧会话（低 id）后 MIN 上移 → 跨度自然变小，不会像 MAX(id) 那样永远超限。
        row = self.conn.execute("SELECT MAX(id), MIN(id) FROM frames").fetchone()
        if not row or row[0] is None:
            return 0
        return int(row[0] - row[1] + 1)

    def auto_prune(self, keep_sessions: int = 8, max_frames: int = 400000) -> int:
        """帧数超上限时删除最旧会话（保留最近 keep_sessions 个），返回删除的帧数。

        数据库无限膨胀 → 写入/checkpoint 变慢 → 程序卡顿。低频调用，删除后文件
        空间可复用（不 vacuum，避免运行中长时间锁库）。
        """
        if self.total_frame_count() <= max_frames:
            return 0
        rows = self.conn.execute(
            "SELECT id FROM sessions ORDER BY id DESC LIMIT -1 OFFSET ?",
            (keep_sessions,),
        ).fetchall()
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        marks = ",".join("?" * len(ids))
        deleted = self.conn.execute(
            f"DELETE FROM frames WHERE session_id IN ({marks})", ids
        ).rowcount
        self.conn.execute(f"DELETE FROM laps WHERE session_id IN ({marks})", ids)
        self.conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", ids)
        self.conn.commit()
        return int(deleted or 0)

    def checkpoint(self) -> None:
        """WAL 合并回主库并截断，控制 WAL 文件大小。"""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def list_sessions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT id, name, start_ts, end_ts, car, track, best_lap_ms FROM sessions"
            " ORDER BY id"
        ).fetchall()
        return [dict(zip(["id", "name", "start_ts", "end_ts", "car", "track", "best_lap_ms"], r))
                for r in rows]

    def get_session(self, session_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, name, start_ts, end_ts, car, track, best_lap_ms, channels_json"
            " FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return (dict(zip(["id", "name", "start_ts", "end_ts", "car", "track",
                          "best_lap_ms", "channels_json"], row))
                if row else None)

    # ---------------- laps ----------------
    def open_lap(self, session_id: int, lap_no: int) -> int:
        # 用 RETURNING 读新 ID：lastrowid 是连接级的，多线程下会被帧批量写入覆盖
        # （recorder 写 frames 的 executemany 会把连接 lastrowid 刷成帧 ID → 圈帧挂错）
        cur = self.conn.execute(
            "INSERT INTO laps (session_id, lap_no) VALUES (?,?) RETURNING id",
            (session_id, lap_no),
        )
        row = cur.fetchone()
        return int(row[0])

    def close_lap(self, lap_id: int, start_frame: int, end_frame: int,
                  total_ms: int, s1_ms: int, s2_ms: int, s3_ms: int,
                  top_speed: float, is_valid: bool, is_inlap: bool,
                  outlap: bool = False, tyre_compound: str = "") -> None:
        self.conn.execute(
            "UPDATE laps SET start_frame=?, end_frame=?, total_ms=?, s1_ms=?,"
            " s2_ms=?, s3_ms=?, top_speed_kmh=?, is_valid=?, is_inlap=?, outlap=?,"
            " tyre_compound=? WHERE id=?",
            (start_frame, end_frame, total_ms, s1_ms, s2_ms, s3_ms,
             top_speed, int(is_valid), int(is_inlap), int(outlap),
             (tyre_compound or "")[:40], lap_id),
        )
        self.conn.commit()

    def delete_lap(self, lap_id: int) -> None:
        """删除无效/误触的圈及其帧（彻底删除，不再解绑：解绑会导致帧永久孤儿、回放无数据）。"""
        self.conn.execute("DELETE FROM frames WHERE lap_id=?", (lap_id,))
        self.conn.execute("DELETE FROM laps WHERE id=?", (lap_id,))
        self.conn.commit()

    def list_laps(self, session_id: int) -> List[dict]:
        rows = self.conn.execute(
            "SELECT l.id, l.lap_no, l.start_frame, l.end_frame, l.total_ms, l.s1_ms,"
            " l.s2_ms, l.s3_ms, l.top_speed_kmh, l.is_valid, l.is_inlap, l.outlap, s.car"
            " FROM laps l JOIN sessions s ON s.id = l.session_id"
            " WHERE l.session_id=? ORDER BY l.lap_no", (session_id,)
        ).fetchall()
        cols = ["id", "lap_no", "start_frame", "end_frame", "total_ms", "s1_ms",
                "s2_ms", "s3_ms", "top_speed_kmh", "is_valid", "is_inlap", "outlap",
                "car"]
        return [dict(zip(cols, r)) for r in rows]

    def get_lap(self, lap_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, session_id, lap_no, total_ms, s1_ms, s2_ms, s3_ms,"
            " top_speed_kmh, is_valid, is_inlap, outlap, tyre_compound"
            " FROM laps WHERE id=?",
            (lap_id,),
        ).fetchone()
        cols = ["id", "session_id", "lap_no", "total_ms", "s1_ms", "s2_ms", "s3_ms",
                "top_speed_kmh", "is_valid", "is_inlap", "outlap", "tyre_compound"]
        return dict(zip(cols, row)) if row else None

    # ---------------- frames ----------------
    def insert_frames(self, rows: List[tuple]) -> None:
        """rows: (session_id, lap_id, t, dist, speed_kmh, gear, rpms, lap_ms, sector, payload)"""
        if not rows:
            return
        self.conn.executemany(_FRAME_INSERT, rows)
        self.conn.commit()

    def frame_count(self, session_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM frames WHERE session_id=?", (session_id,)).fetchone()
        return int(row[0]) if row else 0

    def frames_for_lap(self, session_id: int, lap_id: int) -> List[dict]:
        """取一圈的全部帧（含关键列 + payload 原样），按 id 升序。"""
        rows = self.conn.execute(
            "SELECT id, t, dist, speed_kmh, gear, rpms, lap_ms, sector, payload"
            " FROM frames WHERE session_id=? AND lap_id=? ORDER BY id",
            (session_id, lap_id),
        ).fetchall()
        cols = ["id", "t", "dist", "speed_kmh", "gear", "rpms", "lap_ms", "sector", "payload"]
        return [dict(zip(cols, r)) for r in rows]

    def frames_by_session(self, session_id: int, limit: int = 2_000_000) -> List[dict]:
        rows = self.conn.execute(
            "SELECT id, lap_id, t, dist, speed_kmh, gear, rpms, lap_ms, sector, payload"
            " FROM frames WHERE session_id=? ORDER BY id LIMIT ?",
            (session_id, limit),
        ).fetchall()
        cols = ["id", "lap_id", "t", "dist", "speed_kmh", "gear", "rpms", "lap_ms", "sector", "payload"]
        return [dict(zip(cols, r)) for r in rows]

    def max_frame_id(self, session_id: int) -> int:
        row = self.conn.execute(
            "SELECT MAX(id) FROM frames WHERE session_id=?", (session_id,)).fetchone()
        return int(row[0]) if row and row[0] else 0
