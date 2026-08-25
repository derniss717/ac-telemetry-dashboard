# -*- coding: utf-8 -*-
"""线程安全的环形缓冲：UDP 接收线程写入，录制线程/仪表盘读取。"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from typing import Any, Deque, List, Optional


class RingBuffer:
    def __init__(self, maxlen: int = 8000):
        self._buf: Deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._maxlen = maxlen

    def push(self, frame: dict) -> int:
        """写入一帧（自动分配 seq），返回 seq。"""
        with self._lock:
            seq = next(self._seq)
            frame["seq"] = seq
            self._buf.append(frame)
            return seq

    def drain(self, after_seq: int = 0) -> List[dict]:
        """取出 seq > after_seq 的全部帧（递增顺序，最大为缓冲容量）。"""
        with self._lock:
            if not self._buf:
                return []
            oldest = self._buf[0]["seq"]
            if after_seq < oldest - 1:  # 已超出窗口，给调用方发快照的暗示
                return []
            out = [f for f in self._buf if f["seq"] > after_seq]
            return out

    def latest_seq(self) -> int:
        with self._lock:
            return self._buf[-1]["seq"] if self._buf else 0

    def oldest_seq(self) -> int:
        with self._lock:
            return self._buf[0]["seq"] if self._buf else 0

    def latest(self) -> Optional[dict]:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def snapshot(self, max_frames: int = 2000) -> List[dict]:
        """取最近 max_frames 帧（快照用）。"""
        with self._lock:
            if not self._buf:
                return []
            return list(self._buf)[-max_frames:]
