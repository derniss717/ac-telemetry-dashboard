# -*- coding: utf-8 -*-
"""Assetto Corsa 共享内存读取器（Windows 命名共享内存，无需 UDP 广播）。

游戏始终把实时数据写入命名共享内存（Local\\acpmf_physics / graphics / static），
字段与 UDP 包完全一致。读取端自行按频率采样（physics ~200Hz、graphic ~60Hz），
解析复用 ac_udp 的偏移表，通过 on_packet(kind, fields) 回调对外输出，
与 UDP 接收器接口一致，可无缝替换。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, Optional

import ac_udp

FILE_MAP_READ = 0x0004
# physics 优先映射大版本以读取扩展字段（刹车盘温/胎温层/ERS 等），失败逐级回退
SIZES = {"physics": (668, 400, 288), "graphic": (282,), "static": (482,)}
NAMES = {"physics": r"Local\acpmf_physics", "graphic": r"Local\acpmf_graphics",
         "static": r"Local\acpmf_static"}


class SharedMemReader:
    def __init__(self, on_packet: Callable[[str, Dict[str, object]], None],
                 physics_hz: float = 200, graphic_hz: float = 60):
        self.on_packet = on_packet
        self.physics_dt = 1.0 / physics_hz
        self.graphic_dt = 1.0 / graphic_hz
        self._stop = threading.Event()
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.OpenFileMappingW.restype = wintypes.HANDLE
        self._k32.MapViewOfFile.restype = wintypes.LPVOID
        self._handles: Dict[str, object] = {}
        self._views: Dict[str, int] = {}

    @staticmethod
    def available() -> bool:
        """共享内存是否存在（游戏在跑且写入了遥测）。"""
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenFileMappingW.restype = wintypes.HANDLE
            for name in NAMES.values():
                h = k.OpenFileMappingW(FILE_MAP_READ, False, name)
                if not h:
                    return False
                k.CloseHandle(h)
            return True
        except Exception:
            return False

    def start(self) -> None:
        self._sizes: Dict[str, int] = {}
        for kind, name in NAMES.items():
            h = self._k32.OpenFileMappingW(FILE_MAP_READ, False, name)
            if not h:
                raise RuntimeError(f"无法打开共享内存 {name}（游戏是否在运行？）")
            self._handles[kind] = h
            view = 0
            size = 0
            for try_size in SIZES[kind]:      # 大版本优先（扩展字段）
                view = self._k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, try_size)
                if view:
                    size = try_size
                    break
            if not view:
                raise RuntimeError(f"映射 {name} 失败")
            self._views[kind] = view
            self._sizes[kind] = size
        print(f"[shared_mem] 共享内存已连接: physics={self._sizes.get('physics')}B"
              f" graphic={self._sizes.get('graphic')}B static={self._sizes.get('static')}B")
        self._thread = threading.Thread(target=self._loop, name="shared-mem",
                                        daemon=True)
        self._thread.start()

    def _read(self, kind: str) -> Optional[bytes]:
        view = self._views.get(kind)
        if not view:
            return None
        try:
            return ctypes.string_at(view, self._sizes.get(kind, SIZES[kind][-1]))
        except Exception:
            return None

    def _loop(self) -> None:
        last_p = last_g = 0.0
        static_sent = False
        warn = set()
        while not self._stop.is_set():
            now = time.time()
            if now - last_p >= self.physics_dt:
                last_p = now
                data = self._read("physics")
                if data:
                    try:
                        _, fields = ac_udp.parse_packet(data)
                        if fields:
                            self.on_packet("physics", fields)
                    except Exception as exc:
                        if "phys" not in warn:
                            warn.add("phys")
                            print(f"[shared_mem] physics 解析失败: {exc}")
            if now - last_g >= self.graphic_dt:
                last_g = now
                data = self._read("graphic")
                if data:
                    try:
                        _, fields = ac_udp.parse_packet(data)
                        if fields:
                            self.on_packet("graphic", fields)
                    except Exception as exc:
                        if "graph" not in warn:
                            warn.add("graph")
                            print(f"[shared_mem] graphic 解析失败: {exc}")
            if not static_sent:
                data = self._read("static")
                if data:
                    try:
                        _, fields = ac_udp.parse_packet(data)
                        if fields and fields.get("carModel"):
                            static_sent = True
                            self.on_packet("static", fields)
                    except Exception:
                        pass
            time.sleep(0.001)

    def stop(self) -> None:
        self._stop.set()
        for kind, view in self._views.items():
            if view:
                try:
                    self._k32.UnmapViewOfFile(view)
                except Exception:
                    pass
        for h in self._handles.values():
            try:
                self._k32.CloseHandle(h)
            except Exception:
                pass
