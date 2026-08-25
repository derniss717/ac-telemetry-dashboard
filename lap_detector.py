# -*- coding: utf-8 -*-
"""圈状态机：根据 graphic 包维护 圈号/圈时/扇区，并输出圈结束事件。

判定规则：
- 权威边界：completedLaps 增加 → 上一圈结束、新圈开始（圈时取 iLastTime）。
- 兜底边界：completedLaps 不变但 iCurrentTime 骤降（>1s）→ 视为重开一圈（如
  练习模式重跑），该圈标记 is_valid=0。
- 非 live 状态（回放/暂停/主菜单）：不归属任何圈（lap_no=-1），恢复 live 时
  从零重新跟踪，避免回放数据混入。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import ac_udp

AC_STATUS_LIVE = ac_udp.AC_STATUS_LIVE

# graphic 中 status 枚举: 0=off 1=replay 2=live 3=pause
MIN_LAP_MS = 15_000  # 低于 15s 的"圈"视为误触，丢弃


@dataclass
class LapEvent:
    """一圈结束事件。start/end 为帧 seq 边界（含），由 recorder 回填帧号。"""
    lap_no: int          # 圈号（1 起）
    total_ms: int        # 圈时
    s1_ms: int
    s2_ms: int
    s3_ms: int
    top_speed_kmh: float
    is_inlap: bool = False      # 进站圈：过线时在维修区
    outlap: bool = False        # 出场圈：圈开始时在维修区
    is_valid: bool = True
    start_seq: int = -1
    end_seq: int = -1


@dataclass
class LapState:
    """当前圈跟踪状态，供录制线程给每一帧打标签。"""
    lap_no: int = -1          # -1 = 不在 live 圈内（回放/暂停/菜单）
    lap_ms: int = 0
    sector: int = 0
    is_in_pit: bool = False
    live: bool = False        # AC 是否处于 live 状态
    last_lap_ms: int = 0
    best_lap_ms: int = 0
    completed_laps: int = 0


class LapDetector:
    def __init__(self) -> None:
        self._completed = -1
        self._last_icur = None  # type: Optional[int]
        self._last_sec = -1
        self._lap_sector_ms: Dict[int, int] = {}   # sector -> 用时
        self._last_sec_time = 0
        self._top_speed = 0.0
        self._inlap = False
        self._sector_reset = False
        self._just_closed_at = None  # 正常过线关闭圈的时刻（用于忽略归零误判）
        self._lap_start_in_pit = False  # 当前圈开始时是否在维修区（出场圈标志）
        self.state = LapState()
        self._prev_live = False

    def on_graphic(self, g: Dict, speed_kmh: float) -> Optional[LapEvent]:
        """每收到一个 graphic 包调用。返回圈结束事件（如有）。"""
        st = self.state
        status = g.get("status")
        live = (status == AC_STATUS_LIVE)
        was_live = st.live

        # 进入非 live（回放/暂停/菜单）→ 复位
        if not live:
            self._reset_tracking()
            st.live = False
            st.lap_no = -1
            self._prev_live = False
            return None

        # 从非 live 恢复 live → 全新开始
        if not was_live:
            self._reset_tracking()
            st.live = True
            st.completed_laps = int(g.get("completedLaps") or 0)
            self._completed = st.completed_laps
            st.lap_no = st.completed_laps + 1
            st.lap_ms = int(g.get("iCurrentTime") or 0)
            st.sector = int(g.get("currentSectorIndex") or 0)
            self._last_icur = st.lap_ms
            self._last_sec = st.sector
            self._inlap = bool(g.get("isInPit") or 0)
            self._lap_start_in_pit = bool(g.get("isInPit") or 0)
            self._prev_live = True
            return None

        completed = int(g.get("completedLaps") or 0)
        icur = int(g.get("iCurrentTime") or 0)
        sector = int(g.get("currentSectorIndex") or 0)
        is_in_pit = bool(g.get("isInPit") or 0)
        last_sec_time = int(g.get("lastSectorTime") or 0)
        last_lap = int(g.get("iLastTime") or 0)
        best_lap = int(g.get("iBestTime") or 0)
        st.lap_ms = icur
        st.sector = sector
        st.is_in_pit = is_in_pit
        st.last_lap_ms = last_lap
        st.best_lap_ms = best_lap
        st.completed_laps = completed

        # 悬架行程/速度 → 圈内最高速（用 physics 速度，由调用方传入）
        if speed_kmh > self._top_speed:
            self._top_speed = speed_kmh

        # ---- 权威边界：completedLaps 增加 ----
        if completed > self._completed:
            # 圈速以游戏显示为准：iLastTime 与过线前 iCurrentTime 取较大值
            # （部分 AC 版本 iLastTime 读数略小于游戏实际显示，过线前 icur 更接近游戏界面圈速）
            total_ms = last_lap if last_lap > 0 else icur
            if total_ms > 0 and self._last_icur and self._last_icur > total_ms:
                total_ms = self._last_icur
            # 扇区段用时：lastSectorTime 直接给出 S1/S2/S3（官方 split，游戏精确计时）
            s1_ms = self._lap_sector_ms.get(1, 0)
            s2_ms = self._lap_sector_ms.get(2, 0)
            s3_ms = self._lap_sector_ms.get(3, 0)
            # 兜底：某段缺失时用 icur 累计差值补（帧检测），保证三段覆盖全圈
            if s2_ms <= 0 and s1_ms > 0:
                s2_ms = max(0, total_ms - s1_ms)
            if s3_ms <= 0:
                s3_ms = max(0, total_ms - s1_ms - s2_ms)
            ev = LapEvent(
                lap_no=completed,           # 刚完成的圈号（1 起）
                total_ms=total_ms,
                s1_ms=s1_ms,
                s2_ms=s2_ms,
                s3_ms=s3_ms,
                top_speed_kmh=self._top_speed,
                is_inlap=bool(g.get("isInPit") or 0),   # 过线时在维修区 → 进站圈
                outlap=self._lap_start_in_pit,          # 圈开始时在维修区 → 出场圈
                is_valid=total_ms >= MIN_LAP_MS,
            )
            self._completed = completed
            self._top_speed = 0.0
            self._inlap = bool(g.get("isInPit") or 0)
            self._lap_start_in_pit = bool(g.get("isInPit") or 0)
            self._lap_sector_ms = {}
            self._just_closed_at = time.monotonic()   # 标记刚过线，归零不算重开
            st.lap_no = completed + 1
            st.lap_ms = icur
            self._last_icur = icur
            self._last_sec = sector
            self._prev_live = True
            return ev

        # ---- 兜底边界：iCurrentTime 骤降（重开一圈）----
        if self._last_icur is not None and icur < self._last_icur - 1000:
            # 刚正常过线（<5s）后 icur 归零是正常现象，不是重开，跳过避免幽灵圈
            if self._just_closed_at is not None and (time.monotonic() - self._just_closed_at) < 5.0:
                self._just_closed_at = None
                self._last_icur = icur
                self._last_sec = sector
                self._prev_live = True
                return None
            elapsed = max(0, self._last_icur)
            s1_ms = self._lap_sector_ms.get(1, 0)
            s2_ms = self._lap_sector_ms.get(2, 0)
            s3_ms = self._lap_sector_ms.get(3, 0)
            if s2_ms <= 0 and s1_ms > 0:
                s2_ms = max(0, elapsed - s1_ms)
            if s3_ms <= 0:
                s3_ms = max(0, elapsed - s1_ms - s2_ms)
            ev = LapEvent(
                lap_no=st.lap_no,
                total_ms=elapsed,           # 重开前的部分用时
                s1_ms=s1_ms,
                s2_ms=s2_ms,
                s3_ms=s3_ms,
                top_speed_kmh=self._top_speed,
                is_inlap=bool(g.get("isInPit") or 0),
                outlap=self._lap_start_in_pit,
                # 已跑够 15s 的重开圈视为有效（保留数据）；极短误触才判无效
                is_valid=elapsed >= MIN_LAP_MS,
            )
            self._top_speed = 0.0
            self._lap_sector_ms = {}
            self._inlap = bool(g.get("isInPit") or 0)
            self._lap_start_in_pit = bool(g.get("isInPit") or 0)
            st.lap_no = st.lap_no + 1
            st.lap_ms = icur
            self._last_icur = icur
            self._last_sec = sector
            self._prev_live = True
            return ev

        # ---- 扇区时间捕获 ----
        # AC 的 sector 是 0→1→2→3→0 循环（实测含 sector 3）。
        # 用 iCurrentTime（当前圈累计时间）记录各扇区线时间，权威边界时换算段用时：
        #   0→1 跨 S1 线: 记累计 icur
        #   1→2 跨 S2 线: 记累计 icur
        #   2→3 / 3→0: 后续段并入 S3（段用时 = total - S2 线累计）
        if sector != self._last_sec:
            # lastSectorTime = 刚完成扇区的段用时（官方 split，游戏精确计时）：
            #   0→1 跨 S1 线 → S1 段；1→2 → S2 段；2→3/2→0 → S3 段
            if sector == 1 and self._last_sec == 0:
                self._lap_sector_ms[1] = last_sec_time
            elif sector == 2 and self._last_sec == 1:
                self._lap_sector_ms[2] = last_sec_time
            elif (sector == 3 and self._last_sec == 2) or (sector == 0 and self._last_sec == 2):
                self._lap_sector_ms[3] = last_sec_time
            self._last_sec = sector

        self._last_icur = icur
        self._prev_live = True
        return None

    def _reset_tracking(self) -> None:
        self._completed = -1
        self._last_icur = None
        self._last_sec = -1
        self._lap_sector_ms = {}
        self._top_speed = 0.0
        self._inlap = False
        self._just_closed_at = None
        self._lap_start_in_pit = False
