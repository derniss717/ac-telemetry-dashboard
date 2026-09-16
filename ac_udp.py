# -*- coding: utf-8 -*-
"""Assetto Corsa UDP 遥测：结构体解析 + UDP 接收。

数据来源：游戏内 Documents/Assetto Corsa/cfg/udp.ini 开启 UDP 广播后，
游戏向指定端口发送与共享内存完全一致的三个结构体（pack(4)、小端）：
  - SPageFilePhysics  : 物理数据（油门/刹车/悬架/底板高度/KERS 电量等），约 333Hz
  - SPageFileGraphic  : 圈速/扇区数据，随帧率（60-144Hz）
  - SPageFileStatic   : 车型/赛道等静态信息，会话开始时发送（UDP 模式可能不发）

字段偏移依据 AC 官方 Shared Memory Reference；扩展字段（AC 1.5+ 追加的
brakeTemp / 胎温 I/M/O / ERS 等级 / kersCurrentKJ 等）采用社区通用布局，
解析时做范围合理性检查，越界字段置 NaN 并只告警一次。
"""
from __future__ import annotations

import math
import socket
import struct
import threading
import time
from typing import Callable, Dict, Optional, Tuple

# AC graphic.status 枚举
AC_STATUS_OFF = 0
AC_STATUS_REPLAY = 1
AC_STATUS_LIVE = 2
AC_STATUS_PAUSE = 3

# ---------------------------------------------------------------------------
# 字段表：(name, fmt, count, offset)   fmt: 'f'=float32 'i'=int32 'b'=bool
# ---------------------------------------------------------------------------
# SPageFilePhysics 基础结构（288 字节，AC 官方文档）
PHYSICS_FIELDS: list[tuple] = [
    ("packetId",            "i", 1, 0),
    ("gas",                 "f", 1, 4),
    ("brake",               "f", 1, 8),
    ("fuel",                "f", 1, 12),
    ("gear",                "i", 1, 16),
    ("rpms",                "i", 1, 20),
    ("steerAngle",          "f", 1, 24),
    ("speedKmh",            "f", 1, 28),
    ("velocity",            "f", 3, 32),
    ("accG",                "f", 3, 44),
    ("wheelSlip",           "f", 4, 56),
    ("wheelLoad",           "f", 4, 72),
    ("wheelsPressure",      "f", 4, 88),
    ("wheelAngularSpeed",   "f", 4, 104),
    ("tyreWear",            "f", 4, 120),
    ("tyreDirtyLevel",      "f", 4, 136),
    ("tyreCoreTemperature", "f", 4, 152),
    ("camberRAD",           "f", 4, 168),
    ("suspensionTravel",    "f", 4, 184),   # 每轮悬架行程，米
    ("drs",                 "f", 1, 200),
    ("tc",                  "f", 1, 204),
    ("heading",             "f", 1, 208),
    ("pitch",               "f", 1, 212),
    ("roll",                "f", 1, 216),
    ("cgHeight",            "f", 1, 220),
    ("carDamage",           "f", 5, 224),   # FR, FL, RR, RL, 中央
    ("numberOfTyresOut",    "i", 1, 244),
    ("pitLimiterOn",        "i", 1, 248),
    ("abs",                 "f", 1, 252),
    ("kersCharge",          "f", 1, 256),   # KERS/电池电量 0..1
    ("kersInput",           "f", 1, 260),
    ("autoShifterOn",       "i", 1, 264),
    ("rideHeight",          "f", 2, 268),   # 前/后 底板高度，米
    ("turboBoost",          "f", 1, 276),
    ("ballast",             "f", 1, 280),
    ("airDensity",          "f", 1, 284),
]
BASE_PHYSICS_SIZE = 288

