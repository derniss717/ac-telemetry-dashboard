# -*- coding: utf-8 -*-
"""AC 遥测 - 图形界面（Tkinter）。

双击 exe / `python main.py gui` 打开应用窗口：
  开始录制 / 停止 / 打开仪表盘 / 检查配置 / 生成报告 / 设置端口
后台线程运行 main.run_server，日志实时滚动显示。
"""
from __future__ import annotations

import queue
import sys
import threading
import types
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import config as cfg
import main as main_mod


class _LogFile:
    """把 print 输出转发到队列（跨线程安全，主线程轮询显示）。"""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> int:
        if s.strip():
            self.q.put(s)
        return len(s)

    def flush(self) -> None:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"AC 遥测仪表盘 v{cfg.APP_VERSION}")
        self.geometry("640x420")
        self.minsize(560, 360)

        self._log_q: queue.Queue = queue.Queue()
        self._stop_evt: threading.Event | None = None
        self._server_thread: threading.Thread | None = None
        self._http_port: int = int(cfg.load_config().get("http_port", 8080))
        self._old_stdout = sys.stdout
        self._old_stderr = sys.stderr
        sys.stdout = _LogFile(self._log_q)
        sys.stderr = _LogFile(self._log_q)

        self._build_ui()
        self.after(100, self._drain_log)
        self.after(300, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("欢迎使用 AC 遥测。点击「检查配置」确保 AC 的 UDP 广播已开启，"
                  "进游戏后点「开始录制」。")

    # ---------------- UI ----------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        # 状态栏
        self._status = tk.StringVar(value="状态：未启动")
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self._status, font=("Microsoft YaHei UI", 10)).pack(side="left")
        # 版本号（窗口右上角，点击查看更新日志）
        ver_lbl = ttk.Label(bar, text=f"v{cfg.APP_VERSION}  ↯",
                            foreground="#8b949e", font=("Microsoft YaHei UI", 9), cursor="hand2")
        ver_lbl.pack(side="right")
        ver_lbl.bind("<Button-1>", self._show_changelog)

        # 按钮区
        btns = ttk.Frame(self, padding=(10, 4))
        btns.pack(fill="x")
        self._btn_start = ttk.Button(btns, text="▶ 开始录制", command=self._start)
        self._btn_start.pack(side="left", padx=(0, 6))
        self._btn_stop = ttk.Button(btns, text="■ 停止", command=self._stop, state="disabled")
        self._btn_stop.pack(side="left", padx=(0, 6))
        self._btn_open = ttk.Button(btns, text="📊 打开仪表盘", command=self._open_dash, state="disabled")
        self._btn_open.pack(side="left", padx=(0, 6))
        self._btn_doctor = ttk.Button(btns, text="🔧 检查配置", command=self._doctor)
        self._btn_doctor.pack(side="left", padx=(0, 6))
        self._btn_report = ttk.Button(btns, text="📄 生成报告", command=self._report)
        self._btn_report.pack(side="left", padx=(0, 6))
        self._btn_cfg = ttk.Button(btns, text="⚙ 设置", command=self._settings)
        self._btn_cfg.pack(side="left", padx=(0, 6))

        # 日志区
        logf = ttk.Frame(self, padding=(10, 4))
        logf.pack(fill="both", expand=True)
        self._log_text = tk.Text(logf, height=12, font=("Consolas", 9),
                                 bg="#1e1e1e", fg="#d4d4d4", relief="flat",
                                 state="disabled", wrap="word")
        self._log_text.pack(fill="both", expand=True)
        scr = ttk.Scrollbar(logf, command=self._log_text.yview)
        scr.pack(side="right", fill="y")
        self._log_text.config(yscrollcommand=scr.set)

    # ---------------- 日志 ----------------
    def _show_changelog(self, _evt=None):
        """点击右上角版本号：弹出更新日志窗口（新版本在上）。"""
        win = tk.Toplevel(self)
        win.title(f"更新日志 · v{cfg.APP_VERSION}")
        win.geometry("480x400")
        win.transient(self)
        txt = tk.Text(win, font=("Microsoft YaHei UI", 10), bg="#1e1e1e", fg="#d4d4d4",
                      relief="flat", padx=14, pady=10, wrap="word")
        txt.pack(fill="both", expand=True)
        for ver in sorted(cfg.CHANGELOG.keys(), reverse=True):
            txt.insert("end", f"■ {ver}\n", "ver")
            for item in cfg.CHANGELOG[ver]:
                txt.insert("end", f"   · {item}\n", "item")
            txt.insert("end", "\n")
        txt.tag_config("ver", foreground="#58a6ff", font=("Microsoft YaHei UI", 11, "bold"))
        txt.tag_config("item", foreground="#d4d4d4")
        txt.config(state="disabled")

    def _log(self, msg: str):
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _drain_log(self):
        try:
            while True:
                self._log(self._log_q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def _tick(self):
        """轮询后台线程状态，刷新按钮与状态栏。"""
        if self._server_thread is not None:
            if not self._server_thread.is_alive():
                self._server_thread = None
                self._stop_evt = None
                self._btn_start.config(state="normal")
                self._btn_stop.config(state="disabled")
                if self._status.get().startswith("状态：录制中"):
                    self._status.set("状态：已停止")
        self.after(300, self._tick)

    # ---------------- 动作 ----------------
    def _start(self):
        if self._server_thread and self._server_thread.is_alive():
            return
        try:
            c = cfg.load_config()
            port = int(c.get("udp_port", 9996))
            http_port = int(c.get("http_port", 8080))
            source = c.get("source", "auto")
        except Exception:
            port, http_port, source = 9996, 8080, "auto"
        self._stop_evt = threading.Event()
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._status.set(f"状态：录制中（端口 {http_port}）")

        def on_ready(hp):
            self.after(0, lambda: setattr(self, "_http_port", hp))
            self.after(0, lambda: self._btn_open.config(state="normal"))

        self._server_thread = threading.Thread(
            target=main_mod.run_server,
            args=(port, http_port, source, 0, self._stop_evt, True, on_ready),
            daemon=True)
        self._server_thread.start()
        self._log(f"开始录制：UDP {port} / 仪表盘 http://127.0.0.1:{http_port}")

    def _stop(self):
        if self._stop_evt is not None:
            self._log("正在停止录制…")
            self._stop_evt.set()
            self._status.set("状态：正在停止…")

    def _open_dash(self):
        try:
            webbrowser.open(f"http://127.0.0.1:{self._http_port}")
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _doctor(self):
        self._log("---- 检查 AC 配置 ----")
        t = threading.Thread(target=self._run_doctor, daemon=True)
        t.start()

    def _run_doctor(self):
        try:
            main_mod.cmd_doctor(types.SimpleNamespace(fix=True, auto=True, no_listen=False))
            self._log("---- 配置检查完成 ----")
        except Exception as exc:
            self._log(f"检查出错: {exc}")

    def _report(self):
        self._log("---- 生成赛后报告（最新会话）----")
        t = threading.Thread(target=self._run_report, daemon=True)
        t.start()

    def _run_report(self):
        try:
            rc = main_mod.cmd_analyze(types.SimpleNamespace(
                session="latest", output="", local=False, no_detail=False, no_open=True))
            if rc == 0:
                self._log("报告已生成（reports/ 目录）。")
        except Exception as exc:
            self._log(f"生成出错: {exc}")

    def _settings(self):
        c = cfg.load_config()
        p = simpledialog.askinteger(
            "仪表盘端口", "HTTP 端口（默认 8080，被占用可改 8081）",
            initialvalue=int(c.get("http_port", 8080)), parent=self, minvalue=1024, maxvalue=65535)
        if p is None:
            return
        u = simpledialog.askinteger(
            "UDP 端口", "AC 广播端口（需与游戏 udp.ini 一致，默认 9996）",
            initialvalue=int(c.get("udp_port", 9996)), parent=self, minvalue=1024, maxvalue=65535)
        if u is None:
            return
        c["http_port"] = p
        c["udp_port"] = u
        cfg.save_config(c)
        self._http_port = p
        self._log(f"已保存配置：HTTP {p} / UDP {u}（下次录制生效；若改 UDP 端口需重新检查配置）")

    # ---------------- 退出 ----------------
    def _on_close(self):
        if self._server_thread and self._server_thread.is_alive() and self._stop_evt is not None:
            if not messagebox.askyesno("确认退出", "录制还在进行，确定要退出吗？"):
                return
            self._stop_evt.set()
            self._server_thread.join(timeout=5)
        sys.stdout = self._old_stdout
        sys.stderr = self._old_stderr
        self.destroy()


def run_gui() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run_gui())
