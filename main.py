# -*- coding: utf-8 -*-
"""AC 遥测工具主入口（CLI）。

用法:
  python main.py start                     # 录制 + 实时仪表盘
  python main.py start --duration 60       # 自动停止（测试用）
  python main.py analyze <id|latest>       # 生成赛后分析报告
  python main.py export <id|latest>        # 导出 CSV
  python main.py list                      # 会话列表
  python main.py doctor                    # 检查 AC 配置与 UDP 连通性
  python main.py config                    # 查看/修改配置
"""
from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import config as cfg
from ac_udp import UdpReceiver, bind_test, RtTelemetryClient, _local_ip
from dashboard import Dashboard
from ring_buffer import RingBuffer
from recorder import RecorderThread, SessionManager
from storage import Storage


def _ext_only(on_packet) -> callable:
    """混合模式包装：UDP 广播里只转发扩展包（世界坐标/朝向/速度矢量），
    其余包（physics/graphic/static）由共享内存负责，避免重复处理。"""
    def _wrap(kind: str, fields: dict) -> None:
        if kind == "extended":
            on_packet(kind, fields)
    return _wrap


def _open_storage(args=None) -> Storage:
    c = cfg.load_config()
    data_dir = cfg.resolve_path(c, "data_dir")
    data_dir.mkdir(parents=True, exist_ok=True)
    return Storage(data_dir / "ac.db")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
_cleared_once = False   # 本次进程只清空一次：打开程序时清掉上次记录，进程内再次开始录制保留本次数据