# 扩展字段（400 字节布局，AC 1.5+，社区通用；解析时做合理性检查）
EXT_PHYSICS_FIELDS: list[tuple] = [
    ("roadTemp",            "f", 1, 288),
    ("roadGrip",            "f", 1, 292),
    ("frontwingAngle",      "f", 1, 296),
    ("rearwingAngle",       "f", 1, 300),
    ("drsAvailable",        "i", 1, 304),
    ("drsEnabled",          "i", 1, 308),
    ("brakeTemp",           "f", 4, 312),
    ("clutch",              "f", 1, 328),
    ("tyreTempI",           "f", 4, 332),   # 胎温内层
    ("tyreTempM",           "f", 4, 348),   # 胎温中层
    ("tyreTempO",           "f", 4, 364),   # 胎温外层
    ("ersPowerLevel",       "i", 1, 380),
    ("ersRecoveryLevel",    "i", 1, 384),
    ("ersHeatCharging",     "f", 1, 388),
    ("ersIsCharging",       "f", 1, 392),
    ("kersCurrentKJ",       "f", 1, 396),
]
EXT_PHYSICS_SIZE = 400

# SPageFileGraphic（282 字节）。wchar_t 在 Windows 下为 2 字节（UTF-16LE）
GRAPHIC_FIELDS: list[tuple] = [
    ("packetId",            "i", 1, 0),
    ("status",              "i", 1, 4),     # 0=off 1=replay 2=live 3=pause
    ("session",             "i", 1, 8),     # 0=practice 1=qualify 2=race ...
    ("currentTime",         "w", 15, 12),
    ("lastTime",            "w", 15, 42),
    ("bestTime",            "w", 15, 72),
    ("split",               "w", 15, 102),
    ("completedLaps",       "i", 1, 132),   # 已完成圈数（从 0 开始）
    ("position",            "i", 1, 136),
    ("iCurrentTime",        "i", 1, 140),   # 当前圈用时，毫秒
    ("iLastTime",           "i", 1, 144),   # 上一圈用时，毫秒
    ("iBestTime",           "i", 1, 148),   # 最佳圈用时，毫秒
    ("sessionTimeLeft",     "f", 1, 152),
    ("distanceTraveled",    "f", 1, 156),   # 已行驶距离，米
    ("isInPit",             "i", 1, 160),
    ("currentSectorIndex",  "i", 1, 164),   # 0..sectorCount-1
    ("lastSectorTime",      "i", 1, 168),   # 上一扇区用时，毫秒
    ("numberOfLaps",        "i", 1, 172),
    ("tyreCompound",        "w", 33, 176),
    ("replayTimeMultiplier","f", 1, 242),
    ("normalizedCarPosition","f", 1, 246),
    ("carCoordinates",      "f", 3, 250),
    ("penaltyTime",         "f", 1, 262),
    ("flag",                "i", 1, 266),
    ("idealLineOn",         "i", 1, 270),
    ("isInPitLane",         "i", 1, 274),
    ("surfaceGrip",         "f", 1, 278),
]
GRAPHIC_SIZE = 282

# SPageFileStatic（482 字节）。UDP 广播模式可能收不到；仅用于车型/赛道名等展示
STATIC_FIELDS: list[tuple] = [
    ("smVersion",           "w", 15, 0),
    ("acVersion",           "w", 15, 30),
    ("numberOfSessions",    "i", 1, 60),
    ("numCars",             "i", 1, 64),
    ("carModel",            "w", 33, 68),
    ("track",               "w", 33, 134),
    ("playerName",          "w", 33, 200),
    ("playerSurname",       "w", 33, 266),
    ("playerNick",          "w", 33, 332),
    ("sectorCount",         "i", 1, 398),
    ("maxTorque",           "f", 1, 402),
    ("maxPower",            "f", 1, 406),
    ("maxRpm",              "i", 1, 410),
    ("maxFuel",             "f", 1, 414),
    ("suspensionMaxTravel", "f", 4, 418),
    ("tyreRadius",          "f", 4, 434),
    ("maxTurboBoost",       "f", 1, 450),
    ("airTemp",             "f", 1, 454),
    ("roadTemp",            "f", 1, 458),
    ("penaltiesEnabled",    "b", 1, 462),
    ("aidFuelRate",         "f", 1, 463),
    ("aidTireRate",         "f", 1, 467),
    ("aidMechanicalDamage", "f", 1, 471),
    ("aidAllowTyreBlankets","b", 1, 475),
    ("aidStability",        "f", 1, 476),
    ("aidAutoClutch",       "b", 1, 480),
    ("aidAutoBlip",         "b", 1, 481),
]
STATIC_SIZE = 482

