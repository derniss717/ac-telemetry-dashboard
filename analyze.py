# -*- coding: utf-8 -*-
"""赛后分析报告：读取 SQLite 会话，生成交互式 HTML（plotly）。

报告包含：
  1. 会话概览（车/赛道/圈数/最佳圈/最佳扇区）
  2. 每圈统计表（S1/S2/S3/极速/均速/油门刹车统计/悬架/电量/燃油/G 值）
  3. 圈速柱状图
  4. 圈间对比叠加（速度/油门/刹车 vs 距离，下拉切换高亮圈）
  5. 逐圈详情浏览器（10 行子图，下拉切换查看任意一圈，最佳圈虚影对照）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

import ac_udp
from storage import Storage

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
DARK = dict(
    paper_bgcolor="#0d1117", plot_bgcolor="#161b22",
    font=dict(color="#e6edf3", size=11),
    margin=dict(l=55, r=55, t=30, b=40),
)
GRID = "rgba(139,148,158,0.15)"
SERIES_COLOR = {
    "speed": "#58a6ff", "rpm": "#d2a8ff", "gear": "#8b949e",
    "gas": "#2ea043", "brake": "#f85149", "steer": "#e3b341",
    "kers": "#7ee787", "fuel": "#e3b341", "kj": "#d2a8ff",
    "accX": "#58a6ff", "accY": "#f85149",
}
WHEELS = ["FL", "FR", "RL", "RR"]


def fmt_ms(ms: int) -> str:
    if not ms or ms <= 0:
        return "--:--.---"
    m, s = divmod(ms // 1000, 60)
    return f"{m}:{s:02d}.{ms % 1000:03d}"


def _ch(m: np.ndarray, name: str) -> np.ndarray:
    """从 (N, C) 矩阵取一列；缺失通道返回全 NaN。"""
    idx = ac_udp.CHANNEL_INDEX.get(name)
    if idx is None or m.shape[1] <= idx:
        return np.full(m.shape[0], np.nan)
    return m[:, idx]


def _avail(names: List[str]) -> Dict[str, np.ndarray]:
    return {n: (i, ac_udp.CHANNEL_INDEX[n]) for n in names if n in ac_udp.CHANNEL_INDEX}


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------
def load_lap_arrays(storage: Storage, session_id: int, lap: dict) -> Optional[dict]:
    frames = storage.frames_for_lap(session_id, lap["id"])
    if len(frames) < 10:
        return None
    mats = [np.frombuffer(f["payload"], dtype="<f4") for f in frames]
    M = np.stack([np.asarray(m, dtype=np.float32).reshape(-1) for m in mats])
    x = np.asarray([f["dist"] for f in frames], dtype=np.float64)
    x = x - x[0]
    gear = np.asarray([f["gear"] for f in frames], dtype=np.int16)
    sector = np.asarray([f["sector"] for f in frames], dtype=np.int16)
    # 扇区边界（x 坐标）
    sx1 = None; sx2 = None
    for i in range(1, len(sector)):
        if sector[i] == 1 and sector[i - 1] != 1 and sx1 is None:
            sx1 = float(x[i])
        elif sector[i] == 2 and sx2 is None:
            sx2 = float(x[i])
    return {
        "lap_no": lap["lap_no"], "id": lap["id"], "total_ms": lap["total_ms"],
        "valid": bool(lap["is_valid"]), "inlap": bool(lap["is_inlap"]),
        "s1": lap["s1_ms"], "s2": lap["s2_ms"], "s3": lap["s3_ms"],
        "top_speed": lap["top_speed_kmh"], "x": x, "M": M, "gear": gear,
        "sx1": sx1, "sx2": sx2, "n": len(frames),
    }


def load_session(storage: Storage, session_id: int) -> dict:
    s = storage.get_session(session_id)
    if not s:
        raise SystemExit(f"找不到会话 #{session_id}")
    laps = [l for l in storage.list_laps(session_id)]
    data = []
    for lap in laps:
        d = load_lap_arrays(storage, session_id, lap)
        if d:
            data.append(d)
    return {"session": s, "laps": data}


def lap_stats(d: dict) -> dict:
    M, x = d["M"], d["x"]
    speed = _ch(M, "speedKmh")
    gas = _ch(M, "gas") * 100
    brake = _ch(M, "brake") * 100
    susp = _ch(M, "suspensionTravel_FL") * 1000  # mm
    ride = _ch(M, "rideHeight_F") * 1000
    kers = _ch(M, "kersCharge")
    fuel = _ch(M, "fuel")
    acc = _ch(M, "accG_Z")
    valid_speed = speed[np.isfinite(speed)]
    valid_gas = gas[np.isfinite(gas)]
    valid_brake = brake[np.isfinite(brake)]
    def fmean(a):
        a = a[np.isfinite(a)]
        return float(a.mean()) if a.size else 0.0
    def fmax(a):
        a = a[np.isfinite(a)]
        return float(a.max()) if a.size else 0.0
    def fmin(a):
        a = a[np.isfinite(a)]
        return float(a.min()) if a.size else 0.0
    brake_usage = float(np.mean(valid_brake > 1)) * 100 if valid_brake.size else 0.0
    fuel_use = 0.0
    f = fuel[np.isfinite(fuel)]
    if f.size > 2:
        # fuel 单位是升（如 46.5L），耗油 = 起点-终点（升），报告另按满油归一化
        fuel_use = float(f[0] - f[-1])
    k = kers[np.isfinite(kers)]
    return {
        "avg_speed": float(valid_speed.mean()) if valid_speed.size else 0.0,
        "avg_gas": fmean(valid_gas), "avg_brake": fmean(valid_brake),
        "brake_usage_pct": brake_usage,
        "max_acc": fmax(np.abs(_ch(M, "accG_Z"))),       # 纵向峰值 g（AC: Z 是纵向）
        "max_lat": fmax(np.abs(_ch(M, "accG_X"))),       # 横向峰值 g（AC: X 是横向）
        "min_ride_f": fmin(ride) if np.any(np.isfinite(ride)) else 0.0,
        "max_susp": fmax(np.abs(susp)) if np.any(np.isfinite(susp)) else 0.0,
        "kers_min": fmin(k) * 100 if k.size else None,
        "kers_max": fmax(k) * 100 if k.size else None,
        "fuel_use_l": fuel_use,     # 升
        "max_gas": fmax(valid_gas),
    }


# ---------------------------------------------------------------------------
# 图表
# ---------------------------------------------------------------------------
def _import_plotly():
    try:
        import plotly.graph_objects as go
        from plotly import subplots
        return go, subplots
    except ImportError:
        raise SystemExit("缺少 plotly，请先安装: pip install plotly")


def vlines(fig, xvals, rows: int):
    for x in xvals:
        if x is None:
            continue
        for r in range(1, rows + 1):
            fig.add_vline(x=x, line=dict(color="rgba(227,179,65,0.55)", width=1,
                                         dash="dash"), row=r)


def make_lap_bar_fig(laps: List[dict]) -> "object":
    go, _ = _import_plotly()
    valid = [d for d in laps if d["valid"]]
    best = min((d for d in valid), key=lambda d: d["total_ms"]) if valid else None
    colors = []
    times = []
    labels = []
    for d in laps:
        times.append(d["total_ms"])
        labels.append(str(d["lap_no"]) + ("" if d["valid"] else " *"))
        if not d["valid"]:
            colors.append("#6e7681")
        elif best and d["id"] == best["id"]:
            colors.append("#2ea043")
        else:
            colors.append("#58a6ff")
    fig = go.Figure(go.Bar(x=labels, y=times, marker_color=colors,
                           text=[fmt_ms(t) for t in times], textposition="outside"))
    fig.update_layout(title="每圈用时（* = 无效圈）", yaxis_title="ms",
                      yaxis=dict(gridcolor=GRID), **DARK)
    return fig


def make_comparison_fig(laps: List[dict]) -> "object":
    go, subplots = _import_plotly()
    valid = [d for d in laps if d["valid"]]
    best = min((d for d in valid), key=lambda d: d["total_ms"]) if valid else laps[0]
    rows = 3
    fig = subplots.make_subplots(rows=rows, cols=1, shared_xaxes=True,
                                 subplot_titles=["速度 (km/h)", "油门 (%)", "刹车 (%)"],
                                 vertical_spacing=0.06)
    visible_lists = []
    for d in laps:
        vis = [False] * (len(laps) * rows)
        # 全部圈：细线半透明
        for r, key, color in ((1, "speedKmh", "#58a6ff"), (2, "gas", "#2ea043"),
                              (3, "brake", "#f85149")):
            fig.add_trace(go.Scatter(x=d["x"], y=_ch(d["M"], key),
                                     mode="lines", line=dict(width=1, color=color),
                                     opacity=0.35, showlegend=False,
                                     hoverinfo="skip"), row=r, col=1)
        # 选中圈（本按钮对应的圈）：粗线
        for r, key, color in ((1, "speedKmh", "#58a6ff"), (2, "gas", "#2ea043"),
                              (3, "brake", "#f85149")):
            fig.add_trace(go.Scatter(x=d["x"], y=_ch(d["M"], key),
                                     mode="lines", line=dict(width=2.5, color=color),
                                     showlegend=False, hoverinfo="skip"), row=r, col=1)
        idx = len(fig.data) - rows
        vis[idx:idx + rows] = [True] * rows
        visible_lists.append((d["lap_no"], d, vis))
    # 默认高亮最佳圈
    default_vis = [False] * len(fig.data)
    for ln, d, vis in visible_lists:
        if d["id"] == best["id"]:
            default_vis = vis[:]
    buttons = []
    for ln, d, vis in visible_lists:
        buttons.append(dict(label=f"圈 {ln}" + (f"  {fmt_ms(d['total_ms'])}" if d["valid"] else " *"),
                            method="restyle",
                            args=[{"visible": vis, "opacity": [1.0] * len(vis)}]))
    buttons.append(dict(label="显示全部", method="restyle",
                        args=[{"visible": [True] * len(fig.data)}]))
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=1.02, xanchor="left",
                                        y=1.0, bgcolor="#1c2330",
                                        font=dict(color="#e6edf3"))], **DARK)
    for i in range(1, rows + 1):
        fig.update_yaxes(gridcolor=GRID, row=i, col=1)
    fig.update_xaxes(title_text="圈内距离 (m)", gridcolor=GRID, row=rows, col=1)
    fig.update_layout(showlegend=False)
    return fig


def make_detail_fig(laps: List[dict]) -> "object":
    go, subplots = _import_plotly()
    valid = [d for d in laps if d["valid"]]
    best = min((d for d in valid), key=lambda d: d["total_ms"]) if valid else laps[0]
    has_kj = any(np.isfinite(_ch(d["M"], "kersCurrentKJ")).any() for d in laps)
    rows = 10
    heights = [2, 1, 1.6, 1.6, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4]
    titles = ["车速 km/h / 转速 rpm", "档位", "油门% / 刹车% / 转向°", "悬架行程 mm",
              "底板高度 mm", "KERS 电量% / 油量%", "加速度 g (纵/横)",
              "轮荷 N", "滑移率 %", "胎温 °C (core)"]
    fig = subplots.make_subplots(rows=rows, cols=1, shared_xaxes=True,
                                 subplot_titles=titles, vertical_spacing=0.018,
                                 row_heights=heights)
    # 行定义: (row, 系列构造器) 返回 trace 列表（x 用 d['x']）
    def row_speed(d):
        return [
            go.Scatter(x=d["x"], y=_ch(d["M"], "speedKmh"), name=f"L{d['lap_no']} 车速",
                       line=dict(color=SERIES_COLOR["speed"], width=1.6), showlegend=False),
            go.Scatter(x=d["x"], y=_ch(d["M"], "rpms"), name=f"L{d['lap_no']} 转速",
                       line=dict(color=SERIES_COLOR["rpm"], width=1.2), showlegend=False,
                       yaxis="y2"),
        ]
    def row_gear(d):
        return [go.Scatter(x=d["x"], y=d["gear"].astype(float), name=f"L{d['lap_no']} 档位",
                           line=dict(color=SERIES_COLOR["gear"], width=1.5,
                                     shape="hv"), showlegend=False)]
    def row_pedals(d):
        return [
            go.Scatter(x=d["x"], y=_ch(d["M"], "gas") * 100, name="油门",
                       line=dict(color=SERIES_COLOR["gas"], width=1.4), showlegend=False),
            go.Scatter(x=d["x"], y=_ch(d["M"], "brake") * 100, name="刹车",
                       line=dict(color=SERIES_COLOR["brake"], width=1.4), showlegend=False),
            go.Scatter(x=d["x"], y=_ch(d["M"], "steerAngle") * 180 / np.pi, name="转向°",
                       line=dict(color=SERIES_COLOR["steer"], width=1), showlegend=False,
                       yaxis="y2"),
        ]
    def row_susp(d):
        return [go.Scatter(x=d["x"], y=_ch(d["M"], f"suspensionTravel_{w}") * 1000,
                           name=f"{w}", line=dict(width=1.1), showlegend=False)
                for w in WHEELS]
    def row_ride(d):
        return [
            go.Scatter(x=d["x"], y=_ch(d["M"], "rideHeight_F") * 1000, name="前",
                       line=dict(color="#f0f6fc", width=1.5), showlegend=False),
            go.Scatter(x=d["x"], y=_ch(d["M"], "rideHeight_R") * 1000, name="后",
                       line=dict(color="#f0883e", width=1.5), showlegend=False),
        ]
    def row_kers(d):
        tr = [
            go.Scatter(x=d["x"], y=_ch(d["M"], "kersCharge") * 100, name="电量%",
                       line=dict(color=SERIES_COLOR["kers"], width=1.4), showlegend=False),
            go.Scatter(x=d["x"], y=_ch(d["M"], "fuel") * 100, name="油量%",
                       line=dict(color=SERIES_COLOR["fuel"], width=1.2), showlegend=False),
        ]
        if has_kj:
            tr.append(go.Scatter(x=d["x"], y=_ch(d["M"], "kersCurrentKJ"),
                                 name="KERS kJ", line=dict(color=SERIES_COLOR["kj"],
                                 width=1, dash="dot"), showlegend=False, yaxis="y2"))
        return tr
    def row_acc(d):
        return [
            go.Scatter(x=d["x"], y=_ch(d["M"], "accG_Z"), name="纵向 g",
                       line=dict(color=SERIES_COLOR["accX"], width=1.3), showlegend=False),
            go.Scatter(x=d["x"], y=_ch(d["M"], "accG_X"), name="横向 g",
                       line=dict(color=SERIES_COLOR["accY"], width=1.3), showlegend=False),
        ]
    def row_load(d):
        return [go.Scatter(x=d["x"], y=_ch(d["M"], f"wheelLoad_{w}"), name=w,
                           line=dict(width=1.1), showlegend=False) for w in WHEELS]
    def row_slip(d):
        return [go.Scatter(x=d["x"], y=_ch(d["M"], f"wheelSlip_{w}") * 100, name=w,
                           line=dict(width=1.1), showlegend=False) for w in WHEELS]
    def row_tyre(d):
        tr = [go.Scatter(x=d["x"], y=_ch(d["M"], f"tyreCoreTemperature_{w}"), name=w,
                         line=dict(width=1.1), showlegend=False) for w in WHEELS]
        return tr
    row_builders = [row_speed, row_gear, row_pedals, row_susp, row_ride,
                    row_kers, row_acc, row_load, row_slip, row_tyre]
    # 每个圈：每行建 trace，仅最佳圈默认可见（虚影），选中圈可见
    selected_vis = []
    all_vis = []
    for d in laps:
        for r, builder in enumerate(row_builders, start=1):
            for tr in builder(d):
                tr.visible = False
                fig.add_trace(tr, row=r, col=1)
    n_total = len(fig.data)
    per_lap = n_total // len(laps) if laps else 0
    best_start = 0
    for i, d in enumerate(laps):
        start = i * per_lap
        if d["id"] == best["id"]:
            best_start = start
    # 最佳圈虚影（默认可见）
    for i in range(best_start, best_start + per_lap):
        fig.data[i].visible = True
        fig.data[i].opacity = 0.35
        fig.data[i].line.width = 1.0 if fig.data[i].line.width else 1.0
    buttons = []
    for i, d in enumerate(laps):
        start = i * per_lap
        vis = [False] * n_total
        for j in range(start, start + per_lap):
            vis[j] = True
        for j in range(best_start, best_start + per_lap):
            vis[j] = True
        label = f"圈 {d['lap_no']}" + (f"  {fmt_ms(d['total_ms'])}" if d["valid"] else " *")
        buttons.append(dict(label=label, method="restyle",
                            args=[{"visible": vis, "opacity": [1.0] * n_total}]))
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=1.02, xanchor="left",
                                        y=1.0, bgcolor="#1c2330",
                                        font=dict(color="#e6edf3"))], **DARK)
    for r in range(1, rows + 1):
        fig.update_yaxes(gridcolor=GRID, row=r, col=1)
    fig.update_xaxes(title_text="圈内距离 (m)", gridcolor=GRID, row=rows, col=1)
    # 扇区线
    for d in laps:
        vlines(fig, [d["sx1"], d["sx2"], float(d["x"][-1])], rows)
    # 最佳圈图例
    fig.update_layout(showlegend=False)
    return fig


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------
def build_report(storage: Storage, session_id: int, out_path: Path,
                 local: bool = False, include_detail: bool = True) -> Path:
    import plotly
    import plotly.offline as po

    data = load_session(storage, session_id)
    s = data["session"]
    laps = data["laps"]
    valid = [d for d in laps if d["valid"]]
    best = min((d for d in valid), key=lambda d: d["total_ms"]) if valid else None
    best_s1 = min((d["s1"] for d in valid if d["s1"]), default=0)
    best_s2 = min((d["s2"] for d in valid if d["s2"]), default=0)
    best_s3 = min((d["s3"] for d in valid if d["s3"]), default=0)

    include_js = "inline" if local else "cdn"
    plotlyjs_kw = {"include_plotlyjs": include_js, "output_type": "div",
                   "config": {"displaylogo": False, "scrollZoom": True}}

    divs = []
    divs.append(po.plot(make_lap_bar_fig(laps), **plotlyjs_kw))
    divs.append(po.plot(make_comparison_fig(laps), include_plotlyjs=False,
                        output_type="div", config={"displaylogo": False}))
    if include_detail:
        divs.append(po.plot(make_detail_fig(laps), include_plotlyjs=False,
                            output_type="div", config={"displaylogo": False}))

    stats_rows = []
    for d in laps:
        st = lap_stats(d)
        stats_rows.append(f"""
        <tr class="{'best-row' if best and d['id'] == best['id'] else ''}">
          <td>{d['lap_no']}</td>
          <td class="num">{fmt_ms(d['total_ms'])}</td>
          <td class="num">{fmt_ms(d['s1'])}</td>
          <td class="num">{fmt_ms(d['s2'])}</td>
          <td class="num">{fmt_ms(d['s3'])}</td>
          <td class="num">{(d['top_speed'] or 0):.1f}</td>
          <td class="num">{st['avg_speed']:.1f}</td>
          <td class="num">{st['avg_gas']:.0f}</td>
          <td class="num">{st['brake_usage_pct']:.0f}%</td>
          <td class="num">{st['min_ride_f']:.1f}</td>
          <td class="num">{st['max_susp']:.1f}</td>
          <td class="num">{'-' if st['kers_min'] is None else f"{st['kers_min']:.0f}→{st['kers_max']:.0f}"}</td>
          <td class="num">{st['fuel_use_l']:.1f}</td>
          <td class="num">{st['max_acc']:.2f}</td>
          <td class="num">{st['max_lat']:.2f}</td>
          <td>{'有效' if d['valid'] else '无效'}{' 进站' if d['inlap'] else ''}</td>
        </tr>""")

    start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["start_ts"]))
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>AC 赛后分析 · 会话 #{session_id}</title>
<style>
:root {{ --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2d333b;
  --text:#e6edf3; --dim:#8b949e; --green:#2ea043; --red:#f85149; --blue:#58a6ff; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--text);
  font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; padding:24px; }}
h1 {{ font-size:22px; margin-bottom:6px; }}
h2 {{ font-size:16px; margin:26px 0 10px; color:var(--dim); }}
.sub {{ color:var(--dim); font-size:13px; margin-bottom:18px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:12px; }}
.card .k {{ font-size:11px; color:var(--dim); }}
.card .v {{ font-size:20px; font-weight:700; margin-top:4px; }}
.card .v.green {{ color:var(--green); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th,td {{ padding:7px 10px; text-align:left; border-bottom:1px solid var(--border); }}
th {{ color:var(--dim); font-weight:600; }}
td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.best-row td {{ color:var(--green); }}
.plot {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:10px; margin-bottom:14px; }}
.chk {{ color:var(--dim); font-size:12px; margin-bottom:10px; }}
</style></head><body>
<h1>AC 赛后分析 · 会话 #{session_id}</h1>
<div class="sub">{s['name']} · 开始于 {start_str}
 · 数据通道: {ac_udp.N_CHANNELS} 个（含扩展字段，缺失自动 NaN）</div>
<div class="cards">
  <div class="card"><div class="k">车型</div><div class="v">{s['car'] or '—'}</div></div>
  <div class="card"><div class="k">赛道</div><div class="v">{s['track'] or '—'}</div></div>
  <div class="card"><div class="k">总圈数（有效）</div><div class="v">{len(laps)} ({len(valid)})</div></div>
  <div class="card"><div class="k">最佳圈</div><div class="v green">{fmt_ms(best['total_ms']) if best else '—'}</div></div>
  <div class="card"><div class="k">最佳 S1/S2/S3</div><div class="v green">{fmt_ms(best_s1)} / {fmt_ms(best_s2)} / {fmt_ms(best_s3)}</div></div>
</div>
<h2>每圈统计</h2>
<table><thead><tr>
  <th>圈</th><th class="num">用时</th><th class="num">S1</th><th class="num">S2</th><th class="num">S3</th>
  <th class="num">极速</th><th class="num">均速</th><th class="num">油门均%</th>
  <th class="num">刹车占比</th><th class="num">最低底板mm</th><th class="num">最大悬架mm</th>
  <th class="num">电量%</th><th class="num">耗油L</th><th class="num">纵向G</th><th class="num">横向G</th><th>状态</th>
</tr></thead><tbody>{''.join(stats_rows)}</tbody></table>
<div class="chk">底板高度 = rideHeight（前轴），悬架 = suspensionTravel 绝对值；电量 = KERS charge；速度单位 km/h；G 值为该圈绝对值峰值（g）。</div>
<h2>圈速</h2><div class="plot">{divs[0]}</div>
<h2>圈间对比（速度 / 油门 / 刹车）· 下拉高亮任意一圈</h2><div class="plot">{divs[1]}</div>"""

    if include_detail:
        html += f"""
<h2>逐圈详情浏览器（下拉切换；最佳圈为灰色虚影对照；黄色虚线为扇区分界）</h2>
<div class="plot">{divs[2]}</div>"""

    html += "</body></html>"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="analyze", description="AC 赛后分析报告")
    ap.add_argument("session", help="会话 id 或 latest")
    ap.add_argument("--output", "-o", default="", help="输出 HTML 路径")
    ap.add_argument("--local", action="store_true", help="plotly.js 内联（离线可用）")
    ap.add_argument("--no-detail", action="store_true", help="不生成逐圈详情图")
    ap.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    args = ap.parse_args(argv)

    import config as cfg
    db = cfg.resolve_path(cfg.load_config(), "data_dir") / "ac.db"
    storage = Storage(db)
    if args.session == "latest":
        sessions = storage.list_sessions()
        if not sessions:
            print("还没有任何会话。先运行: python main.py start")
            return 1
        sid = sessions[-1]["id"]
    else:
        sid = int(args.session)
    out = Path(args.output) if args.output else (
        cfg.resolve_path(cfg.load_config(), "reports_dir")
        / f"session_{sid}.html")
    t0 = time.time()
    build_report(storage, sid, out, local=args.local,
                 include_detail=not args.no_detail)
    storage.close()
    print(f"报告已生成: {out}  ({time.time() - t0:.1f}s)")
    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
