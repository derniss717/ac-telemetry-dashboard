# -*- coding: utf-8 -*-
"""会话/圈生命周期管理 + 录制线程。

接收线程（UdpReceiver 回调）内完成：
  graphic  → 圈检测（LapDetector）→ 圈结束事件 → 落库 lap 元数据
  physics  → 打上当前圈上下文 → 写入环形缓冲
  static   → 更新会话的车型/赛道信息
录制线程（RecorderThread）内完成：从环形缓冲批量写 frames。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Set

import numpy as np

import ac_udp
from lap_detector import LapDetector, LapEvent
from ring_buffer import RingBuffer
from storage import Storage

GAP_CLOSE_SECONDS = 2.0  # 收包空洞超过该时长 → 结束当前会话段


class SessionManager:
    """会话/圈状态的管理者（在接收线程中被调用，不并发）。"""

    def __init__(self, storage: Storage, ring: RingBuffer):
        self.storage = storage
        self.ring = ring
        self.detector = LapDetector()
        self._lock = threading.RLock()  # check_gap/finalize 会嵌套调用，需可重入
        self.session_id: Optional[int] = None
        self.session_name = ""
        self.car = ""
        self.track = ""
        self.cur_lap: Optional[dict] = None
        self._skip_laps: Set[int] = set()
        self._last_speed = 0.0
        self._top_speed = 0.0
        self._last_dist = 0.0
        self._static_done = False
        self.tyre_compound = ""    # 轮胎配方（graphic 字段，如 Supersoft/Hard；UDP 模式拿不到）
        self.last_packet_t = 0.0
        self.laps_done = 0
        self._last_ext: Optional[Dict] = None   # 最近一帧 UDP 扩展包（世界坐标/朝向/速度矢量），共享内存模式由混合监听填充

    # ---------------- 包入口（接收线程） ----------------
    def on_packet(self, kind: str, fields: Dict) -> None:
        with self._lock:
            self._on_packet_locked(kind, fields)

    def _on_packet_locked(self, kind: str, fields: Dict) -> None:
        self.last_packet_t = time.time()
        if kind == "static":
            self._on_static(fields)
        elif kind == "graphic":
            self._on_graphic(fields)
        elif kind == "physics":
            self._on_physics(fields)
        elif kind == "extended":
            # UDP 扩展包（含世界坐标/朝向/速度矢量）：缓存最新值，供后续 physics 帧携带
            self._last_ext = fields

    def _on_static(self, f: Dict) -> None:
        car = f.get("carModel") or ""
        track = f.get("track") or ""
        if car or track:
            self.car = car
            self.track = track
            if self.session_id is not None:
                self.storage.update_session_info(self.session_id, car, track)

    def _ensure_session(self) -> None:
        if self.session_id is not None:
            # 健壮性：会话行可能被外部删除（reset_all_data / 清理旧会话），
            # 此时写帧/圈会变成孤儿数据（sessions 无行但 laps/frames 有）。
            # 检测到则重建会话，避免数据不可见。
            if self.storage.get_session(self.session_id) is None:
                print(f"[recorder] 会话 #{self.session_id} 已不存在（可能被清理），重建新会话")
                self.session_id = None
        if self.session_id is None:
            self.session_name = f"AC_{time.strftime('%Y%m%d_%H%M%S')}"
            self.session_id = self.storage.open_session(
                self.session_name,
                channels_json=json.dumps(ac_udp.CHANNELS, ensure_ascii=False),
                car=self.car, track=self.track,
            )
            print(f"[recorder] 新会话 #{self.session_id} {self.session_name}"
                  + (f"  {self.car} @ {self.track}" if self.car else ""))

    def _on_graphic(self, f: Dict) -> None:
        if self.session_id is None:
            # 会话开始条件：收到 live 状态的 graphic 包
            if f.get("status") != ac_udp.AC_STATUS_LIVE:
                return
            self._ensure_session()
        comp = f.get("tyreCompound")
        if comp:
            self.tyre_compound = str(comp).strip()
        # distanceTraveled 是 graphic 结构的字段（physics 里没有），缓存供 physics 帧用
        d = f.get("distanceTraveled")
        if d is not None:
            self._last_dist = float(d)
        ev = self.detector.on_graphic(f, self._last_speed)
        if ev is not None:
            self._close_lap(ev)

    def _on_physics(self, f: Dict) -> None:
        speed = float(f.get("speedKmh") or 0.0)
        self._last_speed = speed
        if speed > self._top_speed:
            self._top_speed = speed
        st = self.detector.state
        if not st.live or st.lap_no < 1:
            return  # 非 live（回放/暂停/菜单）不录帧
        if self.session_id is None:
            self._ensure_session()
        # 圈切换：开新圈
        if self.cur_lap is None or st.lap_no != self.cur_lap["lap_no"]:
            self._open_lap(st.lap_no)
        frame = {
            "t": time.time(),
            "lap_no": st.lap_no,
            "lap_id": self.cur_lap["lap_id"],
            "lap_ms": st.lap_ms,
            "sector": st.sector,
            "in_pit": st.is_in_pit,
            "speed_kmh": self._last_speed,
            "gear": int(f.get("gear") or 0),
            "rpms": int(f.get("rpms") or 0),
            "dist": self._last_dist,
            "channels": ac_udp.channel_array(f),
        }
        # UDP 扩展数据（世界坐标/朝向/速度矢量）：与 physics 帧合并，实时卡片用；回放帧无此列
        ext = self._last_ext
        if ext:
            frame["pos"] = list(ext.get("worldPosition") or ())
            frame["orient"] = list(ext.get("orientation") or ())
            frame["vel"] = list(ext.get("velocity") or ())
        self.ring.push(frame)

    # ---------------- 圈 ---------------- 
    def _open_lap(self, lap_no: int) -> None:
        if self.session_id is None:
            return
        lap_id = self.storage.open_lap(self.session_id, lap_no)
        self.cur_lap = {"lap_id": lap_id, "lap_no": lap_no, "start_seq": self.ring.latest_seq()}

    def _close_lap(self, ev: LapEvent) -> None:
        if self.cur_lap is None:
            return
        start_seq = self.cur_lap["start_seq"]
        end_seq = self.ring.latest_seq()  # 圈结束瞬间的最新帧 seq
        lap_id = self.cur_lap["lap_id"]
        # 幽灵圈剔除：正常过线/重开误判产生的 3 帧空圈（total 却是上一段用时）。
        # 帧数 < 50 说明该"圈"实际没有驾驶数据，物理删除（帧+记录）。
        frame_cnt = end_seq - start_seq + 1
        if frame_cnt < 50:
            self.storage.delete_lap(lap_id)
            print(f"[recorder] 幽灵圈剔除（{frame_cnt}帧）")
            self.cur_lap = None
            return
        self.storage.close_lap(
            lap_id, start_seq, end_seq, ev.total_ms, ev.s1_ms, ev.s2_ms, ev.s3_ms,
            ev.top_speed_kmh, ev.is_valid, ev.is_inlap, ev.outlap,
            self.tyre_compound,   # 注意：SessionManager 的属性（不是 self.mgr）
        )
        self.laps_done += 1
        if not ev.is_valid:
            # 极短误触圈（<15s）：帧保留、记录保留，仅标记无效——不删除，回放仍可见
            print(f"[recorder] 圈 {ev.lap_no} 判定无效（{ev.total_ms}ms），已标记（保留数据）")
        else:
            print(f"[recorder] 圈 {ev.lap_no} 完成: {ev.total_ms/1000:.1f}s"
                  f"  (S1 {ev.s1_ms/1000:.1f} S2 {ev.s2_ms/1000:.1f} S3 {ev.s3_ms/1000:.1f})")
        self.cur_lap = None

    # ---------------- 收包空洞 / 收尾 ----------------
    def check_gap(self, now: float) -> None:
        with self._lock:
            if self.session_id is not None and now - self.last_packet_t > GAP_CLOSE_SECONDS:
                print("[recorder] 收包空洞 >2s（AC 退出/回到菜单/重开），结束当前会话段")
                self.finalize()

    def finalize(self) -> None:
        with self._lock:
            self._finalize_locked()

    def _finalize_locked(self) -> None:
        if self.session_id is None:
            return
        if self.cur_lap is not None:
            # 未触发圈事件直接结束：按最后状态补一条（可能不完整）
            total = self.detector.state.lap_ms or 0
            sec = self.detector._lap_sector_ms
            self.storage.close_lap(
                self.cur_lap["lap_id"], self.cur_lap["start_seq"],
                self.ring.latest_seq(), total, sec.get(1, 0), sec.get(2, 0),
                sec.get(3, total), self._top_speed,
                total >= 15000, False,
                tyre_compound=self.tyre_compound,
            )
            self.cur_lap = None
        self.storage.close_session(self.session_id)
        print(f"[recorder] 会话 #{self.session_id} 已保存")
        self.session_id = None
        self.session_name = ""
        self.detector._reset_tracking()
        self._skip_laps.clear()


class RecorderThread(threading.Thread):
    """把环形缓冲中的帧批量写入 SQLite。"""

    def __init__(self, storage: Storage, ring: RingBuffer, mgr: SessionManager,
                 stop_evt: threading.Event):
        super().__init__(name="recorder", daemon=True)
        self.storage = storage
        self.ring = ring
        self.mgr = mgr
        self.stop_evt = stop_evt
        self._last_seq = 0
        self._prune_ticks = 0   # 自动清理计数器（约每 50 秒检查一次）

    def run(self) -> None:
        """批量落库主循环。

        坑：ring.drain 不消费帧（dashboard 实时拉取也依赖它），且缓冲溢出时
        drain 返回空但不前进——若 _last_seq 落后过多会永久空转，后续帧全部丢失。
        修复：① 限量消费（最多 2000 帧/轮）防单次写库过久导致积压溢出；
             ② drain 空但缓冲有新帧时跳进（丢中间帧，避免卡死）；
             ③ 写库异常不静默死线程。
        """
        while not self.stop_evt.is_set():
            frames = self.ring.drain(self._last_seq)
            if not frames:
                # 缓冲溢出（_last_seq 落后窗口）：跳进到最新，丢弃不可恢复的中间帧
                newest = self.ring.latest_seq()
                if newest > self._last_seq:
                    print(f"[recorder] 帧缓冲溢出，跳过积压 {(newest - self._last_seq)} 帧")
                    self._last_seq = newest - 1
                time.sleep(0.1)
                continue
            take = frames[:2000]
            self._last_seq = take[-1]["seq"]   # 只前进到已消费的帧，其余留待下轮
            rows = []
            sid = self.mgr.session_id
            for fr in take:
                if sid is None or fr.get("lap_id") is None:
                    continue
                payload = np.asarray(fr["channels"], dtype=np.float32).tobytes()
                p = fr.get("pos") or (None, None, None)
                rows.append((sid, fr["lap_id"], fr["t"], fr["dist"], fr["speed_kmh"],
                             fr["gear"], fr["rpms"], fr["lap_ms"], fr["sector"],
                             p[0], p[1], p[2], payload))
            if rows:
                try:
                    self.storage.insert_frames(rows)
                except Exception as exc:
                    print(f"[recorder] 写库失败（跳过本轮）: {exc}")
            # 定时自动清理：约每 500 轮（≈50 秒）检查一次，超上限删最旧会话并合并 WAL
            self._prune_ticks += 1
            if self._prune_ticks >= 500:
                self._prune_ticks = 0
                try:
                    d = self.storage.auto_prune()
                    if d:
                        print(f"[recorder] 自动清理旧数据: 删除 {d} 帧（保留最近 8 个会话）")
                        self.storage.checkpoint()   # 只有真删了数据才 checkpoint，避免无谓 WAL 合并
                except Exception as exc:
                    print(f"[recorder] 自动清理失败: {exc}")