# ---------------------------------------------------------------------------
# 编译解包器
# ---------------------------------------------------------------------------
_COMPILED: Dict[Tuple[str, int], struct.Struct] = {}
_COMPILED_BY_NAME: Dict[str, struct.Struct] = {}


def _compiled(fmt: str, count: int) -> struct.Struct:
    key = (fmt, count)
    s = _COMPILED.get(key)
    if s is None:
        s = struct.Struct("<" + fmt * count)
        _COMPILED[key] = s
    return s


def _read_fields(data: bytes, fields: list[tuple]) -> Dict[str, object]:
    """按字段表从字节流中解出 dict。缺失（包不够长）的字段置 None。"""
    out: Dict[str, object] = {}
    for name, fmt, count, off in fields:
        if fmt == "w":  # UTF-16LE 字符串
            need = count * 2
            if off + need > len(data):
                out[name] = None
                continue
            raw = data[off:off + need]
            out[name] = raw.decode("utf-16-le", errors="replace").split("\x00")[0].strip()
            continue
        if fmt == "b":
            if off + 1 > len(data):
                out[name] = None
                continue
            out[name] = bool(data[off])
            continue
        st = _compiled(fmt, count)
        if off + st.size > len(data):
            out[name] = None
            continue
        vals = st.unpack_from(data, off)
        out[name] = vals if count > 1 else vals[0]
    return out


def parse_physics(data: bytes) -> Dict[str, object]:
    """解析物理包。基础字段必读；扩展字段按包长尽力解析。"""
    out = _read_fields(data, PHYSICS_FIELDS)
    if len(data) >= EXT_PHYSICS_SIZE:
        out.update(_read_fields(data, EXT_PHYSICS_FIELDS))
    return out


def parse_graphic(data: bytes) -> Dict[str, object]:
    return _read_fields(data, GRAPHIC_FIELDS)


def parse_static(data: bytes) -> Dict[str, object]:
    return _read_fields(data, STATIC_FIELDS)


# ---------------------------------------------------------------------------
# SPacketExtended：AC 官方 UDP 扩展包（remote telemetry），52 字节 = 13 个 float32
#   speedKmh         float     偏移  0   车速 km/h
#   worldPosition    float[3]  偏移  4   世界坐标 x/y/z
#   orientation      float[2][3] 偏移 16  朝向（forward/right 各 3 分量，实测校准）
#   velocity         float[3]  偏移 40   速度矢量 x/y/z (m/s)
# 共享内存没有这些字段，只有 UDP 广播会发（部分客户端需握手请求，按包长分派兜底）。
# ---------------------------------------------------------------------------
EXTENDED_SIZE = 52
EXTENDED_FIELDS: list[tuple] = [
    ("speedKmh", "f", 1, 0),
    ("worldPosition", "f", 3, 4),
    ("orientation", "f", 6, 16),
    ("velocity", "f", 3, 40),
]


def parse_extended(data: bytes) -> Dict[str, object]:
    return _read_fields(data, EXTENDED_FIELDS)