def run_server(port: int, http_port: int, source: str = "auto",
               duration: float = 0, stop_evt: threading.Event | None = None,
               no_browser: bool = False, on_ready=None) -> int:
    """启动录制 + 仪表盘，阻塞直到 stop_evt 被设置（或 duration 秒）。

    GUI 在线程中调用：传外部 stop_evt，停止时 set 即可；
    on_ready(http_port) 在服务启动成功后回调（GUI 可借此刷新状态）。
    """
    global _cleared_once
    c = cfg.load_config()
    data_dir = cfg.resolve_path(c, "data_dir")
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "ac.db"

    storage = Storage(db)
    import os as _os
    _skip_reset = _os.environ.get("AC_SKIP_RESET") == "1"
    if not _cleared_once and not _skip_reset:
        # 每次打开程序全新会话：只清一次（清掉昨天/上次的记录），
        # 进程内停止再开始录制不会再清，本次跑的数据保留
        cleared = storage.reset_all_data()
        _cleared_once = True
        if cleared:
            print(f"[main] 已清空历史数据（{cleared} 圈），本次为全新会话")
    ring = RingBuffer()
    mgr = SessionManager(storage, ring)
    if stop_evt is None:
        stop_evt = threading.Event()
    rec_thread = RecorderThread(storage, ring, mgr, stop_evt)
    dash = Dashboard(ring, storage, mgr, http_port=http_port)

    # ---- 数据源：auto 优先共享内存（稳定、无需 UDP 广播），否则 UDP ----
    from shared_mem import SharedMemReader
    reader = None
    if source in ("auto", "shared"):
        if SharedMemReader.available() or source == "shared":
            reader = SharedMemReader(mgr.on_packet)
            try:
                reader.start()
            except RuntimeError as exc:
                if source == "shared":
                    print(f"[main] 共享内存不可用: {exc}")
                    return 1
                reader = None
            else:
                print("[main] 数据源: 共享内存（无需 UDP 广播）")
                # 混合模式：RT 遥测客户端（游戏常驻监听 9996，握手订阅后回发
                # RTCarInfo，含世界坐标/朝向/速度矢量——共享内存没有的位置数据）
                try:
                    ext_recv = RtTelemetryClient(port=port, on_packet=mgr.on_packet)
                    ext_recv.start()
                    print(f"[main] RT 遥测订阅: {_local_ip()}:{port}（世界坐标/朝向/速度矢量）")
                except OSError as exc:
                    print(f"[main] RT 遥测不可用: {exc}，世界坐标/朝向/速度矢量卡片将无数据")
    if reader is None:
        if source == "shared":
            print("[main] 共享内存不可用，请先启动游戏")
            return 1
        recv = UdpReceiver(port=port, on_packet=mgr.on_packet)
        try:
            recv.start()
        except OSError as exc:
            print(f"[main] UDP 启动失败: {exc}")
            print("端口被占用？换端口: --udp-port 9997 --http-port 8081")
            return 1
        print(f"[main] 数据源: UDP {port}（需在游戏文档目录 cfg/udp.ini 开启广播）")
    if reader is not None:
        recv = reader  # 共享内存模式；UDP 模式 recv 已在上面定义

    try:
        rec_thread.start()
        dash.start()
        dash.start_ws(ws_port=int(c.get("ws_port", 8081)))   # WebSocket 实时推送（前端优先通道）
    except OSError as exc:
        print(f"[main] 启动失败: {exc}")
        return 1

    print("[main] 进入游戏开始跑圈。")
    if on_ready:
        try:
            on_ready(http_port)
        except Exception:
            pass
    if not no_browser:
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{http_port}")
        except Exception:
            pass
    t0 = time.time()
    try:
        while not stop_evt.is_set():
            mgr.check_gap(time.time())
            if duration and time.time() - t0 > duration:
                print(f"[main] 到达测试时长 {duration}s，自动停止")
                break
            time.sleep(0.25)
    finally:
        stop_evt.set()
        mgr.finalize()
        rec_thread.join(timeout=5)
        recv.stop()
        dash.stop()
        dash.stop_ws()   # 释放 8081，否则 GUI 停止→再开始时新 WS 起不来
        storage.close()
        print("[main] 录制已停止。赛后报告: python main.py analyze latest")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    c = cfg.load_config()
    port = args.udp_port or int(c["udp_port"])
    http_port = args.http_port or int(c["http_port"])
    source = args.source or c.get("source", "auto")
    stop_evt = threading.Event()

    def shutdown(_sig, _frm):
        print("\n正在停止录制…")
        stop_evt.set()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)

    return run_server(port=port, http_port=http_port, source=source,
                      duration=args.duration, stop_evt=stop_evt,
                      no_browser=args.no_browser)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    storage = _open_storage(args)
    sessions = storage.list_sessions()
    if not sessions:
        print("还没有会话。先运行: python main.py start")
        return 0
    rows = []
    for s in sessions:
        laps = storage.list_laps(s["id"])
        valid = [l for l in laps if l["is_valid"] and l["total_ms"]]
        best = min((l["total_ms"] for l in valid), default=0)
        nframes = storage.frame_count(s["id"])
        rows.append({
            "id": s["id"], "name": s["name"], "car": s["car"], "track": s["track"],
            "start": time.strftime("%m-%d %H:%M:%S", time.localtime(s["start_ts"])),
            "laps": len(laps), "valid": len(valid), "best_ms": best, "frames": nframes,
        })
    storage.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"{'ID':>3}  {'名称':<20} {'车型':<22} {'赛道':<16} {'开始':<14}"
          f" {'圈(有效)':>8} {'最佳':>10} {'帧数':>9}")
    for r in rows:
        best = f"{r['best_ms']/1000:.1f}s" if r["best_ms"] else "-"
        print(f"{r['id']:>3}  {r['name']:<20} {r['car'][:22]:<22} {r['track'][:16]:<16}"
              f" {r['start']:<14} {r['laps']}({r['valid']})  {best:>10} {r['frames']:>9}")
    return 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def cmd_export(args: argparse.Namespace) -> int:
    import ac_udp
    storage = _open_storage(args)
    sid = _resolve_session_id(storage, args.session)
    if sid is None:
        return 1
    frames = storage.frames_by_session(sid)
    out = Path(args.output) if args.output else (
        cfg.resolve_path(cfg.load_config(), "reports_dir") / f"session_{sid}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    header = ["frame_id", "lap_id", "t", "dist", "speed_kmh", "gear", "rpms",
              "lap_ms", "sector"] + ac_udp.CHANNELS
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for fr in frames:
            import numpy as np
            ch = np.frombuffer(fr["payload"], dtype="<f4")
            row = [fr["id"], fr["lap_id"] if fr["lap_id"] else "", fr["t"],
                   fr["dist"], fr["speed_kmh"], fr["gear"], fr["rpms"],
                   fr["lap_ms"], fr["sector"]]
            row += ["" if v != v else round(float(v), 4) for v in ch.tolist()]
            w.writerow(row)
    storage.close()
    print(f"已导出 {len(frames)} 帧 → {out}")
    return 0


