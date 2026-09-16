# -*- coding: utf-8 -*-
"""配置读写 + AC udp.ini 检测/写入（doctor/config 命令）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "ac-telemetry"

APP_VERSION = "2.0"   # 程序版本号：GUI 右上角显示；发新版前改这里再重新打包

# 更新日志（GUI 点版本号查看；新版本发布时在顶部插入新条目）
CHANGELOG: dict = {
    "v2.0": [
        "[新增] 前端重构为 Vue3 + Vite 现代化架构（替代 1.2 单文件 app.js）",
        "[新增] WebSocket 实时推送（替代 200ms HTTP 轮询，数据刷新丝滑无延迟）",
        "[新增] 移动端适配（响应式布局，手机浏览器可直接访问仪表盘）",
        "[新增] 图表鼠标悬停十字线联动（MoTeC 风格：鼠标在任意卡片滑动，顶部数字条实时显示该位置数据）",
        "[新增] 共享内存直读模式（无需 UDP 广播配置，游戏启动即可连接）",
        "[新增] RT 遥测订阅（世界坐标/朝向/速度矢量，为赛道轨迹图准备）",
        "[新增] SQLite 读写分离（录制写入不阻塞仪表盘查询，高频数据刷新不再卡顿）",
        "[优化] 数据库采用 WAL 模式 + busy_timeout（写入不再排队阻塞读查询）",
        "[优化] 帧数据批量裁剪（摊还 O(1)，200Hz 高频写入下 CPU 占用大幅降低）",
        "[优化] 图表渲染：rAF 节流 + 轻量重绘（不再每次鼠标移动全量更新图表）",
        "[优化] 双界面统一：双击 bat 直接使用新版界面（8080 同时服务 HTTP + 前端）",
        "[修复] DRS 红线染色功能失效（v2.0 迁移后数据源引用错误，DRS 区间不再标红）",
        "[修复] WebSocket 缺失模块时线程裸崩溃（现在优雅降级回 HTTP 轮询）",
        "[修复] WS 服务无法停止（GUI 停止→再开始端口占用报错）",
        "[修复] 点击图例切换曲线显隐误触卡片放大",
        "[修复] hover 时上一圈/最佳圈/轮胎配方显示 “--”（改为保留实时值）",
    ],
    "v1.2": [
        "[新增] 图表放大后滚轮缩放 + 拖拽平移",
        "[新增] 报告轮胎磨损曲线图",
        "[新增] 刹车盘温、轮胎磨损数据卡片",
        "[新增] 报告弯角表刹车到开油时间",
        "[新增] 赞德沃特赛道弯角数据",
        "[新增] 每次启动程序为全新会话，自动清空历史记录",
        "[修复] 蒙扎赛道弯角数据：官方命名、补漏弯角",
        "[修复] 报告标题与信息对齐",
        "[修复] 胎温/刹车盘温/磨损数据自检测：数据可靠才显示",
        "[优化] 回放速度大幅提升，同圈重复查看秒开",
    ],
    "v1.1": [
        "修复：全新环境下数据表缺列导致跑圈不记录、看不到圈数的问题",
        "新增：右上角版本号显示 + 点击查看更新日志",
    ],
    "v1.0": [
        "首个正式版发布",
        "实时遥测仪表盘：车速/转速/档位/G值/胎温/滑移率/涡轮/ERS/方向盘",
        "自动圈记录 + S1/S2/S3 分段 + 理论最快圈",
        "单圈报告下载（文件名=赛道+圈速）",
        "两圈对比分析：速度/油门/刹车/涡轮/ERS放电/纵向G/横向G/方向盘/秒差",
        "任意圈回放",
        "数据自动清理（保留最近 8 个会话，防越用越卡）",
        "卡片悬停光效 + 点击放大细看",
    ],
}
DEFAULT_CONFIG = {
    "udp_port": 9996,          # AC 广播端口（udp.ini 中 PORT 需一致）
    "http_port": 8080,         # 实时仪表盘端口
    "ws_port": 8081,           # WebSocket 实时推送端口（前端优先通道）
    "data_dir": "data",        # 会话数据库目录
    "reports_dir": "reports",  # 报告输出目录
    "session_name": "",        # 留空则自动用时间命名
}

UDP_INI_TEMPLATE = """\
[UDP]
ENABLED=1
IP=127.0.0.1
PORT={port}
"""


def project_dir() -> Path:
    """数据/配置目录（config.json、data/、reports/）。

    PyInstaller 打包后返回 exe 所在目录（持久化），源码运行返回脚本目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """只读资源目录（web/ 静态文件等）。

    PyInstaller onefile 打包后资源解压在 _MEIPASS 临时目录。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def config_path() -> Path:
    return project_dir() / "config.json"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"[config] 读取 config.json 失败: {exc}，使用默认配置")
    return cfg


def save_config(cfg: dict) -> None:
    config_path().write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve_path(cfg: dict, key: str) -> Path:
    p = Path(cfg.get(key, DEFAULT_CONFIG[key]))
    if not p.is_absolute():
        p = project_dir() / p
    return p


# ---------------------------------------------------------------------------
# AC 侧配置
# ---------------------------------------------------------------------------
def _real_documents_dirs() -> list[Path]:
    """探测真实的"我的文档"目录（支持盘符重定向/OneDrive）。"""
    dirs: list[Path] = []
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as k:
            v, _ = winreg.QueryValueEx(k, "Personal")
        if v:
            v = v.replace("%USERPROFILE%", str(Path.home()))
            p = Path(v)
            if p.exists():
                dirs.append(p)
    except Exception:
        pass
    return dirs


def candidate_udp_ini_paths() -> list[Path]:
    """可能存在的 udp.ini 位置（真实文档目录优先，含 OneDrive/常见重定向）。"""
    cands: list[Path] = []
    doc_dirs: list[Path] = []
    for d in _real_documents_dirs():
        if d not in doc_dirs:
            doc_dirs.append(d)
    home = Path.home()
    for d in [home / "Documents", home / "OneDrive" / "Documents",
              home / "OneDrive" / "文档", home / "OneDrive - 个人" / "Documents",
              home / "OneDrive - Personal" / "Documents"]:
        if d not in doc_dirs:
            doc_dirs.append(d)
    for d in doc_dirs:
        cands.append(d / "Assetto Corsa" / "cfg" / "udp.ini")
    return cands


def find_udp_ini() -> Path | None:
    for p in candidate_udp_ini_paths():
        if p.exists():
            return p
    return None


def read_udp_ini(path: Path) -> dict:
    info = {"exists": False, "enabled": None, "ip": None, "port": None, "raw": ""}
    if not path.exists():
        return info
    info["exists"] = True
    try:
        info["raw"] = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        info["raw"] = ""
    for line in info["raw"].splitlines():
        line = line.strip()
        if line.lower().startswith("enabled="):
            info["enabled"] = line.split("=", 1)[1].strip() == "1"
        elif line.lower().startswith("ip="):
            info["ip"] = line.split("=", 1)[1].strip()
        elif line.lower().startswith("port="):
            info["port"] = line.split("=", 1)[1].strip()
    return info


def write_udp_ini(path: Path, port: int) -> None:
    """写入/更新 udp.ini，保留原有内容只改 [UDP] 段。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ""
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw = ""
    if "[UDP]" in raw:
        # 原位重写整个 [UDP] 段（跳过原段所有行，其他段保持不动），保证段头只出现一次
        lines = raw.splitlines()
        in_udp = False
        out = []
        for ln in lines:
            stripped = ln.strip()
            is_section = stripped.startswith("[") and stripped.endswith("]")
            if is_section:
                if in_udp:
                    # 原 UDP 段结束（遇到下一个段头）：补写重写后的 UDP 段
                    out.append("[UDP]")
                    out.append("ENABLED=1")
                    out.append("IP=127.0.0.1")
                    out.append(f"PORT={port}")
                    in_udp = False
                in_udp = stripped.lower() == "[udp]"
                if in_udp:
                    continue  # 跳过原 UDP 段头（稍后重写）
                out.append(ln)
            elif in_udp:
                continue  # 跳过原 UDP 段内的键
            else:
                out.append(ln)
        if in_udp:
            # 原文件以 UDP 段结尾：段尾补写
            out.append("[UDP]")
            out.append("ENABLED=1")
            out.append("IP=127.0.0.1")
            out.append(f"PORT={port}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    else:
        path.write_text(UDP_INI_TEMPLATE.format(port=port), encoding="utf-8")


def _ac_install_candidates() -> list[Path]:
    """常见 Steam 库下的 AC 安装目录（system/cfg/assetto_corsa.ini）。"""
    roots = [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa"),
        Path.home() / "SteamLibrary" / "steamapps" / "common" / "assettocorsa",
    ]
    for drive in "DEFGH":
        roots.append(Path(f"{drive}:\\SteamLibrary\\steamapps\\common\\assettocorsa"))
        roots.append(Path(f"{drive}:\\Steam\\steamapps\\common\\assettocorsa"))
    return roots


def ensure_ac_ini_dev_apps() -> Path | None:
    """检查并提示 assetto_corsa.ini 的 ENABLE_DEV_APPS。返回该文件路径或 None。"""
    for udp_ini in candidate_udp_ini_paths():
        ac_ini = (udp_ini.parent / ".." / ".." / "system" / "cfg" / "assetto_corsa.ini").resolve()
        if ac_ini.exists():
            try:
                raw = ac_ini.read_text(encoding="utf-8", errors="replace")
            except Exception:
                raw = ""
            for line in raw.splitlines():
                if line.strip().lower().startswith("enable_dev_apps="):
                    return ac_ini if line.split("=", 1)[1].strip() != "1" else None
    for root in _ac_install_candidates():
        ac_ini = root / "system" / "cfg" / "assetto_corsa.ini"
        if ac_ini.exists():
            try:
                raw = ac_ini.read_text(encoding="utf-8", errors="replace")
            except Exception:
                raw = ""
            for line in raw.splitlines():
                if line.strip().lower().startswith("enable_dev_apps="):
                    return ac_ini if line.split("=", 1)[1].strip() != "1" else None
    return None


def find_ac_tracks_dir() -> Path | None:
    """AC 安装目录 content/tracks（赛道 ui 图/ai 文件所在）。找不到返回 None。"""
    for cand in _ac_install_candidates():
        p = cand / "content" / "tracks"
        if p.is_dir():
            return p
    return None