def parse_packet(data: bytes) -> Tuple[str, Dict[str, object]]:
    """按包长分派。返回 (kind, fields)，kind ∈ physics/graphic/static/extended/unknown。"""
    n = len(data)
    if n == GRAPHIC_SIZE:
        return "graphic", parse_graphic(data)
    if n in (BASE_PHYSICS_SIZE, EXT_PHYSICS_SIZE, 668):
        return "physics", parse_physics(data)
    if STATIC_SIZE - 60 <= n <= 640:  # 不同 AC 版本 static 长度略有差异
        return "static", parse_static(data)
    if n == EXTENDED_SIZE:
        return "extended", parse_extended(data)
    return "unknown", {}


# ---------------------------------------------------------------------------
# 通道清单：payload 中的固定顺序（float32）。缺失通道填 NaN
# ---------------------------------------------------------------------------
_ARRAY_SUFFIX = {3: ("X", "Y", "Z"), 4: ("FL", "FR", "RL", "RR"),
                 5: ("FR", "FL", "RR", "RL", "CO"), 2: ("F", "R")}


def _expand(name: str, fmt: str, count: int) -> list[str]:
    if count == 1:
        return [name]
    suffixes = _ARRAY_SUFFIX.get(count, [str(i) for i in range(count)])
    return [f"{name}_{s}" for s in suffixes]


CHANNELS: list[str] = []
for _n, _f, _c, _o in PHYSICS_FIELDS:
    CHANNELS.extend(_expand(_n, _f, _c))
for _n, _f, _c, _o in EXT_PHYSICS_FIELDS:
    CHANNELS.extend(_expand(_n, _f, _c))
N_CHANNELS = len(CHANNELS)
CHANNEL_INDEX = {name: i for i, name in enumerate(CHANNELS)}
_NA = float("nan")


def channel_array(fields: Dict[str, object]) -> list[float]:
    """把解析出的 physics dict 转成与 CHANNELS 顺序一致的 float32 值列表。"""
    vals: list[float] = []
    for name, fmt, count, off in PHYSICS_FIELDS + EXT_PHYSICS_FIELDS:
        v = fields.get(name)
        if v is None:
            vals.extend([_NA] * count)
            continue
        if isinstance(v, (tuple, list)):
            vals.extend(float(x) if x is not None else _NA for x in v)
        else:
            vals.append(float(v))
    return vals


# ---------------------------------------------------------------------------
# 合理性检查：扩展字段偏移若不对，值会明显越界，这里兜底告警
# ---------------------------------------------------------------------------
def _sane(name: str, v: float) -> bool:
    if v != v:  # NaN
        return False
    checks = {
        "brakeTemp": (-40.0, 900.0),
        "tyreTempI": (-40.0, 250.0),
        "tyreTempM": (-40.0, 250.0),
        "tyreTempO": (-40.0, 250.0),
        "roadTemp": (-50.0, 120.0),
        "roadGrip": (-5.0, 5.0),
        "frontwingAngle": (-1.0, 1.0),
        "rearwingAngle": (-1.0, 1.0),
        "clutch": (-0.2, 1.2),
        "ersHeatCharging": (-2.0, 2.0),
        "ersIsCharging": (-2.0, 2.0),
        "kersCurrentKJ": (-2000.0, 20000.0),
    }
    lo, hi = checks.get(name, (None, None))
    if lo is None:
        return True
    return lo <= v <= hi


def validate_physics(fields: Dict[str, object]) -> list[str]:
    """返回越界字段名列表（扩展字段专用校验，基础字段理论无误）。"""
    bad = []
    for name, fmt, count, off in EXT_PHYSICS_FIELDS:
        v = fields.get(name)
        if isinstance(v, (tuple, list)):
            for x in v:
                if x is not None and not _sane(name, float(x)):
                    bad.append(name)
                    break
        elif v is not None and not _sane(name, float(v)):
            bad.append(name)
    return bad