def _resolve_session_id(storage: Storage, session: str):
    if session == "latest":
        sessions = storage.list_sessions()
        if not sessions:
            print("还没有会话。")
            return None
        return sessions[-1]["id"]
    return int(session)


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def cmd_analyze(args: argparse.Namespace) -> int:
    import analyze
    argv = [args.session]
    if args.output:
        argv += ["--output", args.output]
    if args.local:
        argv.append("--local")
    if args.no_detail:
        argv.append("--no-detail")
    if args.no_open:
        argv.append("--no-open")
    return analyze.main(argv)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    c = cfg.load_config()
    port = int(c["udp_port"])
    ok = True
    print("=== AC UDP 配置检查 ===")
    ini = cfg.find_udp_ini()
    if ini is None:
        print("[!] 未找到 udp.ini（Assetto Corsa 的 Documents 配置不存在）")
        target = cfg.candidate_udp_ini_paths()[0]
        if args.fix or args.auto:
            cfg.write_udp_ini(target, port)
            print(f"[+] 已写入: {target}")
        else:
            print(f"    将自动创建: {target}\n    运行 `python main.py doctor --fix` 自动写入")
            ok = False
    else:
        info = cfg.read_udp_ini(ini)
        print(f"[i] udp.ini 位于: {ini}")
        print(f"    ENABLED={info['enabled']}  IP={info['ip']}  PORT={info['port']}")
        need = []
        if info["enabled"] is not True:
            need.append("ENABLED=1")
        if info["port"] != str(port):
            need.append(f"PORT={port}")
        if need:
            if args.fix or args.auto:
                cfg.write_udp_ini(ini, port)
                print(f"[+] 已修复 ({', '.join(need)})")
            else:
                print(f"[!] 需要修改: {', '.join(need)}"
                      f" → 运行 `python main.py doctor --fix`")
                ok = False
    # ENABLE_DEV_APPS
    ac_ini = cfg.ensure_ac_ini_dev_apps()
    if ac_ini:
        print(f"[!] {ac_ini} 中 ENABLE_DEV_APPS 不是 1，部分版本需要改")
        ok = False

    print("\n=== UDP 端口 ===")
    avail, msg = bind_test(port)
    print(f"[{'OK' if avail else '!'}] {msg}")
    if not avail:
        ok = False

    print("\n=== 游戏进程 ===")
    running = _ac_running()
    print(f"[{'OK' if running else 'i'}] AC {'运行中' if running else '未运行（等进入游戏后再测）'}")
    if running and not args.no_listen:
        print("\n=== 收包测试（4 秒）===")
        n = _count_packets(port)
        print(f"[{'OK' if n > 0 else '!'}] 收到 {n} 个包"
              + ("（一切正常，直接 `python main.py start` 即可）" if n else "（没收到包，检查 udp.ini 与 ENABLE_DEV_APPS）"))
        ok = ok and n > 0
    print("\n=== 通道 ===")
    import ac_udp
    print(f"共 {ac_udp.N_CHANNELS} 个通道，含 悬架行程×4 / 底板高度×2 / KERS电量 / 油量 / 胎温 等")
    if not ok:
        print("\n检查结果有需要处理的项目（可用 --fix 自动修复 udp.ini）")
        return 1
    print("\n检查通过 ✔")
    return 0


def _ac_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        return any(k in out for k in ("acs.exe", "assettocorsa.exe", "assettocorsa"))
    except Exception:
        return False


def _count_packets(port: int, seconds: float = 4.0) -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(0.2)
    n = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            s.recvfrom(2048)
            n += 1
        except socket.timeout:
            pass
    s.close()
    return n


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def cmd_config(args: argparse.Namespace) -> int:
    c = cfg.load_config()
    if args.set:
        for kv in args.set:
            k, _, v = kv.partition("=")
            if k in c:
                c[k] = int(v) if v.lstrip("-").isdigit() else v
                print(f"[config] {k} = {c[k]}")
            else:
                print(f"[config] 未知配置项: {k}")
        cfg.save_config(c)
    print(json.dumps(c, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    # 无参数（双击 exe）或 `gui` → 打开图形界面
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] == "gui":
        from gui import run_gui
        return run_gui()

    ap = argparse.ArgumentParser(prog="main", description="AC 遥测：实时仪表盘 + 赛后分析")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gui", help="打开图形界面")
    def _cmd_gui(_args):
        from gui import run_gui
        return run_gui()
    p.set_defaults(func=_cmd_gui)

    p = sub.add_parser("start", help="录制 + 实时仪表盘")
    p.add_argument("--udp-port", type=int, default=0)
    p.add_argument("--http-port", type=int, default=0)
    p.add_argument("--duration", type=float, default=0, help="测试用：自动停止秒数")
    p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    p.add_argument("--source", choices=["auto", "shared", "udp"], default="auto",
                   help="数据源：auto=共享内存优先，shared=仅共享内存，udp=仅UDP广播")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("list", help="会话列表")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export", help="导出会话为 CSV")
    p.add_argument("session", help="会话 id 或 latest")
    p.add_argument("--output", "-o", default="")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("analyze", help="生成赛后分析报告")
    p.add_argument("session", help="会话 id 或 latest")
    p.add_argument("--output", "-o", default="")
    p.add_argument("--local", action="store_true", help="plotly 内联（离线）")
    p.add_argument("--no-detail", action="store_true")
    p.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("doctor", help="检查 AC 配置与 UDP 连通性")
    p.add_argument("--fix", action="store_true", help="自动修复 udp.ini")
    p.add_argument("--auto", action="store_true", help="非交互（等价 --fix）")
    p.add_argument("--no-listen", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="查看/修改配置")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_config)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    # PyInstaller 控制台默认 cp1252，中文输出会崩；cp1252/ascii 强制 UTF-8，
    # 其余保留原编码（配合 .bat 的 chcp 65001 最稳），全部 errors=replace 兜底
    for _s in (sys.stdout, sys.stderr):
        try:
            _enc = (_s.encoding or "").lower()
            _s.reconfigure(errors="replace",
                           encoding="utf-8" if "cp1252" in _enc or "ascii" in _enc else None)
        except Exception:
            pass
    sys.exit(main())
