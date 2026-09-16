# -*- coding: utf-8 -*-
"""实时仪表盘：本地 HTTP 服务 + JSON 轮询接口。

接口：
  GET /                  → web/index.html
  GET /api/live?after=N  → 增量帧 + 当前状态（after 超出窗口返回 full 快照）
  GET /api/laps          → 最近 10 圈概览
  GET /api/session       → 当前会话信息
  GET /static/...        → web/ 静态资源
"""
from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import config as cfg
from ring_buffer import RingBuffer
from storage import Storage

# 静态前端：优先新版 build 产物，顺序为 ①开发目录 web-ui/dist ②打包内 web/dist
# ③旧版 web/ 兜底。
# 注意 onefile 打包后 __file__ 指向 _MEIPASS，所以打包产物路径要按 resource_dir() 找。
_DEV_UI_DIST = Path(__file__).resolve().parent / "web-ui" / "dist"
_PKG_UI_DIST = cfg.resource_dir() / "web" / "dist"
if _DEV_UI_DIST.is_dir():
    WEB_DIR = _DEV_UI_DIST
elif _PKG_UI_DIST.is_dir():
    WEB_DIR = _PKG_UI_DIST
else:
    WEB_DIR = cfg.resource_dir() / "web"
MAX_SNAPSHOT_FRAMES = 3000  # 首次加载/重置时最多下发的帧数

# 赛道弯角数据（唯一数据源，报告/轨迹图共用；位置为估算代表点 km）
TRACK_TURNS_DATA: dict = {
        "suzuka": [
            (1, "First", 0.95), (2, "", 1.08), (3, "", 1.20),
            (4, "S Curves", 1.33), (5, "S Curves", 1.45), (6, "S Curves", 1.57),
            (7, "Dunlop", 1.75), (8, "Degner 1", 1.95), (9, "Degner 2", 2.12),
            (10, "", 2.32), (11, "Hairpin", 2.52), (12, "", 2.78),
            (13, "Spoon", 3.08), (14, "Spoon", 3.32),
            (15, "130R", 4.30), (16, "Casio", 4.62), (17, "Casio", 4.78),
            (18, "Casio", 4.95),
        ],
        "redbullring": [   # 红牛环：4.326km / 10 弯，官方弯名（红牛环官网 2024），位置为估算代表点(km)
            (1, "Lauda", 0.25), (2, "Münzer", 0.55),    # Niki Lauda / Münzer 弯
            (3, "", 1.10), (4, "Rauch", 1.45),          # T3 重刹区 / Rauch 弯
            (5, "", 2.05), (6, "", 2.30),               # T5 / T6 盲弯下坡
            (7, "Graz", 2.55), (8, "", 2.95),           # Graz 弯 / T8
            (9, "Rindt", 3.40), (10, "", 3.95),         # Jochen Rindt 弯 / T10 接大直道
        ],
        "barcelona": [
            (1, "Elf", 1.05), (2, "", 1.20), (3, "Renault", 1.45),
            (4, "Repsol", 2.00), (5, "Seat", 2.20), (6, "", 2.40),
            (7, "", 2.60), (8, "", 2.75), (9, "Campsa", 3.05),
            (10, "La Caixa", 4.05), (11, "", 4.18), (12, "", 4.30),
            (13, "", 4.45), (14, "New Holland", 4.58),
        ],
        "hungaroring": [
            (1, "Piquet", 0.62), (2, "Hamilton", 0.85), (3, "Spring", 1.15),
            (4, "Mansell", 1.35), (5, "Mogyoród", 1.80),
            (6, "Driving Center", 2.10), (7, "Driving Center", 2.22),
            (8, "Buda", 2.40), (9, "Pest", 2.52),
            (10, "Danube", 2.62), (11, "Alesi", 2.72),
            (12, "Schumacher", 3.00), (13, "Senna", 3.55), (14, "Szisz", 4.10),
        ],
        "gilles_villeneuve": [   # 加拿大蒙特利尔：4.361km / 14 弯，2024 官方命名，位置为估算代表点(km)
            (1, "Senna", 0.25), (2, "Senna", 0.40),       # 塞纳 S 弯（T1 左 + T2 右发卡）
            (3, "", 0.90), (4, "", 1.00),                  # T3/4 右-左减速弯
            (5, "", 1.40),                                 # T5 高速右 kink
            (6, "", 1.85), (7, "", 2.00),                  # T6/7 左-右减速弯（接后直道）
            (8, "", 2.45), (9, "", 2.60),                  # T8/9 右-左减速弯
            (10, "L'Epingle", 3.05), (11, "", 3.40),       # T10 发卡弯（赌场发卡）+ T11 kink
            (12, "", 3.70),                                # T12 右 kink（接最后直道）
            (13, "Wall of Champions", 4.05), (14, "Wall of Champions", 4.20),  # T13/14 最终减速弯+冠军墙
        ],
        "baku": [   # 巴库城市赛道：6.003km / 20 弯，逆时针，2024 官方命名，位置为估算代表点(km)
            (1, "Azadliq", 1.40),    # T1 自由广场 90° 左弯（2.2km 大直道末端）
            (2, "", 1.65), (3, "", 1.90), (4, "", 2.10), (5, "", 2.35),   # 海滨区
            (6, "", 2.55), (7, "", 2.70),                                # T6/7 左-右减速弯
            (8, "Castle", 2.95), (9, "Castle", 3.10), (10, "Castle", 3.20),   # 城堡区
            (11, "Castle", 3.30), (12, "Castle", 3.45),                  # 最窄 7.6m，零容错
            (13, "", 3.70), (14, "", 3.90), (15, "", 4.10),              # T15 下坡陷阱弯
            (16, "", 4.35),                                              # T16 出口接 2.2km 大直道
            (17, "", 4.60), (18, "", 4.70), (19, "", 4.90), (20, "", 5.10),
        ],
        "cota": [   # 美洲赛道 COTA：5.513km / 20 弯，逆时针，41m 落差，位置为估算代表点(km)
            (1, "Big Red", 0.30),        # T1 上坡左发卡（11% 坡度，盲弯，主超车点）
            (2, "", 0.55),               # T2 全油右微弯
            (3, "Esses", 0.80), (4, "Esses", 0.95), (5, "Esses", 1.10), (6, "Esses", 1.25),  # 复刻银石 Maggotts-Becketts
            (7, "", 1.55), (8, "", 1.75), (9, "", 1.95),   # 上坡复合弯
            (10, "", 2.25),              # T10 盲左全油
            (11, "Bobby Pin", 2.50),     # T11 180° 左发卡，接 1.2km 最长直道
            (12, "", 3.70),              # T12 直道末端重刹左弯（超车点）
            (13, "Stadium", 3.95), (14, "Stadium", 4.15), (15, "Stadium", 4.35),  # 体育场段（复刻霍根海姆）
            (16, "", 4.65), (17, "", 4.80), (18, "", 4.95),  # 三顶点右弯（复刻伊斯坦布尔 T8）
            (19, "", 5.20), (20, "Andretti", 5.35),   # T20 终弯回大直道
        ],
        "silverstone": [   # 银石：5.891km / 18 弯，顺时针，官方弯名，位置为估算代表点(km)
            (1, "Abbey", 0.15), (2, "Farm", 0.30),
            (3, "Village", 0.55), (4, "The Loop", 0.75), (5, "Aintree", 1.05),
            (6, "Brooklands", 1.60), (7, "Luffield", 1.85), (8, "Woodcote", 2.15),
            (9, "Copse", 2.60),                                    # 著名高速右弯
            (10, "Maggotts", 2.85), (11, "Maggotts", 3.00),
            (12, "Becketts", 3.20), (13, "Becketts", 3.40), (14, "Chapel", 3.65),   # 高速 S 序列
            (15, "Stowe", 4.40), (16, "Vale", 4.80),
            (17, "Club", 5.10), (18, "Club", 5.30),
        ],
        "monza": [   # 蒙扎：5.793km / 11 弯（7右4左），官方弯名，位置为估算代表点(km)
            (1, "Rettifilo", 0.35), (2, "Rettifilo", 0.50),   # Variante del Rettifilo 第一减速弯（重刹）
            (3, "Curva Grande", 0.90),                          # 全油门右弯
            (4, "Roggia", 1.50), (5, "Roggia", 1.65),           # Variante della Roggia 减速弯
            (6, "Lesmo 1", 2.10), (7, "Lesmo 2", 2.30),         # 中速右弯对
            (8, "Serraglio", 2.95),                             # 快速左弯（DRS 区）
            (9, "Ascari", 3.60), (10, "Ascari", 3.75), (11, "Ascari", 3.90),   # Variante Ascari 左-右-左
            (12, "Parabolica", 4.80),                           # 长右弯，官方又称 Curva Alboreto
        ],
        "zandvoort": [   # 赞德沃特：4.259km / 14 弯，顺时针，官方弯名，位置为估算代表点(km)
            (1, "Tarzan", 0.15), (2, "Gerlach", 0.32),       # T1 发夹弯（主直道后）/ T2
            (3, "Hugenholtz", 0.55), (4, "Hunzerug", 0.85),  # T3 倾斜弯 19° / T4
            (5, "", 1.10), (6, "Slotemaker", 1.35),          # T5 无名 / T6
            (7, "Scheivlak", 1.60), (8, "Masters", 1.85),    # T7 著名高速弯 / T8
            (9, "", 2.10), (10, "", 2.35),                   # T9/T10 无名
            (11, "Hans Ernst", 2.60), (12, "", 2.85), (13, "", 3.10),  # T11 减速弯 / T12/T13 无名
            (14, "Arie Luyendyk", 3.30),                     # T14 倾斜弯 32%，接主直道
        ],
}