# ---------------------------------------------------------------------------
# UDP 接收
# ---------------------------------------------------------------------------
def _local_ip() -> str:
    """本机局域网 IP（AC 广播目标用网卡 IP 而非 127.0.0.1——Windows 10 的
    localhost UDP 问题：游戏绑 0.0.0.0:9996 接收时，发往 127.0.0.1 的包会被
    游戏自己的 socket 吞掉，外部程序收不到；发往网卡 IP 则可正常接收）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # 不实际发包，仅让系统选路由
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


class UdpReceiver:
    """绑定本地端口接收 AC UDP 广播，回调 (kind, fields)。"""

    def __init__(self, port: int = 9996, host: str | None = None,
                 on_packet: Callable[[str, Dict[str, object]], None] = None):
        self.port = port
        self.host = host or _local_ip()   # 默认绑本机网卡 IP（见 _local_ip 注释）
        self.on_packet = on_packet
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 不用 SO_REUSEADDR：防止多个录制实例同时绑定 9996 导致数据被随机劫持
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.25)
        self._thread = threading.Thread(target=self._loop, name="udp-recv", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        warn_log: set[str] = set()
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(2048)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                kind, fields = parse_packet(data)
            except Exception as exc:  # 解析异常不影响接收
                kind, fields = "error", {}
                if "parse" not in warn_log:
                    warn_log.add("parse")
                    print(f"[ac_udp] 解析包失败: {exc} (len={len(data)})")
            if kind == "physics" and fields:
                bad = validate_physics(fields)
                for b in bad:
                    if b not in warn_log:
                        warn_log.add(b)
                        print(f"[ac_udp] 警告: 扩展字段 {b} 数值越界，可能版本布局差异，已忽略该通道")
            if kind == "unknown" and "unknown" not in warn_log:
                warn_log.add("unknown")
                print(f"[ac_udp] 收到无法识别的包长度: {len(data)}B")
            if self.on_packet:
                try:
                    self.on_packet(kind, fields)
                except Exception as exc:
                    tag = f"{type(exc).__name__}: {exc}"
                    if tag not in warn_log:
                        warn_log.add(tag)
                        print(f"[ac_udp] 回调异常（已忽略，仅提示一次）: {tag}")

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


def bind_test(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> Tuple[bool, str]:
    """测试端口能否绑定（用于 doctor）。返回 (是否可用, 描述)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.bind((host, port))
        return True, f"端口 {port} 可绑定（本程序可监听）"
    except OSError as exc:
        return False, f"端口 {port} 绑定失败: {exc}（可能已被其他程序占用）"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# AC Remote Telemetry (RT) 协议：游戏常驻监听 9996，需客户端主动握手+订阅
#   客户端 → AC: 12B <3i> (identifier, version, operation)
#   AC → 客户端: op=0 后发握手响应 408/808B；op=1 后持续推 RTCarInfo 328B
#   RTCarInfo 尾部带世界坐标 carCoordinates X/Y/Z（共享内存没有的 pos 来源）
# 参考：OnTrack(Chrixco) / rickwest ac-remote-telemetry-client 已验证布局。
# ---------------------------------------------------------------------------
RT_OP_HANDSHAKE = 0
RT_OP_SUBSCRIBE_UPDATE = 1
RT_OP_DISMISS = 3
RT_HANDSHAKE_FMT = "<3i"
RT_CAR_INFO_FMT = "<4si3f6b2x3f4i5fif" + "4f" * 14 + "2f3f"
RT_CAR_INFO_SIZE = struct.calcsize(RT_CAR_INFO_FMT)  # 328
RT_RESP_SIZES = (408, 808)


def build_rt_packet(operation: int) -> bytes:
    """构造 12 字节 RT 握手/订阅包。"""
    return struct.pack(RT_HANDSHAKE_FMT, 0, 1, operation)


