# -*- coding: utf-8 -*-
"""离线自测：模拟发包 → ac_udp 解析 → 校验关键字段（不依赖 numpy/plotly）。"""
import sys, time
sys.path.insert(0, __file__ and "." or ".")
import ac_udp
import simulate

fail = []

def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        fail.append(name)

# 1) 模拟 static
st = simulate.build_static()
print(f"static 包长 {len(st)}")
kind, f = ac_udp.parse_packet(st)
check("static 识别", kind == "static" and f.get("carModel") == "formula_e_2024_ev",
      f"{kind} {f.get('carModel')}")
check("static track", f.get("track") == "ks_monza", f.get("track"))
check("static sectorCount", f.get("sectorCount") == 3, f.get("sectorCount"))
check("static airTemp", abs((f.get("airTemp") or 0) - 26.0) < 1e-6, f.get("airTemp"))

# 2) 模拟 graphic（圈边界）
g = simulate.build_graphic(0.0, 5.0, 2, 85.0, 84_500, 83_900, {3: 28_100}, 2 * 4000 + 1234.5, 5)
print(f"graphic 包长 {len(g)}")
kind, f = ac_udp.parse_packet(g)
check("graphic 识别", kind == "graphic", kind)
check("completedLaps", f.get("completedLaps") == 2, f.get("completedLaps"))
check("iCurrentTime", f.get("iCurrentTime") == 5000, f.get("iCurrentTime"))
check("iLastTime", f.get("iLastTime") == 84500, f.get("iLastTime"))
check("sectorIndex", f.get("currentSectorIndex") == 0, f.get("currentSectorIndex"))
check("distanceTraveled", abs((f.get("distanceTraveled") or 0) - 9234.5) < 0.01,
      f.get("distanceTraveled"))
check("status live", f.get("status") == 2, f.get("status"))

# 3) 模拟 physics
p = simulate.build_physics(10.0, 2000.0, 1, 85.0, 0.8, 0.75)
print(f"physics 包长 {len(p)}")
kind, f = ac_udp.parse_packet(p)
check("physics 识别", kind == "physics", kind)
speed = f.get("speedKmh")
check("speed 合理", speed and 40 < speed < 220, speed)
check("gas/brake 在 0..1", 0 <= f.get("gas", 0) <= 1 and 0 <= f.get("brake", 0) <= 1,
      f"gas={f.get('gas'):.2f} brake={f.get('brake'):.2f}")
check("gear 合理", 1 <= f.get("gear", 0) <= 6, f.get("gear"))
check("rpms 合理", 1500 < f.get("rpms", 0) < 12000, f.get("rpms"))
susp = f.get("suspensionTravel")
check("suspensionTravel[4] 合理", susp and all(0.0 < s < 0.2 for s in susp), susp)
rh = f.get("rideHeight")
check("rideHeight[2] 合理", rh and all(0.02 < h < 0.2 for h in rh), rh)
check("kersCharge 合理", 0 < f.get("kersCharge", 0) < 1, f.get("kersCharge"))
check("fuel 合理", 0 < f.get("fuel", 0) < 1, f.get("fuel"))

# 4) channel_array 顺序与长度
arr = ac_udp.channel_array(f)
check(f"channel_array 长度 {len(arr)} == {ac_udp.N_CHANNELS}", len(arr) == ac_udp.N_CHANNELS)
i = ac_udp.CHANNEL_INDEX["suspensionTravel_FL"]
check("channel 索引映射正确", abs(arr[i] - susp[0]) < 1e-6)

# 5) 未知长度
kind, _ = ac_udp.parse_packet(b"\x00" * 999)
check("未知长度降级", kind == "unknown")

print()
if fail:
    print(f"共 {len(fail)} 项失败: {fail}")
    sys.exit(1)
print("全部校验通过 ✔")