# 已知目视验证的轨迹图对齐参数（theta/flipX/flipZ），按 track 名索引。
# score 评价对 mod 赛道（赞德沃特等）不可靠（赛道像素稀疏 + 边界模糊），
# 需目视验证后 hardcode。验证要点：必须用**单圈完整数据**（lap.frames），
# 不能用跨多 session 的混合数据（含 pit 进出 + 多圈叠加，视觉上看着像赛道实则噪声）。
# 验证方式：tools/align_vis2.py 单圈 pts + 各 theta/flip 画到赛道图上，肉眼挑最贴合的。
SEED_TRACK_ALIGN = {
    "zandvoort2020": {
        "theta": 0.0, "flipX": False, "flipZ": False,
        "score": 0.756, "size": [256, 256],
        "note": "manually verified via lap10 (完整一圈 72s); 切勿用跨 session 混合数据验证",
    },
}


def match_track_key(track: str) -> str:
    """赛道名 → track_key（与 _laphtml 内匹配逻辑一致）。"""
    _tk = (track or "").lower()
    if "suzuka" in _tk:
        return "suzuka"
    if "hungar" in _tk:
        return "hungaroring"
    if "barcelona" in _tk or "catalunya" in _tk:
        return "barcelona"
    if "red_bull" in _tk or "redbull" in _tk or "spielberg" in _tk or "osterreich" in _tk or "austria" in _tk:
        return "redbullring"
    if "gilles" in _tk or "villeneuve" in _tk or "montreal" in _tk or "canada" in _tk:
        return "gilles_villeneuve"
    if "baku" in _tk or "azerbaijan" in _tk:
        return "baku"
    if "cota" in _tk or "austin" in _tk or "americas" in _tk:
        return "cota"
    if "silver" in _tk:
        return "silverstone"
    if "monza" in _tk:
        return "monza"
    if "zandvoort" in _tk:
        return "zandvoort"
    return ""


