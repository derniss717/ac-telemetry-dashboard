# -*- coding: utf-8 -*-
"""模拟 AC UDP 广播：不启动游戏即可测试 录制→仪表盘→报告 全流程。

用法: python simulate.py [--laps 3] [--port 9996] [--lap-time 85]
"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import time

PHY_FMT = "<i f f f i i f f 3f 3f 4f 4f 4f 4f 4f 4f 4f 4f 4f f f f f f f 5f i i f f f i 2f f f f"
assert struct.calcsize(PHY_FMT) == 288, f"physics size {struct.calcsize(PHY_FMT)}"

GRA_FMT = "<3i 15H 15H 15H 15H 5i 2f 4i 33H 2f 3f f 3i f"
assert struct.calcsize(GRA_FMT) == 282, f"graphic size {struct.calcsize(GRA_FMT)}"

STA_FMT = "<15H 15H i i 33H 33H 33H 33H 33H i f f i f 4f 4f f f f b f f f b f b b"
assert struct.calcsize(STA_FMT) == 482, f"static size {struct.calcsize(STA_FMT)}"


def wstr(s: str, n: int) -> bytes:
    b = s.encode("utf-16-le")
    return b + b"\x00" * (n * 2 - len(b))


def build_static() -> bytes:
    return struct.pack(
        STA_FMT,
        *[ord(c) for c in "SM_1.0".ljust(15, "\x00")[:15]],
        *[ord(c) for c in "1.16.4".ljust(15, "\x00")[:15]],
        1, 1,
        *[ord(c) for c in "formula_e_2024_ev".ljust(33, "\x00")[:33]],
        *[ord(c) for c in "ks_monza".ljust(33, "\x00")[:33]],
        *[ord(c) for c in "Driver".ljust(33, "\x00")[:33]],
        *[ord(c) for c in "".ljust(33, "\x00")[:33]],
        *[ord(c) for c in "ACsim".ljust(33, "\x00")[:33]],
        3, 340.0, 480.0, 11000, 90.0,
        0.10, 0.10, 0.10, 0.10, 0.32, 0.32, 0.32, 0.32, 2.0,
        26.0, 32.0, False, 0.5, 0.5, 0.0, False, 0.0, False, False,
    )


def build_physics(t: float, d: float, laps_done: int, lap_time: float,
                  kers: float, fuel: float) -> bytes:
    L = 4000.0
    x = (d % L) / L
    v = 42.0 + 148.0 * (0.5 + 0.5 * math.cos(2 * math.pi * 3 * x)) ** 2  # km/h
    v = max(40.0, v)
    eps = 4.0
    vp = 42.0 + 148.0 * (0.5 + 0.5 * math.cos(2 * math.pi * 3 * ((d + eps) % L) / L)) ** 2
    vm = 42.0 + 148.0 * (0.5 + 0.5 * math.cos(2 * math.pi * 3 * ((d - eps) % L) / L)) ** 2
    dv_dd = (vp - vm) / (2 * eps)          # km/h per meter
    gas = max(0.0, min(1.0, 0.18 + 9.0 * max(0.0, dv_dd)))
    brake = max(0.0, min(1.0, -9.0 * min(0.0, dv_dd)))
    gear = min(6, max(1, int(v / 32) + 1))
    rpm = int(2200 + 6200 * (v / 190.0) ** 1.3)
    speed_ms = v / 3.6
    steer = 0.18 * math.sin(2 * math.pi * 3 * x + math.pi / 2)
    acc_x = dv_dd * speed_ms / 9.81
    acc_y = 1.15 * math.sin(2 * math.pi * 3 * x)
    susp = [0.026 + 0.012 * abs(math.sin(0.02 * d + i * 1.7)) for i in range(4)]
    ride_f = max(0.02, 0.056 - 0.00009 * v + 0.004 * math.sin(0.03 * d))
    ride_r = ride_f + 0.008
    # 模拟 DRS：大直道段（x 接近 0.5）开启，验证前端红色高亮
    drs = 1.0 if 0.35 < x < 0.55 else 0.0
    load = [3500 + 800 * acc_x + 600 * (1 if i in (2, 3) else -1) * acc_y
            for i in range(4)]
    slip = [0.04 * brake * (1 if i in (0, 1) else -1) + 0.02 * math.sin(0.05 * d + i)
            for i in range(4)]
    tyre = [70 + 26 * v / 190 + 5 * math.sin(0.03 * d + i * 2) for i in range(4)]
    kers_in = 0.6 if gas > 0.5 else 0.0
    wear = [min(0.5, 0.02 + 0.0015 * (t % 120) + 0.004 * i) for i in range(4)]
    base = struct.pack(
        PHY_FMT,
        1, gas, brake, fuel, gear, rpm, steer, v,
        speed_ms, 0.0, 0.0, acc_x, acc_y, 0.02,
        *slip, *load,
        24.0, 24.0, 24.0, 24.0,            # wheelsPressure
        180.0, 180.0, 180.0, 180.0,        # wheelAngularSpeed
        *wear,                             # tyreWear (0~1)
        0.0, 0.0, 0.0, 0.0,                # tyreDirtyLevel
        *tyre,                             # tyreCoreTemperature
        -0.06, -0.06, -0.05, -0.05,        # camberRAD
        *susp,                             # suspensionTravel
        drs, 0.0, 0.0, 0.0, 0.0, 0.0,     # drs tc heading pitch roll cgHeight
        0.0, 0.0, 0.0, 0.0, 0.0,           # carDamage[5]
        0, 0,                              # numberOfTyresOut pitLimiterOn
        0.0, kers, kers_in,                # abs kersCharge kersInput
        0,                                 # autoShifterOn
        ride_f, ride_r,                    # rideHeight[2]
        1.2, 0.0, 1.205,                   # turboBoost ballast airDensity
    )
    # 扩展段（400B，AC 1.5+）：刹车盘温/胎温三层/ERS 等
    brake_t = [90 + 220 * brake + 20 * math.sin(d * 0.01 + i) for i in range(4)]
    tyre_i = [t_ - 6 + 2 * math.sin(d * 0.02 + i) for i, t_ in enumerate(tyre)]
    tyre_m = [t_ + 4 + 2 * math.sin(d * 0.02 + i) for i, t_ in enumerate(tyre)]
    tyre_o = [t_ + 10 + 2 * math.sin(d * 0.02 + i) for i, t_ in enumerate(tyre)]
    ext = struct.pack(
        "<f f f f i i 4f f 4f 4f 4f i i f f f",
        28.0, 1.0, 0.08, 0.12,   # roadTemp roadGrip frontwingAngle rearwingAngle
        1, 0,                    # drsAvailable drsEnabled
        *brake_t,                # brakeTemp[4]
        1.0,                     # clutch
        *tyre_i, *tyre_m, *tyre_o,  # 胎温 I/M/O
        2, 0,                    # ersPowerLevel ersRecoveryLevel
        0.2, 0.0,                # ersHeatCharging ersIsCharging
        kers * 900.0,            # kersCurrentKJ
    )
    return base + ext


def build_graphic(lap_start_t: float, t: float, laps_done: int, lap_time: float,
                  last_lap_ms: int, best_lap_ms: int, sector_times: dict,
                  total_dist: float, laps_total: int) -> bytes:
    cur = max(0, int((t - lap_start_t) * 1000))
    x = ((total_dist % 4000.0) / 4000.0)
    sector = 0 if x < 1 / 3 else (1 if x < 2 / 3 else 2)
    # lastSectorTime = 刚完成扇区的段用时（真实 AC 语义，官方 split）：
    #   sector_times 存累计（1=S1线,2=S2线,3=整圈），换算为段用时
    if sector > 0:
        prev_cum = sector_times.get(sector - 1, 0) if sector > 1 else 0
        last_sec = max(0, sector_times.get(sector, 0) - prev_cum)
    else:
        last_sec = max(0, sector_times.get(3, 0) - sector_times.get(2, 0))
    return struct.pack(
        GRA_FMT,
        1, 2, 0,  # packetId, status=live, session=practice
        *[ord(c) for c in "00:00:00".ljust(15, "\x00")[:15]],
        *[ord(c) for c in "00:00:00".ljust(15, "\x00")[:15]],
        *[ord(c) for c in "00:00:00".ljust(15, "\x00")[:15]],
        *[ord(c) for c in "".ljust(15, "\x00")[:15]],
        laps_done, 1, cur, last_lap_ms, best_lap_ms,
        600.0, total_dist, 0, sector, last_sec, laps_total,
        *[ord(c) for c in "SM".ljust(33, "\x00")[:33]],
        1.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0.98,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="模拟 AC UDP 遥测广播")
    ap.add_argument("--laps", type=int, default=3)
    ap.add_argument("--port", type=int, default=9996)
    ap.add_argument("--lap-time", type=float, default=85.0)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(("127.0.0.1", args.port))
    print(f"[sim] 向 127.0.0.1:{args.port} 广播模拟遥测"
          f"（{args.laps} 圈 × {args.lap_time:.0f}s）")

    sock.send(build_static())
    L = 4000.0
    t0 = time.time()
    lap_start = t0
    laps_done = 0
    kers = 1.0
    fuel = 0.80
    last_lap_ms = 0
    best_lap_ms = 0
    sector_times: dict[int, int] = {}
    sector_seen = -1
    prev_t = t0

    while laps_done < args.laps:
        t = time.time()
        dt = t - prev_t
        prev_t = t
        lap_time = t - lap_start
        d = laps_done * L + (lap_time / args.lap_time) * L
        x = (d % L) / L
        cur_sec = 0 if x < 1 / 3 else (1 if x < 2 / 3 else 2)
        # 扇区推进: 进入扇区1 → S1 完成; 进入扇区2 → S2 完成; 过线回到0 → S3 完成
        if cur_sec != sector_seen:
            if cur_sec == 0:
                sector_times[3] = int(last_lap_ms if last_lap_ms > 0 else lap_time * 1000)
            else:
                sector_times[cur_sec] = int(lap_time * 1000)
            sector_seen = cur_sec
        # 电量/油量模型
        gas = 0.5 if (0.5 + 0.5 * math.cos(2 * math.pi * 3 * x)) ** 2 > 0.6 else 0.3
        if gas > 0.5:
            kers = max(0.15, kers - 0.012 * dt)
        else:
            kers = min(1.0, kers + 0.006 * dt)
        fuel = max(0.02, fuel - 0.00045 * dt)

        total_dist = d
        sock.send(build_physics(t, d, laps_done, args.lap_time, kers, fuel))
        sock.send(build_graphic(lap_start, t, laps_done, args.lap_time,
                                last_lap_ms, best_lap_ms, sector_times,
                                total_dist, args.laps))
        if lap_time >= args.lap_time:
            last_lap_ms = int(lap_time * 1000)
            sector_times[3] = last_lap_ms   # S3 = 刚完成圈的总用时（过线包要用）
            if best_lap_ms == 0 or last_lap_ms < best_lap_ms:
                best_lap_ms = last_lap_ms
            laps_done += 1
            print(f"[sim] 圈 {laps_done} 完成: {last_lap_ms/1000:.1f}s"
                  f"  (KERS {kers*100:.0f}% 油量 {fuel*100:.0f}%)")
            lap_start = t
        time.sleep(0.005)  # ~200Hz

    # 补发最后一圈的过线包，让录制端正常闭合最后一圈
    for _ in range(20):
        t = time.time()
        sock.send(build_graphic(lap_start, t, laps_done, args.lap_time,
                                last_lap_ms, best_lap_ms, sector_times,
                                laps_done * L + 1.0, args.laps))
        time.sleep(0.02)
    print("[sim] 模拟结束，停止发送 3.5s（触发会话段关闭）…")
    time.sleep(3.5)


if __name__ == "__main__":
    main()