def parse_rt_car_info(data: bytes) -> Dict[str, object] | None:
    """解析 328B RTCarInfo → 字段 dict；非法包返回 None。"""
    if len(data) != RT_CAR_INFO_SIZE:
        return None
    v = struct.unpack(RT_CAR_INFO_FMT, data)
    if not v[0] or v[0][0:1] != b"a":
        return None
    return {
        "speedKmh": v[2],
        "accG": (v[13], v[12], v[11]),          # 纵, 横, 垂（对齐前端 accG 顺序）
        "lapTime": v[14], "lastLap": v[15], "bestLap": v[16], "lapCount": v[17],
        "gas": v[18], "brake": v[19], "clutch": v[20],
        "rpms": v[21], "steer": v[22], "gear": v[23], "cgHeight": v[24],
        "posNormalized": v[81], "slope": v[82],
        "worldPosition": (v[83], v[84], v[85]),  # 世界坐标 X/Y/Z
    }


class RtTelemetryClient:
    """AC 远程遥测客户端：握手 → 订阅 → 收 RTCarInfo（328B，~100Hz）。

    与游戏 UDP 广播不同，RT 协议无需 udp.ini 配置（游戏常驻监听 9996），
    客户端绑独立端口（不抢 9996），主动握手订阅后游戏回发数据。
    数据经 on_packet("extended", ...) 回调（worldPosition + 差分 orientation/velocity），
    供 recorder 并入实时帧。断线自动重连（超时重发握手）。
    """

    def __init__(self, port: int = 9996,
                 on_packet: Callable[[str, Dict[str, object]], None] = None):
        self.port = port
        self.on_packet = on_packet
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._last_pos: Optional[tuple] = None
        self._last_t = 0.0

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", 0))          # 客户端用随机端口，不抢游戏的 9996
        self._sock.settimeout(0.5)
        target = _local_ip()
        self._target = (target, self.port)
        self._thread = threading.Thread(target=self._loop, name="rt-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.sendto(build_rt_packet(RT_OP_DISMISS), self._target)
        except Exception:
            pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _loop(self) -> None:
        state = "handshake"     # handshake → subscribed
        last_attempt = 0.0
        last_data = time.time()
        while not self._stop.is_set():
            now = time.time()
            # 握手/订阅（首次或响应超时 5s 重来）
            if state == "handshake":
                if now - last_attempt > 1.0:
                    try:
                        self._sock.sendto(build_rt_packet(RT_OP_HANDSHAKE), self._target)
                    except OSError:
                        pass
                    last_attempt = now
                try:
                    data, _addr = self._sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(data) in RT_RESP_SIZES:
                    try:
                        self._sock.sendto(build_rt_packet(RT_OP_SUBSCRIBE_UPDATE), self._target)
                    except OSError:
                        pass
                    state = "subscribed"
                    last_data = time.time()
                continue
            # 已订阅：收 RTCarInfo
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                if time.time() - last_data > 5.0:
                    state = "handshake"     # 数据断流，重新握手订阅
                continue
            except OSError:
                break
            last_data = time.time()
            fields = parse_rt_car_info(data)
            if not fields:
                continue
            pos = fields["worldPosition"]
            # 坐标差分：推出朝向（yaw）与速度矢量（m/s）。
            # 注意：now 要在收包后取（循环开头取的 now 可能和上一帧相同，
            # dt≈0 会被阈值跳过，导致差分永远不输出）
            now = time.time()
            orient, vel = None, None
            if self._last_pos is not None:
                dt = now - self._last_t
                if dt > 0.001:
                    dx = pos[0] - self._last_pos[0]
                    dy = pos[1] - self._last_pos[1]
                    dz = pos[2] - self._last_pos[2]
                    yaw = math.atan2(dz, dx) if (dx or dz) else 0.0
                    orient = (math.cos(yaw), math.sin(yaw), 0.0,
                              -math.sin(yaw), math.cos(yaw), 0.0)  # forward + right
                    vel = (dx / dt, dy / dt, dz / dt)
            self._last_pos = pos
            self._last_t = now
            if self.on_packet:
                try:
                    self.on_packet("extended", {
                        "speedKmh": fields["speedKmh"],
                        "worldPosition": pos,
                        "orientation": orient,
                        "velocity": vel,
                    })
                except Exception:
                    pass