class Dashboard:
    def __init__(self, ring: RingBuffer, storage: Storage, mgr,
                 http_port: int = 8080):
        self.ring = ring
        self.storage = storage
        self.mgr = mgr
        self.http_port = http_port
        self._server: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        handler = self._make_handler()
        server = ThreadingHTTPServer
        server.allow_reuse_address = False  # 防止第二个实例抢 8080
        # 绑 0.0.0.0（重构 #4）：局域网内手机/平板可访问（移动端联动基础），本地仍走 127.0.0.1
        self._server = ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        t = threading.Thread(target=self._server.serve_forever,
                             name="http", daemon=True)
        t.start()
        print(f"[dashboard] 实时仪表盘: http://127.0.0.1:{self.http_port}（局域网: http://<本机IP>:{self.http_port}）")

    def start_ws(self, ws_port: int = 8081) -> None:
        """WebSocket 实时推送：每 200ms 向所有客户端推 /api/live 同格式增量帧。

        前端优先走 WS（丝滑、省轮询），断开自动回退 HTTP 轮询。
        支持优雅停止（stop_ws）：GUI 停止→再开始时旧线程必须让出端口，
        否则 8081 被旧实例占用，新实例启动报错、旧线程还在推过期数据。
        """
        def _run():
            import asyncio
            try:
                import websockets
            except ImportError:
                print("[dashboard] WebSocket 不可用：缺 websockets 模块，前端将回退 HTTP 轮询")
                return
            clients = {}   # ws -> after_seq
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._ws_loop = loop
            self._ws_stop = None
            server = None

            async def push_one(ws, after):
                # 每客户端独立推送：单个慢客户端（手机 Wi-Fi 卡顿）不阻塞其他端
                try:
                    payload = self._live_payload(after)
                    clients[ws] = payload.get("latest_seq", after)
                    await ws.send(json.dumps(payload, ensure_ascii=False))
                except Exception:
                    clients.pop(ws, None)

            async def broadcaster():
                while True:
                    await asyncio.sleep(0.2)
                    if clients:
                        await asyncio.gather(
                            *(push_one(ws, after) for ws, after in list(clients.items())))

            async def handler(ws):
                clients[ws] = -1   # -1 → full 快照（首帧全量）
                try:
                    await ws.wait_closed()
                finally:
                    clients.pop(ws, None)

            async def main():
                nonlocal server
                server = await websockets.serve(handler, "0.0.0.0", ws_port)
                stop = asyncio.Event()
                self._ws_stop = stop
                bc = asyncio.create_task(broadcaster())
                await stop.wait()
                # 关停必须走完整流程（否则 8081 没真正释放，GUI 停止→再开始会端口占用）：
                # ① 停 broadcaster 并等它退出 ② 关监听 ③ 关已连客户端 ④ 等内部 _close 任务收尾
                bc.cancel()
                try:
                    await bc
                except BaseException:
                    pass
                server.close()
                for ws in list(clients):
                    try:
                        await ws.close()
                    except Exception:
                        pass
                try:
                    await server.wait_closed()
                except Exception:
                    pass

            try:
                loop.run_until_complete(main())
            except Exception:
                pass   # stop_ws 触发的正常中断（Event loop stopped before Future completed）
            finally:
                clients.clear()
                try:
                    loop.close()
                except Exception:
                    pass

        t = threading.Thread(target=_run, name="ws", daemon=True)
        self._ws_thread = t
        t.start()
        print(f"[dashboard] WebSocket 实时推送: ws://127.0.0.1:{ws_port}（局域网: ws://<本机IP>:{ws_port}）")

    def stop_ws(self, timeout: float = 3.0) -> None:
        """通知 WS 线程退出并等它真正结束（释放 8081）。

        不等线程收尾就返回的话，紧接着重新开始录制会因 8081 仍被占用而启动失败。
        """
        loop = getattr(self, "_ws_loop", None)
        stop = getattr(self, "_ws_stop", None)
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass   # loop 已停
        t = getattr(self, "_ws_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    def stop(self) -> None:
        if self._server:
            t = threading.Thread(target=self._server.shutdown, daemon=True)
            t.start()
            t.join(timeout=3)          # 不阻塞主线程退出
            self._server.server_close()

    # ---------------- 数据组装 ----------------
    def _live_payload(self, after_seq: int) -> dict:
        latest = self.ring.latest_seq()
        oldest = self.ring.oldest_seq()
        full = False
        if after_seq < oldest - 1:
            full = True
            frames = self.ring.snapshot(MAX_SNAPSHOT_FRAMES)
        else:
            frames = self.ring.drain(after_seq)
        st = self.mgr.detector.state
        cur = {
            "live": st.live,
            "lap_no": st.lap_no,
            "lap_ms": st.lap_ms,
            "sector": st.sector,
            "in_pit": st.is_in_pit,
            "last_lap_ms": st.last_lap_ms,
            "best_lap_ms": st.best_lap_ms,
            "completed_laps": st.completed_laps,
            "speed_kmh": 0.0,
            "gear": 0,
            "rpms": 0,
            "kers_charge": None,
            "fuel": None,
            "tyre_compound": self.mgr.tyre_compound or "",
        }
        latest_frame = self.ring.latest()
        if latest_frame:
            ch = latest_frame["channels"]
            cur.update({
                "speed_kmh": latest_frame["speed_kmh"],
                "gear": latest_frame["gear"],
                "rpms": latest_frame["rpms"],
                "kers_charge": _ch(ch, "kersCharge"),
                "fuel": _ch(ch, "fuel"),
                "steer_deg": _deg(_ch(ch, "steerAngle")),
                # UDP 扩展数据（世界坐标/朝向/速度矢量），共享内存模式由混合监听提供
                "pos": latest_frame.get("pos"),
                "orient": latest_frame.get("orient"),
                "vel": latest_frame.get("vel"),
            })
        out_frames = []
        for fr in frames:
            ch = fr["channels"]
            out_frames.append({
                "seq": fr["seq"],
                "t": fr["t"],
                "lap_ms": fr["lap_ms"],
                "lap_no": fr["lap_no"],
                "sector": fr["sector"],
                "dist": fr.get("dist", 0.0),
                "speed": fr["speed_kmh"],
                "gear": fr["gear"],
                "rpms": fr["rpms"],
                "gas": _ch(ch, "gas"),
                "brake": _ch(ch, "brake"),
                "steer": _deg(_ch(ch, "steerAngle")),
                "susp": [_ch(ch, f"suspensionTravel_{s}") for s in ("FL", "FR", "RL", "RR")],
                "ride": [_ch(ch, f"rideHeight_{s}") for s in ("F", "R")],
                "kers": _ch(ch, "kersCharge"),
                "fuel": _ch(ch, "fuel"),
                "kj": _ch(ch, "kersCurrentKJ"),
                "turbo": _ch(ch, "turboBoost"),
                "brakeT": [_ch(ch, f"brakeTemp_{s}") for s in ("FL", "FR", "RL", "RR")],
                "wear": [_ch(ch, f"tyreWear_{s}") for s in ("FL", "FR", "RL", "RR")],
                # AC 实际布局: accG=[横向(44), 垂直(48), 纵向(52)]；按 [纵,横,垂] 顺序输出给前端
                "accG": [_ch(ch, f"accG_{s}") for s in ("Z", "X", "Y")],
                "drs": _ch(ch, "drs"),
                "tyreT": [_ch(ch, f"tyreCoreTemperature_{s}") for s in ("FL", "FR", "RL", "RR")],
                "tyreO": [_ch(ch, f"tyreTempO_{s}") for s in ("FL", "FR", "RL", "RR")],
                "slip": [_ch(ch, f"wheelSlip_{s}") for s in ("FL", "FR", "RL", "RR")],
                "pressure": [_ch(ch, f"wheelsPressure_{s}") for s in ("FL", "FR", "RL", "RR")],
                # UDP 扩展数据（世界坐标/朝向/速度矢量），无则 null
                "pos": fr.get("pos"),
                "orient": fr.get("orient"),
                "vel": fr.get("vel"),
            })
        return {"latest_seq": latest, "oldest_seq": oldest, "full": full, "current": cur,
                "frames": out_frames}

    def _laps_payload(self) -> dict:
        sid = self.mgr.session_id
        if sid is None:
            sessions = self.storage.list_sessions()
            sid = sessions[-1]["id"] if sessions else None
        if sid is None:
            return {"session_id": None, "laps": []}
        laps = self.storage.list_laps(sid)
        return {"session_id": sid, "laps": laps}

    def _cleanup(self, keep: int) -> dict:
        """清理旧会话：只保留最近 keep 个（按 id 升序取尾部），其余永久删除。

        当前正在录制的会话永远不删（可能不在尾部，比如刚重开新会话）。
        """
        sessions = sorted(self.storage.list_sessions(), key=lambda s: s["id"])
        protect = self.mgr.session_id
        keep_ids = {s["id"] for s in sessions[-keep:]}
        deleted = 0
        freed = 0
        for s in sessions:
            if s["id"] in keep_ids or s["id"] == protect:
                continue
            freed += self.storage.frame_count(s["id"])
            self.storage.delete_session(s["id"])
            deleted += 1
        return {"deleted": deleted, "freed_frames": freed}

    def _lap_payload(self, lap_id: int) -> dict:
        """返回某一圈的全部帧（与 /api/live 帧格式一致，供前端回放）。

        不降采样（精度优先）：大数据量靠 _json 的 gzip 无损压缩提速。
        """
        import numpy as np
        lap = self.storage.get_lap(lap_id)
        if not lap:
            return {"lap": None, "frames": []}
        frames = self.storage.frames_for_lap(lap["session_id"], lap_id)
        if not frames:
            return {"lap": None, "frames": []}
        n = len(frames)
        # 整圈批量解析：所有 payload 拼成一个大 float32 矩阵，一次性向量化提取，
        # 避免逐帧 np.frombuffer（每帧 167 个 float 转换，16000 帧 = 268 万次无谓转换）。
        # 数值与原逐帧解析完全一致（同一份 float32 数据），不损失精度。
        arr = np.frombuffer(b"".join(fr["payload"] for fr in frames), dtype="<f4").reshape(n, -1)

        def col(name):
            """按通道名批量取整列；缺通道/NaN 保持原 _ch 语义返回 None。"""
            idx = cfg_index(name)
            if idx is None or idx >= arr.shape[1]:
                return [None] * n
            vals = arr[:, idx].tolist()
            return [v if v == v else None for v in vals]

        gas = col("gas"); brake = col("brake")
        steer_rad = col("steerAngle")
        susp = {s: col(f"suspensionTravel_{s}") for s in ("FL", "FR", "RL", "RR")}
        ride = {s: col(f"rideHeight_{s}") for s in ("F", "R")}
        kers = col("kersCharge"); fuel = col("fuel"); kj = col("kersCurrentKJ")
        turbo = col("turboBoost")
        brakeT = {s: col(f"brakeTemp_{s}") for s in ("FL", "FR", "RL", "RR")}
        wear = {s: col(f"tyreWear_{s}") for s in ("FL", "FR", "RL", "RR")}
        accG = {s: col(f"accG_{s}") for s in ("Z", "X", "Y")}
        drs = col("drs")
        tyreT = {s: col(f"tyreCoreTemperature_{s}") for s in ("FL", "FR", "RL", "RR")}
        tyreO = {s: col(f"tyreTempO_{s}") for s in ("FL", "FR", "RL", "RR")}
        slip = {s: col(f"wheelSlip_{s}") for s in ("FL", "FR", "RL", "RR")}
        pressure = {s: col(f"wheelsPressure_{s}") for s in ("FL", "FR", "RL", "RR")}

        out = []
        for i, fr in enumerate(frames):
            out.append({
                "seq": fr["id"],
                "t": fr["t"],
                "lap_ms": fr["lap_ms"],
                "lap_no": lap["lap_no"],
                "sector": fr["sector"],
                "dist": fr["dist"],
                "speed": fr["speed_kmh"],
                "gear": fr["gear"],
                "rpms": fr["rpms"],
                "gas": gas[i],
                "brake": brake[i],
                "steer": _deg(steer_rad[i]),
                "susp": [susp[s][i] for s in ("FL", "FR", "RL", "RR")],
                "ride": [ride[s][i] for s in ("F", "R")],
                "kers": kers[i],
                "fuel": fuel[i],
                "kj": kj[i],
                "turbo": turbo[i],
                "brakeT": [brakeT[s][i] for s in ("FL", "FR", "RL", "RR")],
                "wear": [wear[s][i] for s in ("FL", "FR", "RL", "RR")],
                # AC 实际布局: accG=[横向(44), 垂直(48), 纵向(52)]；按 [纵,横,垂] 顺序输出给前端
                "accG": [accG[s][i] for s in ("Z", "X", "Y")],
                "drs": drs[i],
                "tyreT": [tyreT[s][i] for s in ("FL", "FR", "RL", "RR")],
                "tyreO": [tyreO[s][i] for s in ("FL", "FR", "RL", "RR")],
                "slip": [slip[s][i] for s in ("FL", "FR", "RL", "RR")],
                "pressure": [pressure[s][i] for s in ("FL", "FR", "RL", "RR")],
            })
        return {"lap": {k: lap[k] for k in ("id", "session_id", "lap_no", "total_ms",
                                            "s1_ms", "s2_ms", "s3_ms", "is_valid")},
                "frames": out}

    def _laphtml(self, lap_id: int) -> tuple:
        """生成单圈 HTML 报告（内嵌数据 + Chart.js 曲线 + 统计表）。"""
        import numpy as np
        import ac_udp
        lap = self.storage.get_lap(lap_id)
        if not lap:
            return None, None
        if not lap.get("total_ms") or lap["total_ms"] <= 0:
            return None, None   # 未完成/异常圈：无圈速，不生成报告
        frames = self.storage.frames_for_lap(lap["session_id"], lap_id)
        if not frames:
            return None, None
        # 降采样到 4000 点（曲线细节更完整）
        MAX_PTS = 4000
        step = max(1, len(frames) // MAX_PTS)
        sel = frames[::step]
        data = []
        max_speed = 0.0
        for fr in sel:
            ch = [float(v) for v in np.frombuffer(fr["payload"], dtype="<f4")]
            sp = fr["speed_kmh"]
            if sp > max_speed:
                max_speed = sp
            data.append({
                "t": fr["lap_ms"] / 1000.0,
                "d": fr["dist"],
                "v": sp,
                "gear": fr["gear"],
                "rpm": fr["rpms"],
                "gas": _ch(ch, "gas") * 100,
                "brk": _ch(ch, "brake") * 100,
                "steer": _deg(_ch(ch, "steerAngle")),
                "accX": _ch(ch, "accG_Z"),   # 纵向
                "accY": _ch(ch, "accG_X"),   # 横向
                "drs": _ch(ch, "drs"),
                "kers": (_ch(ch, "kersCharge") or 0) * 100,
                "fuel": (_ch(ch, "fuel") or 0),
                "turbo": _ch(ch, "turboBoost"),
                "susp": [_ch(ch, f"suspensionTravel_{s}") * 1000 for s in ("FL", "FR", "RL", "RR")],
                "ride": [_ch(ch, f"rideHeight_{s}") * 1000 for s in ("F", "R")],
                "tyre": [_ch(ch, f"tyreCoreTemperature_{s}") for s in ("FL", "FR", "RL", "RR")],
                "slip": [(_ch(ch, f"wheelSlip_{s}") or 0) * 100 for s in ("FL", "FR", "RL", "RR")],   # 四轮滑移率 %
                "wear": [_ch(ch, f"tyreWear_{s}") for s in ("FL", "FR", "RL", "RR")],   # 轮胎磨损 0~100%（基础字段，可靠）
                # 世界坐标（RT 遥测采集；无数据为 None，供 AI 轨迹/走线分析）
                "pos": [fr["pos_x"], fr["pos_y"], fr["pos_z"]]
                      if fr["pos_x"] is not None else None,
            })
        # 差分补充朝向（yaw°）与速度矢量（m/s）：AI 分析滑移角/走线用；无 pos 数据则为 None
        prev_p = prev_t = None
        for d in data:
            p = d["pos"]
            if p is None or prev_p is None or d["t"] - prev_t <= 0.001:
                d["yaw"] = d["vel"] = None
            else:
                dt = d["t"] - prev_t
                dx, dy, dz = p[0] - prev_p[0], p[1] - prev_p[1], p[2] - prev_p[2]
                yaw = math.atan2(dz, dx) * 180.0 / math.pi if (dx or dz) else 0.0
                d["yaw"] = round((yaw + 360.0) % 360.0, 1)
                d["vel"] = [round(dx / dt, 2), round(dy / dt, 2), round(dz / dt, 2)]
            if p is not None:
                prev_p, prev_t = p, d["t"]
        session = self.storage.get_session(lap["session_id"])
        s1, s2, s3 = lap["s1_ms"], lap["s2_ms"], lap["s3_ms"]
        def col(key, f=None):
            vals = [d[key] for d in data if d[key] is not None]
            if not vals:
                return 0.0
            return f(vals) if f else float(max(vals))
        avg = lambda v: sum(v) / len(v)
        max_lat = col("accY", lambda v: max(abs(x) for x in v))
        max_lon = col("accX", lambda v: max(abs(x) for x in v))
        avg_speed = col("v", avg)
        max_rpm = col("rpm")
        max_gas = col("gas")
        max_brk = col("brk")
        fuel0 = data[0]["fuel"] if data else 0
        fuel1 = data[-1]["fuel"] if data else 0
        fuel_use = max(0.0, (fuel0 or 0) - (fuel1 or 0))
        kers0 = data[0]["kers"] if data else 0
        kers1 = data[-1]["kers"] if data else 0
        # 最大滑移率：P98 分位 + 封顶 100%（AC 的 wheelSlip 偶发 >1 尖峰，直接用 max 会虚高）
        slip_vals = sorted(max(d["slip"]) for d in data if d.get("slip"))
        if slip_vals:
            p98 = slip_vals[min(len(slip_vals) - 1, int(len(slip_vals) * 0.98))]
            max_slip = min(p98, 100.0)
        else:
            max_slip = 0.0
        # ---- 弯角分析：横向G >= 1.2g 的连续区间视为一个弯 ----
        # 官方弯表（按赛道）：匈牙利 Hungaroring 14 弯，2026 官方命名；位置为估算代表点(km)

        track_key = match_track_key((session or {}).get("track") or "")
        turns = TRACK_TURNS_DATA.get(track_key, [])
        base_d = data[0]["d"] if data else 0.0
        TH, MIN_LEN, MERGE_M = 1.2, 10, 30
        runs, cur = [], []
        for i, f in enumerate(data):
            if abs(f.get("accY") or 0) >= TH:
                cur.append(i)
            else:
                if cur:
                    runs.append(cur); cur = []
        if cur:
            runs.append(cur)
        runs = [r for r in runs if len(r) >= MIN_LEN]
        corners = []
        for r in runs:
            pk = max(r, key=lambda i: abs(data[i].get("accY") or 0))
            c = {
                "dist_km": (data[pk]["d"] - base_d) / 1000.0,
                "peak_g": abs(data[pk].get("accY") or 0),
                "min_speed": min((data[i]["v"] for i in r), default=0),
            }
            if corners and c["dist_km"] - corners[-1]["dist_km"] < MERGE_M / 1000.0:
                if c["peak_g"] > corners[-1]["peak_g"]:
                    corners[-1] = c
            else:
                corners.append(c)
        corners.sort(key=lambda c: c["dist_km"])

        # 单弯角"刹车→开油"时间：弯心往前找第一次踩刹车(brk>5%)、往后找第一次开油(gas>10%)，
        # 返回两者时间差（秒）；找不到（全油门弯/数据缺失）返回 None。
        def brake_to_throttle(pos_km):
            best_i, best_dd = 0, 1e9
            for i, f in enumerate(data):
                dd = abs(((f["d"] - base_d) / 1000.0) - pos_km)
                if dd < best_dd:
                    best_dd, best_i = dd, i
            t_brake = None
            for i in range(best_i, max(0, best_i - 300), -1):
                if (data[i].get("brk") or 0) > 5:
                    t_brake = data[i]["t"]
                    break
            t_gas = None
            for i in range(best_i, min(len(data), best_i + 150)):
                if (data[i].get("gas") or 0) > 10:
                    t_gas = data[i]["t"]
                    break
            if t_brake is not None and t_gas is not None and t_gas > t_brake:
                return t_gas - t_brake
            return None

        # 官方弯名映射：识别弯按位置归属官方弯（相邻代表点中点分界，保证顺序）
        def official_rows(corners, turns):
            if not turns:
                rows = []
                for n, c in enumerate(corners, 1):
                    rows.append(f'<tr><td>弯{n}</td><td class="num">{c["dist_km"]:.2f} km</td>'
                                f'<td class="num">{c["min_speed"]:.0f} km/h</td>'
                                f'<td class="num">{c["peak_g"]:.2f} g</td></tr>')
                return rows, {}
            bounds = [(turns[i][2] + turns[i+1][2]) / 2 for i in range(len(turns) - 1)]
            assigned = {n: [] for n in range(1, len(turns) + 1)}
            for c in corners:
                idx = 0
                for b in bounds:
                    if c["dist_km"] > b:
                        idx += 1
                    else:
                        break
                assigned[idx + 1].append(c)
            rows = []
            label_map = {}
            for n, name, pos in turns:
                lst = assigned[n]
                if not lst:
                    continue
                best = max(lst, key=lambda c: c["peak_g"])
                min_sp = min(c["min_speed"] for c in lst)
                label_map[n] = name
                rows.append((n, name, best["dist_km"], min_sp, best["peak_g"]))
            # 同名相邻弯合并（T6-7 Driving Center、T8-9 Buda/Pest）
            merged = []
            for r in rows:
                if merged and merged[-1][1] == r[1]:
                    prev = merged[-1]
                    start_n = str(prev[0]).split("-")[0]
                    if r[4] > prev[4]:   # 新弯 G 更大，位置取新弯
                        merged[-1] = (f"{start_n}-{r[0]}", prev[1], r[2],
                                      min(prev[3], r[3]), r[4])
                    else:
                        merged[-1] = (f"{start_n}-{r[0]}", prev[1], prev[2],
                                      min(prev[3], r[3]), prev[4])
                else:
                    merged.append((str(r[0]), r[1], r[2], r[3], r[4]))
            html_rows = []
            for n, name, dk, ms, pg in merged:
                nm = f" {name}" if name else ""
                bt = brake_to_throttle(dk)   # 有官方弯角数据才计算刹车→开油时间
                bt_str = f"{bt:.2f}s" if bt is not None else "—"
                html_rows.append(f'<tr><td>T{n}{nm}</td><td class="num">{dk:.2f} km</td>'
                                 f'<td class="num">{ms:.0f} km/h</td>'
                                 f'<td class="num">{pg:.2f} g</td>'
                                 f'<td class="num">{bt_str}</td></tr>')
            return html_rows, label_map

        html_rows, label_map = official_rows(corners, turns)
        corners_html = "".join(html_rows) if html_rows else (
            f'<tr><td colspan="{5 if turns else 4}" style="color:var(--dim)">本圈无明显弯角（横向G峰值低于 1.2g）</td></tr>')
        # 弯角表表头：有官方弯角数据才加"刹车→开油"列
        corner_header = ('<tr><th>弯角</th><th class="num">位置</th><th class="num">最低速度</th>'
                         '<th class="num">最大横向G</th>')
        if turns:
            corner_header += '<th class="num">刹车→开油</th>'
        corner_header += '</tr>'
        # 轮胎磨损（基础字段 tyreWear 0~100%，取末帧四轮）
        _w = (data[-1].get("wear") if data else None) or []
        wear_row = " / ".join(f"{x:.1f}%" for x in _w) if _w and all(x is not None for x in _w) else "—"
        # 横/纵最大 G 出现位置（优先用官方弯名）
        lon_pk = max(data, key=lambda f: abs(f.get("accX") or 0))
        lat_pk = max(data, key=lambda f: abs(f.get("accY") or 0))
        lon_d = (lon_pk["d"] - base_d) / 1000.0
        lat_d = (lat_pk["d"] - base_d) / 1000.0

        def corner_label(dk):
            best, bi, bn = None, None, None
            if turns:
                for n, name, pos in turns:
                    d = abs(pos - dk)
                    if best is None or d < best:
                        best, bi, bn = d, n, name
                if best is not None and best < 0.20:
                    return f"T{bi}{' ' + bn if bn else ''} · {dk:.2f} km"
            for i, c in enumerate(corners, 1):
                d = abs(c["dist_km"] - dk)
                if best is None or d < best:
                    best, bi = d, i
            return f"弯{bi} · {dk:.2f} km" if best is not None and best < 0.15 else f"{dk:.2f} km"

        gstat_rows = (
            f'<tr><td>最大横向G位置</td><td class="num">{abs(lat_pk.get("accY") or 0):.2f} g（{corner_label(lat_d)}）</td></tr>'
            f'<tr><td>最大纵向G位置</td><td class="num">{abs(lon_pk.get("accX") or 0):.2f} g（{corner_label(lon_d)}）</td></tr>'
        )
        json_data = json.dumps({
            "lap_no": lap["lap_no"], "total_ms": lap["total_ms"],
            "s1": s1, "s2": s2, "s3": s3,
            "top_speed": lap["top_speed_kmh"] or max_speed,
            "car": (session or {}).get("car", ""),
            "track": (session or {}).get("track", ""),
            "frames": data,
        }, ensure_ascii=False)
        # 普通字符串模板（非 f-string），动态值用 %()s 占位
        html = _lap_report_template() % {
            "lap_no": lap["lap_no"],
            "car": (session or {}).get("car", ""),
            "track": (session or {}).get("track", ""),
            "total": f"{lap['total_ms']//60000}:{(lap['total_ms']%60000)//1000:02d}.{lap['total_ms']%1000:03d}",
            "s1": f"{s1/1000 if s1 else 0:.2f}", "s2": f"{s2/1000 if s2 else 0:.2f}",
            "s3": f"{s3/1000 if s3 else 0:.2f}",
            "top_speed": f"{lap['top_speed_kmh'] or max_speed:.0f}",
            "avg_speed": f"{avg_speed:.0f}",
            "max_lon": f"{max_lon:.2f}", "max_lat": f"{max_lat:.2f}",
            "fuel_use": f"{fuel_use:.2f}", "n_pts": len(data),
            "compound": (lap.get("tyre_compound") or "—"),
            "max_slip": f"{max_slip:.1f}",
            "max_rpm": f"{max_rpm:.0f}", "max_gas": f"{max_gas:.0f}",
            "max_brk": f"{max_brk:.0f}",
            "kers0": f"{kers0:.0f}", "kers1": f"{kers1:.0f}",
            "fuel0": f"{fuel0:.1f}", "fuel1": f"{fuel1:.1f}",
            "gstat_rows": gstat_rows, "corners_html": corners_html,
            "corner_header": corner_header, "wear_row": wear_row,
            "json_data": json_data,
        }
        # 下载文件名：赛道名 + 圈速（如 红牛环_1-19.417.html），冒号/斜杠等文件名非法字符全部去掉
        _cn = {"suzuka": "铃鹿", "barcelona": "巴塞罗那", "redbullring": "红牛环",
               "hungaroring": "匈牙利", "gilles_villeneuve": "蒙特利尔",
               "baku": "巴库", "cota": "美洲赛道",
               "silverstone": "银石", "monza": "蒙扎",
               "zandvoort": "赞德沃特"}.get(track_key)
        if not _cn:
            _cn = ((session or {}).get("track") or "lap").split("/")[-1].strip()
            for _pfx in ("fn_", "ts_", "ks_", "dr_", "ac_", "kart_"):
                if _cn.startswith(_pfx):
                    _cn = _cn[len(_pfx):]
                    break
        _tot = max(0, int(lap.get("total_ms") or 0))
        _m, _rem = divmod(_tot // 1000, 60)
        _f = _tot % 1000
        return html, f"{_cn}_{_m}-{_rem:02d}.{_f:03d}.html"

    def _session_payload(self) -> dict:
        sid = self.mgr.session_id
        if sid is None:
            sessions = self.storage.list_sessions()
            sid = sessions[-1]["id"] if sessions else None
        if sid is None:
            return {"session_id": None, "name": None, "car": None, "track": None,
                    "frames": 0}
        s = self.storage.get_session(sid)
        n = self.storage.frame_count(sid)
        return {"session_id": sid, "name": s["name"], "car": self.mgr.car or s["car"],
                "track": self.mgr.track or s["track"], "frames": n,
                "laps_done": self.mgr.laps_done}

    def _turns_payload(self, track: str) -> dict:
        """赛道弯角数据（轨迹图标注用）。返回 track_key + turns 列表。"""
        key = match_track_key(track)
        turns = TRACK_TURNS_DATA.get(key, [])
        return {"track_key": key, "turns": [{"n": n, "name": nm, "km": km}
                                            for n, nm, km in turns]}

    def _trackmap_payload(self, track: str) -> dict:
        """赛道 outline 图（轨迹图背景）。在 AC 安装目录 tracks/<dir>/ui/**/ 下
        找 outline_cropped.png（优先，已裁剪到赛道 bbox）或 outline.png。
        返回 dataURL + 图宽高 + 轨迹对齐参数（theta/flipX/flipZ），用于把
        世界坐标 (x, z) 旋转/镜像到赛道图像素坐标。
        对齐结果按 track 名缓存到 data/align_cache.json，跑过一次后秒回。"""
        import base64 as _b64
        import glob as _glob
        import json as _json
        import struct as _st
        from trackalign import compute_align
        tracks_dir = cfg.find_ac_tracks_dir()
        if not tracks_dir:
            return {"ok": False}
        # 优先：track 名直接匹配目录（session.track 一般是目录名）
        cand_dirs = []
        d0 = tracks_dir / track
        if d0.is_dir():
            cand_dirs.append(d0)
        # 兜底：按 track_key 模糊匹配（含 key 的目录）
        key = match_track_key(track)
        if key:
            for d in tracks_dir.iterdir():
                if d.is_dir() and key in d.name.lower():
                    cand_dirs.append(d)
        cache = self._load_align_cache()
        for d in cand_dirs:
            pngs = sorted(_glob.glob(str(d / "ui" / "**" / "outline_cropped.png"), recursive=True)) \
                 or sorted(_glob.glob(str(d / "ui" / "**" / "outline.png"), recursive=True))
            for p in pngs:
                if "outline_1" in p:
                    continue
                try:
                    raw = Path(p).read_bytes()
                except OSError:
                    continue
                if len(raw) < 3000:   # 空/占位/纯透明小图（如 384B 透明 png）跳过
                    continue
                w = h = 0
                if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 24:
                    w, h = _st.unpack(">II", raw[16:24])
                if w < 50 or h < 50:
                    continue
                b64 = _b64.b64encode(raw).decode("ascii")
                # 对齐：优先用缓存（基于 track 名 + 图尺寸）；没有则用 DB 轨迹点预算
                # score 阈值 0.70：mod 赛道图（赞德沃特等）赛道像素稀疏 + 边界模糊，
                # 完美几何对齐的 score 也只有 0.5-0.7（硬验证发现），用纯像素评分易误判。
                # 阈值放宽后其他赛道跑 1 圈也能写入 cache（不完美的对齐也比无对齐的轴对齐好）。
                cached = cache.get(track)
                cached_size = cached.get("size") if cached else None
                if isinstance(cached_size, list):
                    cached_size = tuple(cached_size)
                align = cached if (cached and cached_size == (w, h)) else None
                if align is None or align.get("score", 0) < 0.70:
                    align = self._compute_track_align(track, raw)
                    if align is not None and align.get("score", 0) >= 0.70:
                        cache[track] = {**align, "size": [w, h], "ts": __import__("time").time()}
                        self._save_align_cache(cache)
                    else:
                        align = None  # 不达标，前端走无 align 的轴对齐 fallback
                out = {"ok": True, "src": f"data:image/png;base64,{b64}", "w": w, "h": h}
                if align:
                    out["align"] = align
                return out
        return {"ok": False}

    def _compute_track_align(self, track: str, png_raw: bytes):
        """从 DB 取该 track 的轨迹点（pos_x, pos_z），旋转搜索找最优对齐。"""
        try:
            import numpy as _np
            import sqlite3 as _sq
            db_path = self.storage.db_path
            conn = _sq.connect(str(db_path))
            cur = conn.cursor()
            cur.execute(
                "SELECT pos_x, pos_z FROM frames WHERE pos_x IS NOT NULL AND pos_z IS NOT NULL "
                "AND session_id IN (SELECT id FROM sessions WHERE track = ?) "
                "ORDER BY id DESC LIMIT 12000", (track,))
            rows = cur.fetchall()
            conn.close()
            if len(rows) < 30:
                return None
            pts = _np.asarray(rows, dtype=float)
            from trackalign import compute_align as _ca
            return _ca(png_raw, pts)
        except Exception as exc:
            print(f"[dashboard] 对齐计算失败: {type(exc).__name__}: {exc}")
            return None

    def _align_cache_path(self) -> "Path":
        return Path(self.storage.db_path).parent / "align_cache.json"

    def _load_align_cache(self) -> dict:
        """加载磁盘 cache + 合并代码内置 seed（已知目视验证的 align）。

        seed 优先（用户验证过的目视对齐），disk cache 兜底（运行时自动算的）。
        """
        try:
            import json as _json
            p = self._align_cache_path()
            disk = _json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except Exception:
            disk = {}
        merged = dict(disk)
        merged.update(SEED_TRACK_ALIGN)
        return merged

    def _save_align_cache(self, cache: dict) -> None:
        try:
            import json as _json
            p = self._align_cache_path()
            p.write_text(_json.dumps(cache, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        except Exception as exc:
            print(f"[dashboard] align 缓存写入失败: {exc}")

    # ---------------- HTTP ----------------
    def _make_handler(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静默访问日志
                pass

            def _json(self, obj, code=200):
                import gzip as _gzip
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                enc = ""
                if "gzip" in self.headers.get("Accept-Encoding", ""):
                    # level 1：JSON 文本压缩率本来就高，默认 9 级 CPU 开销大（几 MB 数据要 0.1-0.2s），
                    # 降到 1 级压缩快 3-5 倍，体积只多几个百分点 —— 回放接口延迟大头之一
                    body = _gzip.compress(body, mtime=0, compresslevel=1)
                    enc = "gzip"
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                if enc:
                    self.send_header("Content-Encoding", enc)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _static(self, name: str):
                p = (WEB_DIR / name).resolve()
                if not str(p).startswith(str(WEB_DIR.resolve())) or not p.exists():
                    self.send_error(404)
                    return
                ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                         ".css": "text/css; charset=utf-8", ".png": "image/png",
                         ".svg": "image/svg+xml", ".ico": "image/x-icon"}.get(
                    p.suffix.lower(), "application/octet-stream")
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                # 静态资源缓存：/assets/ 下是 vite hash 文件名（内容变→文件名变），
                # 可 immutable 长缓存；页面入口 index.html 必须 no-store（保证拿最新）
                if name.startswith("assets/"):
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                else:
                    self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            # ---- 路由表（重构 #1：替代 do_GET 的 if-elif 串，新增接口只需注册一条）----
            def _h_index(self, path, params):
                self._static("index.html")

            def _h_live(self, path, params):
                try:
                    after = int(params.get("after", "0"))
                except ValueError:
                    after = 0
                self._json(dash._live_payload(after))

            def _h_laps(self, path, params):
                self._json(dash._laps_payload())

            def _h_cleanup(self, path, params):
                try:
                    keep = max(1, int(params.get("keep", "3")))
                except ValueError:
                    keep = 3
                self._json(dash._cleanup(keep))

            def _h_session(self, path, params):
                self._json(dash._session_payload())

            def _h_turns(self, path, params):
                self._json(dash._turns_payload(params.get("track", "")))

            def _h_trackmap(self, path, params):
                self._json(dash._trackmap_payload(params.get("track", "")))

            def _h_lap(self, path, params):
                try:
                    lap_id = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    lap_id = -1
                self._json(dash._lap_payload(lap_id))

            def _h_laphtml(self, path, params):
                try:
                    lap_id = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    lap_id = -1
                html_text, fname = dash._laphtml(lap_id)
                if html_text is None:
                    self.send_error(404)
                    return
                body = html_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # HTTP 头必须 latin-1：filename 用 URL 编码（ASCII 安全），中文名走 filename*
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{quote(fname)}\"; "
                                 f"filename*=UTF-8''{quote(fname)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _h_static(self, path, params):
                self._static(path[len("/static/"):])

            def _h_assets(self, path, params):
                # 新版前端 build 产物：/assets/x → WEB_DIR/assets/x（保留目录层级）
                self._static(path[1:])

            _ROUTES = [
                ("exact", "/", _h_index),
                ("exact", "/index.html", _h_index),
                ("exact", "/api/live", _h_live),
                ("exact", "/api/laps", _h_laps),
                ("exact", "/api/cleanup", _h_cleanup),
                ("exact", "/api/session", _h_session),
                ("exact", "/api/turns", _h_turns),
                ("exact", "/api/trackmap", _h_trackmap),
                # 前缀路由按长度降序：laphtml 先于 lap（避免误匹配）
                ("prefix", "/api/laphtml/", _h_laphtml),
                ("prefix", "/api/lap/", _h_lap),
                ("prefix", "/static/", _h_static),
                ("prefix", "/assets/", _h_assets),
            ]

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                params = dict(pair.split("=", 1) for pair in q.split("&") if "=" in pair)
                for kind, pat, fn in self._ROUTES:
                    if (kind == "exact" and path == pat) or (kind == "prefix" and path.startswith(pat)):
                        fn(self, path, params)
                        return
                self.send_error(404)

        return Handler

_LAP_TPL_FALLBACK = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>单圈报告 · 圈 %(lap_no)s</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root { --bg:#0d1117; --panel:#161b22; --border:#2d333b; --text:#e6edf3; --dim:#8b949e; }
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:"Segoe UI","Microsoft YaHei",sans-serif; padding:24px; max-width:1080px; margin:0 auto; }
h1 { font-size:20px; margin-bottom:12px; }
.meta { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; margin-bottom:20px; }
.meta div { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px; }
.meta .k { font-size:11px; color:var(--dim); } .meta .v { font-size:18px; font-weight:700; }
table { width:100%%; border-collapse:collapse; font-size:13px; margin-bottom:20px; }
th,td { padding:6px 10px; text-align:left; border-bottom:1px solid var(--border); }
th { color:var(--dim); font-weight:600; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.grid { display:grid; grid-template-columns:1fr; gap:12px; }
.card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:12px; }
.card h3 { font-size:13px; color:var(--dim); margin-bottom:8px; }
.chart-box { height:240px; position:relative; }
.chart-box canvas { position:absolute; left:0; top:0; width:100%%; height:100%%; }
</style></head><body>
<h1 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">🏁 单圈报告 · 圈 %(lap_no)s <span style="color:var(--dim);font-size:14px">%(car)s @ %(track)s</span></h1>
<div class="meta">
  <div><div class="k">圈时</div><div class="v">%(total)s</div></div>
  <div><div class="k">S1 / S2 / S3</div><div class="v">%(s1)s / %(s2)s / %(s3)s</div></div>
  <div><div class="k">极速</div><div class="v">%(top_speed)s km/h</div></div>
  <div><div class="k">平均速度</div><div class="v">%(avg_speed)s km/h</div></div>
  <div><div class="k">最大纵向G</div><div class="v">%(max_lon)s g</div></div>
  <div><div class="k">最大横向G</div><div class="v">%(max_lat)s g</div></div>
  <div><div class="k">耗油</div><div class="v">%(fuel_use)s L</div></div>
  <div><div class="k">轮胎配方</div><div class="v">%(compound)s</div></div>
  <div><div class="k">最大滑移率</div><div class="v">%(max_slip)s%%</div></div>
  <div><div class="k">采样点数</div><div class="v">%(n_pts)s</div></div>
</div>
<h2 style="font-size:15px;margin-bottom:8px">📊 统计</h2>
<table>
<thead><tr><th>指标</th><th class="num">值</th></tr></thead><tbody>
  <tr><td>最高转速</td><td class="num">%(max_rpm)s rpm</td></tr>
  <tr><td>电量变化</td><td class="num">%(kers0)s%% → %(kers1)s%%</td></tr>
  <tr><td>油量变化</td><td class="num">%(fuel0)s L → %(fuel1)s L</td></tr>
  <tr><td>轮胎磨损</td><td class="num">%(wear_row)s</td></tr>
%(gstat_rows)s
</tbody></table>
<h2 style="font-size:15px;margin-bottom:8px">🔄 弯角分析（横向G峰值 ≥ 1.2g）</h2>
<table>
<thead>%(corner_header)s</thead>
<tbody>
%(corners_html)s
</tool_body></table>
<div class="grid">
<div class="card"><h3>车速 km/h（DRS 段红色）</h3><div class="chart-box"><canvas id="c-speed"></canvas></div></div>
<div class="card"><h3>油门 %% / 刹车 %%</h3><div class="chart-box"><canvas id="c-pedals"></canvas></div></div>
<div class="card"><h3>转速 rpm</h3><div class="chart-box"><canvas id="c-rpm"></canvas></div></div>
<div class="card"><h3>挡位</h3><div class="chart-box"><canvas id="c-gear"></canvas></div></div>
<div class="card"><h3>纵向 G / 横向 G</h3><div class="chart-box"><canvas id="c-acc"></canvas></div></div>
<div class="card"><h3>转向角度 °</h3><div class="chart-box"><canvas id="c-steer"></canvas></div></div>
<div class="card"><h3>悬架行程 mm（FL/FR/RL/RR）</h3><div class="chart-box"><canvas id="c-susp"></canvas></div></div>
<div class="card"><h3>底板高度 mm（前/后）</h3><div class="chart-box"><canvas id="c-ride"></canvas></div></div>
<div class="card"><h3>电量 %% / 油量 L</h3><div class="chart-box"><canvas id="c-kers"></canvas></div></div>
<div class="card"><h3>涡轮压力 bar</h3><div class="chart-box"><canvas id="c-turbo"></canvas></div></div>
<div class="card"><h3>胎温 °C（FL/FR/RL/RR）</h3><div class="chart-box"><canvas id="c-tyre"></canvas></div></div>
<div class="card"><h3>滑移率 %%（FL/FR/RL/RR）</h3><div class="chart-box"><canvas id="c-slip"></canvas></div></div>
<div class="card"><h3>轮胎磨损 %%（FL/FR/RL/RR）</h3><div class="chart-box"><canvas id="c-wear"></canvas></div></div></div>
<script>
const D = %(json_data)s;
const X = D.frames.map(f => (f.d > 0 ? (f.d - D.frames[0].d) / 1000 : f.t));
const drs = D.frames.map(f => f.drs);
const GRID = { color:"rgba(139,148,158,0.15)" };
const TICK = "#8b949e";
function mk(id, datasets, extraY) {
  // 横轴按赛道实际长度自适应：数据最大距离向上取整到 0.1km，避免刻度取整后轴延伸到 5km 造成右侧空白
  const xMax = Math.ceil(Math.max(...X) * 10) / 10 || 1;
  const scales = {
    x: { type:"linear", min:0, max:xMax, ticks: { color:TICK, maxTicksLimit:6, callback:v => v>=1 ? v.toFixed(1)+"km" : Math.round(v*1000)+"m" },
          grid: GRID, title: { display:true, text:"圈内距离", color:TICK, font:{size:10} } },
    y: { ticks: { color:TICK }, grid: GRID }
  };
  Object.assign(scales, extraY||{});
  return new Chart(document.getElementById(id), {
    type: "line", data: { labels: X, datasets },
    options: { animation:false, responsive:true, maintainAspectRatio:false, scales,
      interaction: { mode:"index", intersect:false },
      plugins: { legend: { labels: { color:"#e6edf3", boxWidth:10, font:{size:10} } } } }
  });
}
function get(f, key) {
  const parts = key.split(".");
  let v = f;
  for (const p of parts) v = v ? v[p] : undefined;
  return v;
}
function smooth(arr, win) {
  if (!win || win <= 1 || !arr || arr.length <= win) return arr;
  const half = Math.floor(win / 2), out = [];
  for (let i = 0; i < arr.length; i++) {
    let sum = 0, n = 0;
    for (let j = Math.max(0, i - half); j <= Math.min(arr.length - 1, i + half); j++) {
      const v = arr[j];
      if (v != null && !isNaN(v)) { sum += v; n++; }
    }
    out.push(n ? sum / n : null);
  }
  return out;
}
function s(name, color, key, extra, win) {
  const ds = { label:name, borderColor:color, backgroundColor:color, data: smooth(D.frames.map(f => get(f, key)), win || 0),
    borderWidth:1.5, pointRadius:0, tension:0.15, spanGaps:true };
  Object.assign(ds, extra||{});
  return ds;
}
const SPEED_CFG = { segment: { borderColor: ctx => {
  const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex;
  return (drs[i]||0) > 0.5 ? "#f85149" : "#58a6ff";
} } };
mk("c-speed", [s("车速 km/h", "#58a6ff", "v", SPEED_CFG, 3)]);
mk("c-pedals", [s("油门 %%", "#2ea043", "gas", null, 3), s("刹车 %%", "#f85149", "brk", null, 3)]);
mk("c-rpm", [s("转速 rpm", "#d2a8ff", "rpm", null, 3)]);
mk("c-gear", [s("挡位", "#e3b341", "gear", null, 1)]);   // 档位不平滑（阶梯数据）
mk("c-acc", [s("纵向 G", "#58a6ff", "accX", null, 5), s("横向 G", "#f0883e", "accY", null, 5)]);
mk("c-steer", [s("转向 °", "#3fb950", "steer", null, 5)]);
mk("c-susp", ["FL","FR","RL","RR"].map((w,i) => s("悬架 "+w+" mm", ["#58a6ff","#f0883e","#3fb950","#da3633"][i], "susp." + i, null, 5)));
mk("c-ride", [s("底板 前 mm", "#f0f6fc", "ride.0", null, 5), s("底板 后 mm", "#f0883e", "ride.1", null, 5)]);
mk("c-kers", [s("电量 %%", "#7ee787", "kers", null, 3), s("油量 L", "#e3b341", "fuel", null, 5)]);
mk("c-turbo", [s("涡轮 bar", "#d2a8ff", "turbo", null, 3)]);
mk("c-tyre", ["FL","FR","RL","RR"].map((w,i) => s("胎温 "+w+" °C", ["#58a6ff","#f0883e","#3fb950","#da3633"][i], "tyre." + i, null, 5)));
mk("c-slip", ["FL","FR","RL","RR"].map((w,i) => s("滑移率 "+w+" %%", ["#58a6ff","#f0883e","#3fb950","#da3633"][i], "slip." + i, null, 3)));
// 轮胎磨损曲线：磨损变化极小（~99%%），Y 轴跟随数据范围放大显示
(function(){
  const wa = [];
  for (const f of D.frames) if (f.wear) for (const v of f.wear) if (v != null && !isNaN(v)) wa.push(v);
  let lo = 0, hi = 100;
  if (wa.length) { lo = Math.min.apply(null, wa); hi = Math.max.apply(null, wa); }
  const pad = (hi - lo) || 2;
  mk("c-wear", ["FL","FR","RL","RR"].map((w,i) => s("磨损 "+w+" %%", ["#58a6ff","#f0883e","#3fb950","#da3633"][i], "wear." + i, null, 1)),
    { y: { ticks: { color: TICK }, grid: GRID, min: Math.max(0, lo - pad * 0.3), max: hi + pad * 0.3 } });
})();
</script></body></html>"""


def _lap_report_template() -> str:
    """单圈报告 HTML 模板：优先读独立文件 templates/lap_report.html（改样式不用重启），
    文件缺失时回退内置模板（兼容未带 templates 的旧环境）。"""
    p = Path(__file__).resolve().parent / "templates" / "lap_report.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return _LAP_TPL_FALLBACK



def _ch(ch: list, name: str):
    idx = cfg_index(name)
    if idx is None:
        return None
    v = ch[idx]
    return None if v != v else v


def _deg(rad):
    return None if rad is None else rad * 180.0 / 3.141592653589793


_cfg_idx: dict = {}


def cfg_index(name: str):
    if not _cfg_idx:
        import ac_udp
        _cfg_idx.update(ac_udp.CHANNEL_INDEX)
    return _cfg_idx.get(name)
