/* AC 遥测仪表盘前端逻辑：200ms 轮询 /api/live，增量维护各图表数据。 */
"use strict";

const POLL_MS = 200;
const LAPS_POLL_MS = 2000;
const SESSION_POLL_MS = 5000;
const MAX_PTS = 4000;   // 每个图表保留的原始点数（超出丢弃最旧）
const DISPLAY_PTS = 400; // 渲染时降采样点数

let afterSeq = 0;
let curLapNo = -1;
let lastDataT = 0;
let maxFuelSeen = 0;   // 会话内最大油量(升)，用于把油量归一化为 %
// ERS 电池满电能量：VRC F1 2024 按 ~4MJ（F1 规则值）；其他车可调
const ERS_BATTERY_KJ = 4000;
// 充放电功率用 kersCharge（基础通道，可靠）差分算，不用 kersCurrentKJ（668 扩展通道，与刹车盘温同批不可靠）
let lastKers = null, lastKersT = null;
let lapDistBase = 0;   // 当前圈起点累计距离(米)，X 轴按圈内距离显示

const CHART_COLORS = {
  speed: "#58a6ff", rpm: "#d2a8ff", gear: "#8b949e",
  gas: "#2ea043", brake: "#f85149", steer: "#e3b341",
  susp: ["#58a6ff", "#f0883e", "#3fb950", "#da3633"],
  ride: ["#f0f6fc", "#f0883e"],
  kers: "#7ee787", fuel: "#e3b341",
  accX: "#58a6ff", accY: "#f85149",
  tyre: ["#58a6ff", "#f0883e", "#3fb950", "#da3633"],
  turbo: "#d2a8ff",
};
const WHEELS = ["FL", "FR", "RL", "RR"];
const G_TO_MS2 = 9.80665;   // g → m/s²（备用，仪表盘 G 值直接用 g）

/* ---------------- 图表数据仓库 ---------------- */
function makeRepo() {
  return { x: [], series: {}, drsArr: [] };
}
const repos = {
  speed: makeRepo(), rpm: makeRepo(), gear: makeRepo(), pedals: makeRepo(), susp: makeRepo(), ride: makeRepo(),
  kers: makeRepo(), kerskj: makeRepo(), accg: makeRepo(), tyre: makeRepo(),
  pressure: makeRepo(),
  turbo: makeRepo(),
  tyreO: makeRepo(), slip: makeRepo(),
  steer: makeRepo(),
};

function pushFrame(repo, x, vals) {
  repo.x.push(x);
  for (const k of Object.keys(vals)) {
    (repo.series[k] = repo.series[k] || []).push(vals[k]);
    // 旁路数组（drs 等不渲染的标记数据）随主序列同步维护
    if (k === "drs") (repo.drsArr = repo.drsArr || []).push(vals[k]);
  }
  // 回放模式不截断（要显示整圈）；实时模式保留最近 MAX_PTS 帧
  if (!replayMode && repo.x.length > MAX_PTS) {
    const cut = repo.x.length - MAX_PTS;
    repo.x.splice(0, cut);
    for (const k of Object.keys(repo.series)) repo.series[k].splice(0, cut);
    if (repo.drsArr) repo.drsArr.splice(0, cut);
  }
}

function resetRepos() {
  for (const k of Object.keys(repos)) repos[k] = makeRepo();
  lastKers = null; lastKersT = null;
}

function ingestFrame(fr) {
  if (fr.lap_no !== curLapNo && !replayMode) {
    curLapNo = fr.lap_no;
    resetRepos();
    if (fr.dist != null) lapDistBase = fr.dist;   // 新圈：距离基准 = 本圈起点
  }
  // X 轴用圈内距离（累计距离 - 圈起点），无距离数据时回退用时间
  const x = (fr.dist != null && lapDistBase != null) ? (fr.dist - lapDistBase) : fr.lap_ms;
  pushFrame(repos.speed, x, { speed: fr.speed, drs: fr.drs > 0.5 ? 1 : 0 });
  pushFrame(repos.rpm, x, { rpm: fr.rpms });
  pushFrame(repos.gear, x, { gear: fr.gear - 1 });   // AC: gear=档位+1（1=空挡, 9=8档）
  pushFrame(repos.pedals, x, { gas: fr.gas * 100, brake: fr.brake * 100 });
  // 单位换算：悬架/底板 米→mm；电量 0~1→%；油量是"升"（如 46.5L），按会话最大油量归一化为 %；
  // G 值保留原值(g)
  pushFrame(repos.susp, x, { FL: fr.susp[0] * 1000, FR: fr.susp[1] * 1000, RL: fr.susp[2] * 1000, RR: fr.susp[3] * 1000 });
  pushFrame(repos.ride, x, { F: fr.ride[0] * 1000, R: fr.ride[1] * 1000 });
  if (fr.fuel != null && !isNaN(fr.fuel)) maxFuelSeen = Math.max(maxFuelSeen, fr.fuel);
  const fuelPct = maxFuelSeen > 0 ? Math.max(0, Math.min(100, fr.fuel / maxFuelSeen * 100)) : 0;
  pushFrame(repos.kers, x, { kers: fr.kers * 100, fuel: fuelPct });
  // ERS 充放电功率：电量差分 Δ(0~1) × 电池容量 → kW，正=放电（MGUK出力），负=充电（回收）
  // kersCharge（基础通道）可靠；kersCurrentKJ（扩展通道）与刹车盘温同批实测为垃圾值
  let ersPwr = null;
  if (fr.kers != null && fr.t != null && lastKers != null && lastKersT != null && fr.t > lastKersT) {
    ersPwr = -(fr.kers - lastKers) / (fr.t - lastKersT) * ERS_BATTERY_KJ;   // 0~1/s × kJ = kW
  }
  if (fr.kers != null) { lastKers = fr.kers; lastKersT = fr.t; }
  pushFrame(repos.kerskj, x, { kj: fr.kers, pwr: ersPwr });
  // accG 已由后端按 [纵向, 横向, 垂直] 输出：X=纵向、Y=横向
  pushFrame(repos.accg, x, { X: fr.accG[0], Y: fr.accG[1] });
  pushFrame(repos.tyre, x, { FL: fr.tyreT[0], FR: fr.tyreT[1], RL: fr.tyreT[2], RR: fr.tyreT[3] });
  if (fr.pressure) pushFrame(repos.pressure, x, { FL: fr.pressure[0], FR: fr.pressure[1], RL: fr.pressure[2], RR: fr.pressure[3] });   // 直接 psi，与游戏调车界面一致
  pushFrame(repos.turbo, x, { turbo: fr.turbo });
  if (fr.tyreO) pushFrame(repos.tyreO, x, { FL: fr.tyreO[0], FR: fr.tyreO[1], RL: fr.tyreO[2], RR: fr.tyreO[3] });
  if (fr.slip) pushFrame(repos.slip, x, { FL: fr.slip[0] * 100, FR: fr.slip[1] * 100, RL: fr.slip[2] * 100, RR: fr.slip[3] * 100 });   // AC 滑移率 0~1 → %
  if (fr.steer != null) pushFrame(repos.steer, x, { steer: fr.steer });   // 方向盘角度 °（左负右正）
}

/* ---------------- 降采样（渲染用，stride） ---------------- */
function downsample(arr, maxPts) {
  if (!arr || arr.length <= maxPts) return arr;
  const step = Math.ceil(arr.length / maxPts);
  const out = [];
  for (let i = 0; i < arr.length; i += step) out.push(arr[i]);
  return out;
}

/* ---------------- Chart.js 初始化 ---------------- */
const gridColor = "rgba(139,148,158,0.15)";
const tickColor = "#8b949e";
function baseOptions(unit, secondAxis) {
  const o = {
    animation: false, responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    scales: {
      x: { type: "linear", ticks: { color: tickColor, maxTicksLimit: 6,
                    callback: v => (v == null || isNaN(v)) ? "" : fmtDist(v) },
           grid: { color: gridColor },
           title: { display: true, text: "圈内距离", color: tickColor, font: { size: 10 } } },
      y: { ticks: { color: tickColor }, grid: { color: gridColor },
           title: { display: true, text: unit, color: tickColor, font: { size: 10 } } },
    },
    plugins: {
      legend: { labels: { color: "#e6edf3", boxWidth: 10, font: { size: 10 } } },
      tooltip: {
        callbacks: {
          title: items => {
            const v = items && items[0] ? items[0].parsed.x : null;
            return v == null || isNaN(v) ? "" : `距离 ${fmtDist(v)}`;
          },
        },
      },
    },
  };
  if (secondAxis) {
    o.scales.y1 = { position: "right", ticks: { color: tickColor }, grid: { drawOnChartArea: false },
                    title: { display: true, text: secondAxis, color: tickColor, font: { size: 10 } } };
  }
  return o;
}
function mkChart(id, datasets, options) {
  return new Chart(document.getElementById(id), { type: "line", data: { datasets }, options });
}
function line(label, color, key, yAxis, extra) {
  const ds = { label, borderColor: color, backgroundColor: color, data: [],
               borderWidth: 1.5, pointRadius: 0, tension: 0.15, spanGaps: true };
  if (yAxis) ds.yAxisID = yAxis;
  if (extra) Object.assign(ds, extra);
  ds._key = key; ds._repoKey = key;
  return ds;
}

const charts = {
  speed: mkChart("c-speed", [
    line("车速 km/h", CHART_COLORS.speed, "speed"),
  ], baseOptions("km/h")),
  rpm: mkChart("c-rpm", [
    line("转速 rpm", CHART_COLORS.rpm, "rpm"),
  ], baseOptions("rpm")),
  gear: mkChart("c-gear", [
    line("档位", CHART_COLORS.gear, "gear"),
  ], baseOptions("档")),
  pedals: mkChart("c-pedals", [
    line("油门 %", CHART_COLORS.gas, "gas"),
    line("刹车 %", CHART_COLORS.brake, "brake"),
  ], baseOptions("%")),
  susp: mkChart("c-susp", WHEELS.map((w, i) =>
    line(`悬架 ${w} mm`, CHART_COLORS.susp[i], w)), baseOptions("mm")),
  ride: mkChart("c-ride", [
    line("底板 前 mm", CHART_COLORS.ride[0], "F"),
    line("底板 后 mm", CHART_COLORS.ride[1], "R"),
  ], baseOptions("mm")),
  kers: mkChart("c-kers", [
    line("KERS 电量 %", CHART_COLORS.kers, "kers"),
    line("油量 %", CHART_COLORS.fuel, "fuel"),
  ], baseOptions("%")),
  kerskj: mkChart("c-kerskj", [
    line("充放电 kW", "#d2a8ff", "pwr", null, { segment: { borderColor: ctx => {
      const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex;
      const v = ctx.chart.data.datasets[ctx.datasetIndex].data[i];
      return v >= 0 ? "#f85149" : "#7ee787";   // 放电红 / 充电绿
    } } }),
  ], baseOptions("kW")),
  accg: mkChart("c-accg", [
    line("纵向 g", CHART_COLORS.accX, "X"),
    line("横向 g", CHART_COLORS.accY, "Y", "y1"),
  ], baseOptions("g", "g")),
  tyre: mkChart("c-tyre", WHEELS.map((w, i) =>
    line(`胎温 ${w} °C`, CHART_COLORS.tyre[i], w)), baseOptions("°C")),
  tyreO: mkChart("c-tyreO", WHEELS.map((w, i) =>
    line(`外层胎温 ${w} °C`, CHART_COLORS.tyre[i], w)), baseOptions("°C")),
  slip: mkChart("c-slip", WHEELS.map((w, i) =>
    line(`滑移率 ${w} %`, CHART_COLORS.tyre[i], w)), baseOptions("%")),
  pressure: mkChart("c-pressure", WHEELS.map((w, i) =>
    line(`胎压 ${w} psi`, CHART_COLORS.tyre[i], w)), baseOptions("psi")),
  turbo: mkChart("c-turbo", [
    line("涡轮增压 bar", CHART_COLORS.turbo, "turbo"),
  ], baseOptions("bar")),
  steer: mkChart("c-steer", [
    line("方向盘 °", "#e3b341", "steer", null, { segment: { borderColor: ctx => {
      const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex;
      const v = ctx.chart.data.datasets[ctx.datasetIndex].data[i];
      return (v != null && v < 0) ? "#f85149" : "#2ea043";   // 左打红 / 右打绿
    } } }),
  ], baseOptions("°")),
};

/* y 轴自适应策略（每个图表在 _auto 里声明）：
   pad   = 跟随数据 min/max + 留白（默认，曲线占满）
   zero  = 从 0 起 + 上限跟随数据
   sym   = 以 0 为中心对称（正负等幅，适合 G 值）
   fixed = 固定范围不做自适应 */
charts.speed._auto = { mode: "pad", zero: true };
charts.rpm._auto = { mode: "pad", zero: true };
charts.gear._auto = { mode: "fixed", min: 1, max: 8 };   // 只显示 1~8 档
charts.gear._smooth = 1;   // 阶梯数据不平滑（避免出现 4.333 档）
charts.kerskj._auto = { mode: "pad", zero: true };   // 跟随数据自适应（0 线保留，不再固定对称浪费空间）
// KERS 充放电 tooltip：正=放电 / 负=充电，直接标注，方便区分
charts.kerskj.options.plugins.tooltip.callbacks.label = (ctx) => {
  const v = ctx.parsed.y;
  if (v == null || isNaN(v)) return "";
  return (v >= 0 ? "放电 " : "充电 ") + Math.abs(v).toFixed(1) + " kW";
};
charts.kerskj._smooth = 5;   // 功率差分噪声大，平滑
charts.pressure._auto = { mode: "pad", loCap: 0 };   // 默认数据自适应；已知车型由 setPressureRange 覆盖为固定范围
charts.pressure._smooth = 3;
charts.pedals._auto = { mode: "fixed", min: 0, max: 100 };
charts.susp._auto = { mode: "pad" };
charts.ride._auto = { mode: "pad" };
charts.kers._auto = { mode: "fixed", min: 1, max: 100 };
charts.accg._auto = { mode: "fixed", min: -6, max: 6 };  // 纵向 F1 极限范围：急刹 ~5g
charts.accg._auto1 = { mode: "sym", padAbs: 0.5 };       // 横向独立右轴，自适应（弯道 ~2g 也能看清）
charts.tyre._auto = { mode: "pad" };
charts.tyreO._auto = { mode: "pad" };
charts.slip._auto = { mode: "pad", zero: true };   // 滑移率从 0 起
charts.turbo._auto = { mode: "pad", loCap: 0 };
charts.steer._auto = { mode: "pad" };   // 方向盘角度跟随数据自适应
charts.accg._smooth = 5;      // 高噪声通道用更宽滤波窗口
charts.turbo._smooth = 3;

/* 点击图表卡片放大显示（细致查看），再点还原 */
document.querySelectorAll(".card").forEach(card => {
  card.addEventListener("click", () => {
    const zoomed = card.classList.toggle("zoom");
    const h = card.querySelector("h2, h3");
    if (!h) return;
    if (zoomed && !card.dataset.origTitle) {
      card.dataset.origTitle = h.textContent;
      h.textContent = h.textContent.replace(/（.*?）/g, "") + "（点击卡片还原）";
    } else if (!zoomed && card.dataset.origTitle) {
      h.textContent = card.dataset.origTitle;
    }
  });
});
charts.speed._smooth = 3;

/* 胎压 Y 轴按车型动态调整：已知车型用实测固定范围（稳），未知车型回退数据自适应 */
const PRESSURE_RANGES = {
  "vrc_formula_alpha_2024_csp": [15, 28],   // F1 冷胎设定 17/16 psi，热胎实测 20~25.4
};
let currentCar = null;   // 当前会话车型（pollSession 刷新），回放结束回实时时恢复用
function setPressureRange(car) {
  const r = car && PRESSURE_RANGES[car];
  charts.pressure._auto = r ? { mode: "fixed", min: r[0], max: r[1] }
                            : { mode: "pad", loCap: 0 };
  if (car) currentCar = car;
}

function applyAutoRange(ch) {
  // 主轴（y）
  applyAxisRange(ch, "y", ch._auto || { mode: "pad" });
  // 副轴（y1）如横向 G：按绑定该轴的 series 数据自适应
  if (ch._auto1 && ch.options.scales.y1) {
    applyAxisRange(ch, "y1", ch._auto1);
  }
}
function applyAxisRange(ch, axisId, auto) {
  const all = [];
  for (const ds of ch.data.datasets) {
    if (ds.yAxisID && ds.yAxisID !== axisId) continue;   // 只统计绑定本轴的数据
    if (!ds.yAxisID && axisId !== "y") continue;
    for (const v of ds.data) if (v != null && !isNaN(v)) all.push(v);
  }
  const y = ch.options.scales[axisId];
  if (auto.mode === "fixed") {
    y.min = auto.min; y.max = auto.max;
    return;
  }
  if (!all.length) return;
  let lo = Math.min(...all), hi = Math.max(...all);
  const span = (hi - lo) || 1;
  const pad = (auto.padAbs != null ? auto.padAbs : span * 0.08);
  if (auto.mode === "sym") {
    const m = Math.max(Math.abs(lo), Math.abs(hi));
    y.min = -m - pad; y.max = m + pad;
  } else {
    y.min = lo - pad; y.max = hi + pad;
    if (auto.loCap != null) y.min = Math.max(y.min, auto.loCap);
    if (auto.hiCap != null) y.max = Math.min(y.max, auto.hiCap);
    // 仅显式声明 zero 的图（车速/转速）才从 0 起，避免高温小波动数据（胎温等）被压扁
    if (auto.zero) y.min = Math.min(0, lo);
  }
}

/* 抖动过滤：轻量滑动平均（只影响显示，不改原始数据） */
function smooth(arr, w) {
  if (!arr || arr.length <= w) return arr;
  const out = new Array(arr.length);
  const half = Math.floor(w / 2);
  for (let i = 0; i < arr.length; i++) {
    let s = 0, c = 0;
    for (let j = Math.max(0, i - half); j <= Math.min(arr.length - 1, i + half); j++) {
      const v = arr[j];
      if (v == null || isNaN(v)) continue;
      s += v; c++;
    }
    out[i] = c ? s / c : arr[i];
  }
  return out;
}

function refreshChart(name) {
  const repo = repos[name];
  const ch = charts[name];
  const dsMap = {};
  for (const ds of ch.data.datasets) dsMap[ds._key] = ds;
  ch.data.labels = downsample(repo.x, DISPLAY_PTS);
  const w = ch._smooth || 3;
  for (const k of Object.keys(repo.series)) {
    if (dsMap[k]) dsMap[k].data = smooth(downsample(repo.series[k], DISPLAY_PTS), w);
  }
  // 速度曲线：DRS 开启段用红色（segment 逐段着色）
  if (name === "speed" && dsMap.speed && repo.drsArr && repo.drsArr.length) {
    const drsDs = downsample(repo.drsArr, DISPLAY_PTS);
    dsMap.speed._drs = drsDs;
    dsMap.speed.segment = {
      borderColor: ctx => {
        const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex;
        return (drsDs[i] || 0) > 0.5 ? "#f85149" : CHART_COLORS.speed;
      },
    };
  } else if (name === "speed" && dsMap.speed) {
    dsMap.speed.segment = undefined;
  }
  applyAutoRange(ch);
  ch.update("none");
}

/* ---------------- 数值块 ---------------- */
function fmtMs(ms) {
  if (ms == null || isNaN(ms) || ms <= 0) return "--:--.---";
  const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000), f = ms % 1000;
  return `${m}:${String(s).padStart(2, "0")}.${String(f).padStart(3, "0")}`;
}
/* X 轴刻度用距离格式：0、500m、1.0km、1.5km… */
function fmtDist(m) {
  if (m == null || isNaN(m) || m <= 0) return "0";
  if (m < 1000) return `${Math.round(m)}m`;
  return `${(m / 1000).toFixed(1)}km`;
}
function gearText(g) {
  if (g == null) return "N";
  if (g <= 0) return "R";
  if (g === 1) return "N";
  return String(g - 1);
}
const $ = (id) => document.getElementById(id);

function updateTiles(cur) {
  $("t-speed").textContent = Math.round(cur.speed_kmh || 0);
  $("t-rpm").textContent = Math.round(cur.rpms || 0);
  $("t-gear").textContent = gearText(cur.gear);
  $("t-curlap").textContent = fmtMs(cur.lap_ms);
  $("t-lastlap").textContent = fmtMs(cur.last_lap_ms);
  $("t-bestlap").textContent = fmtMs(cur.best_lap_ms);
  $("t-sector").textContent = cur.sector == null ? "-" : (cur.sector + 1);
  const ts = $("t-steer");
  if (cur.steer_deg == null) {
    ts.textContent = "-";
    ts.style.color = "";
  } else {
    ts.textContent = cur.steer_deg.toFixed(1) + "°";
    ts.style.color = cur.steer_deg < 0 ? "var(--red)" : "var(--green)";   // 左打红 / 右打绿
  }
  $("t-kers").textContent = cur.kers_charge == null ? "--%" : Math.round(cur.kers_charge * 100) + "%";
  $("t-compound").textContent = cur.tyre_compound ? cur.tyre_compound : "--";
  // 油量字段单位是升（如 46.5），用会话最大油量归一化为 %；无基准时显示升数
  if (cur.fuel == null) {
    $("t-fuel").textContent = "--%";
  } else if (maxFuelSeen > 0 && cur.fuel <= maxFuelSeen) {
    $("t-fuel").textContent = Math.round(Math.max(0, Math.min(100, cur.fuel / maxFuelSeen * 100))) + "%";
  } else {
    $("t-fuel").textContent = cur.fuel.toFixed(1) + "L";
  }
  const badge = $("s-status");
  if (!cur.live) {
    badge.textContent = "未连接/回放";
    badge.className = "badge off";
  } else if (cur.in_pit) {
    badge.textContent = "进站中";
    badge.className = "badge paused";
  } else {
    badge.textContent = "LIVE";
    badge.className = "badge live";
  }
}

/* 会话统计条：理论最快圈（Ideal）= 各段最快相加。
   注意：AC 的 S1/S2/S3 是游戏官方记时段（部分赛道三段和≠整圈，属正常，
   如匈牙利 4 段循环只报 3 个记时段），理论最快仍按三段最优直接相加。 */
function renderLapsStats(laps) {
  const el = $("laps-stats");
  if (!el) return;
  if (!laps || laps.length === 0) { el.innerHTML = ""; return; }
  // 有效圈：排除出场圈/进站圈/未完成（出场圈含维修区段，会污染段统计）
  const valid = laps.filter(l => l.is_valid && !l.outlap && !l.is_inlap
    && l.total_ms > 0 && l.s1_ms > 0 && l.s2_ms > 0 && l.s3_ms > 0);
  if (!valid.length) {
    el.innerHTML = '<span class="st dim">暂无有效圈数据</span>';
    return;
  }
  const best = valid.reduce((a, b) => b.total_ms < a.total_ms ? b : a);
  const avg = valid.reduce((s, l) => s + l.total_ms, 0) / valid.length;
  const slow = valid.reduce((a, b) => b.total_ms > a.total_ms ? b : a);
  const span = slow.total_ms - best.total_ms;     // 最快↔最慢差
  // 三段覆盖全圈才有"理论最快"意义：三段和与整圈差 >5s 说明赛道扇区线未覆盖全圈
  // （部分 mod 赛道如 ts_hungaroringACC 三段和 < 整圈 20s+，此时理论最快无意义）
  const segOk = valid.filter(l =>
    Math.abs(l.s1_ms + l.s2_ms + l.s3_ms - l.total_ms) <= 5000);
  const parts = [
    `<span class="st">最快 <b>${fmtMs(best.total_ms)}</b> <i>圈${best.lap_no}</i></span>`,
  ];
  if (segOk.length) {
    const bS1 = segOk.reduce((a, b) => b.s1_ms < a.s1_ms ? b : a);
    const bS2 = segOk.reduce((a, b) => b.s2_ms < a.s2_ms ? b : a);
    const bS3 = segOk.reduce((a, b) => b.s3_ms < a.s3_ms ? b : a);
    const ideal = bS1.s1_ms + bS2.s2_ms + bS3.s3_ms;
    // 提升空间 = 实际最快 - 理论（>0 表示还能快这么多；每段最快拼凑几乎总快于任何单圈，
    // 所以"达成"只有当某一圈同时包含全部最快段时才成立，基本不会出现）
    const gap = best.total_ms - ideal;
    parts.push(
      `<span class="st">理论最快 <b>${fmtMs(ideal)}</b> <i>(S1 圈${bS1.lap_no} + S2 圈${bS2.lap_no} + S3 圈${bS3.lap_no})</i></span>`,
      gap > 0
        ? `<span class="st">提升空间 <b class="up">${fmtMs(gap)}</b></span>`
        : `<span class="st">已达成 <b class="dn">理论值</b></span>`,
    );
  } else {
    parts.push('<span class="st">理论最快 <b class="dim">—</b> <i>（此赛道扇区线未覆盖全圈，无法计算）</i></span>');
  }
  parts.push(
    `<span class="st sep">│</span>`,
    `<span class="st">有效 <b>${valid.length}</b>/${laps.length} 圈</span>`,
    `<span class="st">平均 <b>${fmtMs(avg)}</b></span>`,
    `<span class="st">最快↔最慢 <b>${fmtMs(span)}</b></span>`,
  );
  el.innerHTML = parts.join("");
}

function renderLaps(laps) {
  renderLapsStats(laps);   // 顶部统计条（理论最快/平均/稳定性）
  // 保留已勾选的圈（pollLaps 每几秒重建表格，避免勾选被重置）
  const prevSelected = new Set(
    [...document.querySelectorAll(".lap-chk:checked")].map(c => c.dataset.id));
  const tbody = document.querySelector("#laps-table tbody");
  tbody.innerHTML = "";
  if (!laps || laps.length === 0) {
    tbody.innerHTML = "<tr><td colspan='9' class='dim'>暂无圈数据</td></tr>";
    return;
  }
  let best = Infinity;
  for (const l of laps) if (l.is_valid && l.total_ms && l.total_ms < best) best = l.total_ms;
  // 各段最快记时段（紫色高亮用）：只在有效圈里取最小，排除出场圈/未完成
  let minS1 = Infinity, minS2 = Infinity, minS3 = Infinity;
  for (const l of laps) {
    if (!(l.is_valid && !l.outlap && !l.is_inlap && l.total_ms > 0)) continue;
    if (l.s1_ms > 0 && l.s1_ms < minS1) minS1 = l.s1_ms;
    if (l.s2_ms > 0 && l.s2_ms < minS2) minS2 = l.s2_ms;
    if (l.s3_ms > 0 && l.s3_ms < minS3) minS3 = l.s3_ms;
  }
  // 秒差大卡片：最后有效圈 vs 最快圈
  const validLaps = laps.filter(l => l.is_valid && l.total_ms);
  if (validLaps.length >= 1 && best !== Infinity) {
    const last = validLaps[validLaps.length - 1];
    const gap = last.total_ms - best;
    const sign = gap > 0 ? "+" : (gap < 0 ? "−" : "±");
    $("t-gap").textContent = `${sign}${fmtMs(Math.abs(gap))}  (末圈 ${fmtMs(last.total_ms)} / 最快 ${fmtMs(best)})`;
  } else {
    $("t-gap").textContent = "--:--.---";
  }
  // 重复圈号加后缀（5a/5b）：同一圈号可能有多段数据（重开/未完成分段）
  const lapNoCount = {};
  for (const l of laps) {
    lapNoCount[l.lap_no] = (lapNoCount[l.lap_no] || 0) + 1;
  }
  const lapNoSeen = {};
  for (const l of laps) {
    const tr = document.createElement("tr");
    const isUnfinished = !l.total_ms || l.total_ms <= 0;
    const isBest = l.total_ms === best;
    if (isBest) tr.className = "best-row";
    if (l.outlap || l.is_inlap) tr.classList.add("pit-row");
    if (isUnfinished) tr.classList.add("pit-row");
    if (replayMode && l.lap_no === replayLapNo) tr.classList.add("replay-row");
    lapNoSeen[l.lap_no] = (lapNoSeen[l.lap_no] || 0) + 1;
    let label = String(l.lap_no);
    if (lapNoCount[l.lap_no] > 1) label += "abcdefgh"[lapNoSeen[l.lap_no] - 1];
    let status;
    if (isUnfinished) status = "未完成";
    else if (l.outlap) status = "出场圈";
    else if (l.is_inlap) status = "进站圈";
    else status = l.is_valid ? "有效" : "无效/重开";
    const segCls = (v, m) => (v > 0 && v === m) ? "num best-seg" : "num";
    tr.innerHTML = `
      <td><input type="checkbox" class="lap-chk" data-id="${l.id}" data-no="${label}" data-total="${l.total_ms || 0}" ${isUnfinished ? "disabled" : ""}></td>
      <td>${label}</td>
      <td class="num">${isUnfinished ? "—" : fmtMs(l.total_ms)}</td>
      <td class="${segCls(l.s1_ms, minS1)}">${isUnfinished ? "—" : fmtMs(l.s1_ms)}</td>
      <td class="${segCls(l.s2_ms, minS2)}">${isUnfinished ? "—" : fmtMs(l.s2_ms)}</td>
      <td class="${segCls(l.s3_ms, minS3)}">${isUnfinished ? "—" : fmtMs(l.s3_ms)}</td>
      <td class="num">${(l.top_speed_kmh || 0).toFixed(1)}</td>
      <td>${status}</td>
      <td>${isUnfinished
        ? '<button class="dl" disabled title="该圈未完成，无法生成报告">报告</button>'
        : `<button class="dl" onclick="event.stopPropagation(); downloadLap(${l.id})">报告</button>`}</td>`;
    tr.title = "点击回放该圈遥测";
    tr.style.cursor = "pointer";
    const chk = tr.querySelector(".lap-chk");
    if (chk) {
      if (prevSelected.has(String(l.id))) chk.checked = true;
      chk.addEventListener("change", (e) => {
        e.stopPropagation();
        updateCompareState();
      });
    }
    tr.onclick = () => loadLap(l);
    tbody.appendChild(tr);
  }
  updateCompareState();
}

async function downloadLap(lapId) {
  try {
    // 帧批量落库：刚完成的圈可能帧未写完，重试最多 3 次等落库
    let r = null;
    for (let attempt = 0; attempt < 4; attempt++) {
      r = await fetch(`/api/laphtml/${lapId}`);
      if (r.ok) break;
      if (attempt < 3) await new Promise(res => setTimeout(res, 1000));
    }
    if (!r.ok) { alert("下载失败（帧可能还在写入，稍后再试）"); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `lap_${lapId}.html`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert("下载失败: " + e); }
}

async function cleanupSessions() {
  const keepStr = prompt("保留最近几个会话？（更早的会话将被永久删除，无法恢复）", "3");
  if (keepStr === null) return;
  const keep = parseInt(keepStr, 10);
  if (isNaN(keep) || keep < 1) { alert("请输入 ≥1 的整数"); return; }
  if (!confirm(`确定永久删除最旧的会话、只保留最近 ${keep} 个吗？\n（当前正在录制的会话不会被删）`)) return;
  try {
    const r = await fetch(`/api/cleanup?keep=${keep}`);
    const d = await r.json();
    alert(`清理完成：删除 ${d.deleted} 个旧会话，释放 ${(d.freed_frames * 0.4 / 1048576).toFixed(1)} MB 帧数据`);
    pollLaps(); pollSession();
  } catch (e) {
    alert("清理失败: " + e);
  }
}

/* ---------------- 圈回放：点击最近圈行查看该圈遥测 ---------------- */
let replayMode = false;
let replayLapNo = null;

async function loadLap(lap) {
  try {
    // 帧是批量落库的：刚完成的圈圈记录已入库但帧可能还在缓冲区，
    // 自动重试最多 3 次（每次等 1s）等帧写库，避免误报"无数据"
    let data = null;
    for (let attempt = 0; attempt < 4; attempt++) {
      const r = await fetch(`/api/lap/${lap.id}`);
      data = await r.json();
      if (data.frames && data.frames.length > 0) break;
      if (attempt < 3) await new Promise(res => setTimeout(res, 1000));
    }
    if (!data.frames || data.frames.length === 0) {
      alert("该圈还没有数据（帧可能还在写入，稍后再试）");
      return;
    }
    replayMode = true;
    replayLapNo = lap.lap_no;
    resetRepos();
    curLapNo = lap.lap_no;
    // 回放：距离基准 = 首帧累计距离（X 轴显示圈内距离）
    if (data.frames[0].dist != null) lapDistBase = data.frames[0].dist;
    for (const fr of data.frames) ingestFrame(fr);
    // 回放：X 轴跟随该圈实际数据范围（避免异常圈 total 过大/数据起点不为 0 导致大片空白）
    let xmin = Infinity, xmax = -Infinity;
    for (const fr of data.frames) {
      const x = (fr.dist != null && lapDistBase != null) ? (fr.dist - lapDistBase) : fr.lap_ms;
      if (x != null && !isNaN(x)) {
        if (x < xmin) xmin = x;
        if (x > xmax) xmax = x;
      }
    }
    if (xmin === Infinity) { xmin = 0; xmax = 0; }
    setPressureRange(lap.car || currentCar);   // 回放圈可能来自其他车型，按该圈车型定胎压轴
    for (const k of Object.keys(charts)) {
      charts[k].options.scales.x.min = xmin;
      charts[k].options.scales.x.max = xmax;
    }
    for (const k of Object.keys(charts)) refreshChart(k);
    // 顶部数值卡片同步为该圈末帧数据（回放态）
    const last = data.frames[data.frames.length - 1];
    updateTiles({
      live: false, in_pit: false,
      lap_no: lap.lap_no, lap_ms: last.lap_ms, sector: last.sector,
      last_lap_ms: lap.total_ms, best_lap_ms: null, completed_laps: lap.lap_no,
      speed_kmh: last.speed, gear: last.gear, rpms: last.rpms,
      kers_charge: last.kers, fuel: last.fuel, steer_deg: last.steer,
    });
    $("replay-hint").textContent = `· 回放中：圈 ${lap.lap_no}（${fmtMs(lap.total_ms)}）— 点击这里回到实时`;
    $("replay-hint").classList.add("replay-on");
  } catch (e) {
    alert("回放加载失败: " + e);
  }
}

function backToLive() {
  replayMode = false;
  replayLapNo = null;
  resetRepos();
  curLapNo = -1;
  afterSeq = 0;
  lapDistBase = 0;
  for (const k of Object.keys(charts)) {
    delete charts[k].options.scales.x.min;
    delete charts[k].options.scales.x.max;
  }
  $("replay-hint").textContent = "";
  $("replay-hint").classList.remove("replay-on");
  setPressureRange(currentCar);   // 回到实时：恢复当前会话车型的胎压轴
  refreshChart("pressure");
}

/* ---------------- 轮询 ---------------- */
async function pollLive() {
  try {
    const r = await fetch(`/api/live?after=${afterSeq}`);
    const data = await r.json();
    lastDataT = Date.now();
    $("banner").classList.add("hidden");
    if (replayMode) return;   // 回放中冻结实时曲线与数值卡片（保持该圈末帧数据）
    updateTiles(data.current);
    if (data.full || !afterSeq) {
      resetRepos();
      curLapNo = -1;
    }
    for (const fr of data.frames) ingestFrame(fr);
    afterSeq = data.latest_seq || afterSeq;
    for (const k of Object.keys(charts)) refreshChart(k);
    if (data.full) afterSeq = data.latest_seq;
  } catch (e) {
    if (Date.now() - lastDataT > 5000) $("banner").classList.remove("hidden");
  }
}

/* ---------------- 两圈对比 ---------------- */
let cmpCharts = {};

function updateCompareState() {
  const sel = [...document.querySelectorAll(".lap-chk:checked")];
  if (sel.length > 2) {
    sel[sel.length - 1].checked = false;   // 最多选两圈
  }
  const n = document.querySelectorAll(".lap-chk:checked").length;
  $("btn-compare").disabled = n !== 2;
  $("cmp-hint").textContent = n === 2
    ? "已选两圈，点击「对比」查看速度与秒差曲线"
    : "勾选任意两圈（勾选框），对比秒差曲线";
}

async function compareLaps() {
  const sel = [...document.querySelectorAll(".lap-chk:checked")];
  if (sel.length !== 2) return;
  const idA = sel[0].dataset.id, idB = sel[1].dataset.id;
  const [d1, d2] = await Promise.all([
    fetch(`/api/lap/${idA}`).then(r => r.json()),   // 全量帧，传输走服务端 gzip 无损压缩
    fetch(`/api/lap/${idB}`).then(r => r.json()),
  ]);
  const f1 = d1.frames || [], f2 = d2.frames || [];
  if (f1.length < 10 || f2.length < 10) { alert("所选圈数据不足，无法对比"); return; }
  // 快慢判定
  const t1 = (d1.lap.total_ms || 0), t2 = (d2.lap.total_ms || 0);
  const fast = t1 <= t2 ? { lap: d1.lap, fr: f1, tag: sel[0].dataset.no }
                        : { lap: d2.lap, fr: f2, tag: sel[1].dataset.no };
  const slow = t1 <= t2 ? { lap: d2.lap, fr: f2, tag: sel[1].dataset.no }
                        : { lap: d1.lap, fr: f1, tag: sel[0].dataset.no };
  const pts = alignFrames(fast.fr, slow.fr, fast.lap.total_ms / 1000, slow.lap.total_ms / 1000);
  if (!pts || pts.length < 10) { alert("两圈距离数据无法对齐（需有距离字段）"); return; }
  renderCompare(pts, fast, slow);
}

/* 两圈按距离对齐：统一 10m 网格插值，输出 {d, tA, tB, vA, vB}
   自适应处理 distanceTraveled 末端缺失：终点取两圈较远者，较短圈的缺失段
   从最后数据点线性补全到该圈真实圈速 → 秒差曲线终点天然等于两圈总差（任意赛道通用）。 */
function alignFrames(fastFr, slowFr, fastTotal, slowTotal, stepM = 10) {
  const prep = (frames) => {
    const valid = frames.filter(f => f.dist > 0 && f.lap_ms != null && f.speed != null);
    if (valid.length < 2) return null;
    const base = valid[0].dist;
    // ERS 瞬时功率：电量差分（0~1）× 电池容量 → kW（与实时图口径一致；kersCharge 可靠）
    let prevKers = null, prevT = null;
    return valid.map(f => {
      let ers = null;
      if (f.kers != null && prevKers != null && prevT != null && f.t > prevT) {
        ers = -(f.kers - prevKers) / (f.t - prevT) * ERS_BATTERY_KJ;
      }
      if (f.kers != null) { prevKers = f.kers; prevT = f.t; }
      const ag = f.accG || null;   // 后端输出 [纵向, 横向, 垂直]
      return { d: (f.dist - base) / 1000, t: f.lap_ms / 1000, v: f.speed,
               gas: f.gas, brake: f.brake, turbo: f.turbo, ers,
               accX: ag ? ag[0] : null, accY: ag ? ag[1] : null,
               steer: f.steer };   // 方向盘角度 °（左负右正）
    });
  };
  const A = prep(fastFr), B = prep(slowFr);
  if (!A || !B) return null;
  // 终点取两圈较远者；较短圈缺失段外推补全
  const maxD = Math.max(A[A.length - 1].d, B[B.length - 1].d);
  if (maxD < 0.5) return null;
  const step = stepM / 1000;
  // 线性插值：二分定位区间（数组按 d 升序，O(log n)，原线性搜索 2 万帧时较慢）
  const interp = (arr, d, key, total) => {
    let lo = 0, hi = arr.length - 1;
    if (d <= arr[0].d) return arr[0][key];
    if (d >= arr[hi].d) {
      // 超出数据末端（distanceTraveled 缺失）：从最后数据点线性补全到真实圈速
      if (key === "t" && total != null) {
        const last = arr[hi];
        const span = maxD - last.d;
        if (span > 0.001) {
          return last.t + (total - last.t) * (d - last.d) / span;
        }
      }
      return arr[hi][key];
    }
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (arr[mid].d < d) lo = mid; else hi = mid;
    }
    const a = arr[lo], b = arr[hi];
    const f = (b.d - a.d) ? (d - a.d) / (b.d - a.d) : 0;
    return a[key] + (b[key] - a[key]) * f;
  };
  const pts = [];
  for (let d = 0; d <= maxD; d += step) {
    pts.push({ d, tA: interp(A, d, "t", fastTotal), tB: interp(B, d, "t", slowTotal),
               vA: interp(A, d, "v", null), vB: interp(B, d, "v", null),
               gA: interp(A, d, "gas", null), gB: interp(B, d, "gas", null),
               bA: interp(A, d, "brake", null), bB: interp(B, d, "brake", null),
               tuA: interp(A, d, "turbo", null), tuB: interp(B, d, "turbo", null),
               erA: interp(A, d, "ers", null), erB: interp(B, d, "ers", null),
               axA: interp(A, d, "accX", null), axB: interp(B, d, "accX", null),
               ayA: interp(A, d, "accY", null), ayB: interp(B, d, "accY", null),
               stA: interp(A, d, "steer", null), stB: interp(B, d, "steer", null) });
  }
  // 网格步长可能错过精确终点：末尾强制追加 maxD，保证秒差终点=两圈总差
  if (pts.length === 0 || pts[pts.length - 1].d < maxD - step / 2) {
    pts.push({ d: maxD, tA: interp(A, maxD, "t", fastTotal), tB: interp(B, maxD, "t", slowTotal),
               vA: interp(A, maxD, "v", null), vB: interp(B, maxD, "v", null),
               gA: interp(A, maxD, "gas", null), gB: interp(B, maxD, "gas", null),
               bA: interp(A, maxD, "brake", null), bB: interp(B, maxD, "brake", null),
               tuA: interp(A, maxD, "turbo", null), tuB: interp(B, maxD, "turbo", null),
               erA: interp(A, maxD, "ers", null), erB: interp(B, maxD, "ers", null),
               axA: interp(A, maxD, "accX", null), axB: interp(B, maxD, "accX", null),
               ayA: interp(A, maxD, "accY", null), ayB: interp(B, maxD, "accY", null),
               stA: interp(A, maxD, "steer", null), stB: interp(B, maxD, "steer", null) });
  }
  return pts;
}

function renderCompare(pts, fast, slow) {
  $("cmp-panel").style.display = "block";   // 先显示面板，Chart.js 才能读到正确尺寸
  const X = pts.map(p => p.d);
  const TICK = "#8b949e", GRID = { color: "rgba(139,148,158,0.15)" };
  const base = {
    animation: false, responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: "#e6edf3", boxWidth: 10, font: { size: 10 } } } },
    scales: {
      x: { type: "linear", min: 0, max: X[X.length - 1] || 1,
           ticks: { color: TICK, maxTicksLimit: 6, callback: v => v >= 1 ? v.toFixed(1) + "km" : Math.round(v * 1000) + "m" },
           grid: GRID, title: { display: true, text: "圈内距离", color: TICK, font: { size: 10 } } },
      y: { ticks: { color: TICK }, grid: GRID },
    },
  };
  if (cmpCharts.speed) cmpCharts.speed.destroy();
  // 速度 Y 轴自适应：两圈速度主体 5%~95% 百分位定轴（差距放大可见），离群峰值保护
  const spdAll = pts.flatMap(p => [p.vA, p.vB]).filter(v => v != null && !isNaN(v));
  let sLo, sHi;
  if (spdAll.length >= 10) {
    const ss = [...spdAll].sort((a, b) => a - b);
    const nS = ss.length;
    const sp05 = ss[Math.max(0, Math.floor(nS * 0.05))];
    const sp95 = ss[Math.min(nS - 1, Math.floor(nS * 0.95))];
    const sSpan = (sp95 - sp05) || 10;
    sLo = sp05 - sSpan * 0.15;
    sHi = sp95 + sSpan * 0.15;
    const sMin = Math.min(...spdAll), sMax = Math.max(...spdAll);
    if (sMin < sLo) sLo = sMin - 2;    // 峰值保护：不截断
    if (sMax > sHi) sHi = sMax + 2;
  } else {
    sLo = 0; sHi = 300;
  }
  cmpCharts.speed = new Chart($("c-cmp-speed"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快 ${fast.tag}）`, data: pts.map(p => p.vA),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.6, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢 ${slow.tag}）`, data: pts.map(p => p.vB),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.2, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, min: sLo, max: sHi, title: { display: true, text: "km/h", color: TICK, font: { size: 10 } } } } }),
  });
  // 油门对比（快红/慢灰，固定 0~100）
  if (cmpCharts.gas) cmpCharts.gas.destroy();
  cmpCharts.gas = new Chart($("c-cmp-gas"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: pts.map(p => (p.gA != null ? p.gA * 100 : null)),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢）`, data: pts.map(p => (p.gB != null ? p.gB * 100 : null)),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, min: 0, max: 100, title: { display: true, text: "油门 %", color: TICK, font: { size: 10 } } } } }),
  });
  // 刹车对比（快红/慢灰，固定 0~100）
  if (cmpCharts.brake) cmpCharts.brake.destroy();
  cmpCharts.brake = new Chart($("c-cmp-brake"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: pts.map(p => (p.bA != null ? p.bA * 100 : null)),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢）`, data: pts.map(p => (p.bB != null ? p.bB * 100 : null)),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, min: 0, max: 100, title: { display: true, text: "刹车 %", color: TICK, font: { size: 10 } } } } }),
  });
  // 涡轮压力对比（快红/慢灰，跟随数据自适应）
  if (cmpCharts.turbo) cmpCharts.turbo.destroy();
  cmpCharts.turbo = new Chart($("c-cmp-turbo"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: pts.map(p => p.tuA),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢）`, data: pts.map(p => p.tuB),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, title: { display: true, text: "bar", color: TICK, font: { size: 10 } } } } }),
  });
  // ERS 放电功率对比（充电段画在 0 线保持连续；Y 轴从 0 起、上限拉长 20% 留白）
  if (cmpCharts.ers) cmpCharts.ers.destroy();
  const ersData = pts.map(p => [p.erA, p.erB]);
  let ersMax = 0;
  for (const [a, b] of ersData) {
    if (a != null && a > ersMax) ersMax = a;
    if (b != null && b > ersMax) ersMax = b;
  }
  const toDis = v => (v != null && v > 0 ? v : 0);   // 负值/空 → 0 线，曲线连续
  cmpCharts.ers = new Chart($("c-cmp-ers"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: pts.map(p => toDis(p.erA)),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢）`, data: pts.map(p => toDis(p.erB)),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, min: 0, max: (ersMax * 1.2) || 100, title: { display: true, text: "kW", color: TICK, font: { size: 10 } } } } }),
  });
  // 纵向 G 对比（快红/慢灰，跟随数据自适应，平滑噪声）
  if (cmpCharts.accx) cmpCharts.accx.destroy();
  cmpCharts.accx = new Chart($("c-cmp-accx"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: smooth(pts.map(p => p.axA), 3),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢）`, data: smooth(pts.map(p => p.axB), 3),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, title: { display: true, text: "纵向 G", color: TICK, font: { size: 10 } } } } }),
  });
  // 横向 G 对比（以 0 对称自适应，左右弯等幅显示）
  if (cmpCharts.acy) cmpCharts.acy.destroy();
  const gyAll = pts.flatMap(p => [p.ayA, p.ayB]).filter(v => v != null && !isNaN(v));
  const gyM = gyAll.length ? Math.max(...gyAll.map(v => Math.abs(v))) : 3;
  cmpCharts.acy = new Chart($("c-cmp-acy"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: smooth(pts.map(p => p.ayA), 3),
        borderColor: "#f85149", backgroundColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true },
      { label: `圈${slow.lap.lap_no}（慢）`, data: smooth(pts.map(p => p.ayB), 3),
        borderColor: "#8b949e", backgroundColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, min: -gyM * 1.15, max: gyM * 1.15, title: { display: true, text: "横向 G", color: TICK, font: { size: 10 } } } } }),
  });
  // 方向盘角度对比（左打红/右打绿；快圈实线、慢圈虚线；Y 轴跟随数据自适应）
  if (cmpCharts.steer) cmpCharts.steer.destroy();
  const steerSeg = { segment: { borderColor: ctx => {
    const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex;
    const v = ctx.chart.data.datasets[ctx.datasetIndex].data[i];
    return (v != null && v < 0) ? "#f85149" : "#2ea043";   // 左打红 / 右打绿
  } } };
  cmpCharts.steer = new Chart($("c-cmp-steer"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: `圈${fast.lap.lap_no}（快）`, data: pts.map(p => p.stA),
        borderColor: "#f85149", borderWidth: 1.4, pointRadius: 0, tension: 0.15, spanGaps: true,
        segment: steerSeg.segment },
      { label: `圈${slow.lap.lap_no}（慢）`, data: pts.map(p => p.stB),
        borderColor: "#8b949e", borderWidth: 1.1, pointRadius: 0, tension: 0.15, spanGaps: true,
        borderDash: [4, 3], segment: steerSeg.segment },
    ]},
    options: Object.assign({}, base, { scales: { ...base.scales,
      y: { ...base.scales.y, title: { display: true, text: "方向盘 °", color: TICK, font: { size: 10 } } } } }),
  });
  // 累计秒差：基准=快圈 0 线，慢圈每个距离点的累计用时 - 快圈累计用时
  const delta = pts.map(p => p.tB - p.tA);
  // 纵坐标自适应：以主体 5%~95% 百分位定轴（曲线占满），离群峰值时扩展保护峰值可见
  const sorted = [...delta].sort((a, b) => a - b);
  const nD = delta.length;
  const p05 = sorted[Math.max(0, Math.floor(nD * 0.05))];
  const p95 = sorted[Math.min(nD - 1, Math.floor(nD * 0.95))];
  let span = (p95 - p05) || 0.1;
  let lo = p05 - span * 0.2, hi = p95 + span * 0.2;
  if (lo > 0) lo = -span * 0.05;       // 保证基准 0 线在视野内
  if (hi < 0) hi = span * 0.05;
  let axis = Math.max(Math.abs(lo), Math.abs(hi));
  const maxAbs = Math.max(0.1, ...delta.map(Math.abs));
  if (maxAbs > axis) axis = maxAbs * 1.05;   // 峰值保护：不截断
  if (cmpCharts.delta) cmpCharts.delta.destroy();
  cmpCharts.delta = new Chart($("c-cmp-delta"), {
    type: "line",
    data: { labels: X, datasets: [
      { label: "快圈基准 0", data: Array(X.length).fill(0),
        borderColor: "#8b949e", borderDash: [6, 4], borderWidth: 1.2, pointRadius: 0, fill: false },
      { label: "慢圈 − 快圈（秒）", data: delta,
        borderColor: "#e3b341", backgroundColor: "#e3b341", borderWidth: 1.6, pointRadius: 0, tension: 0.15, spanGaps: true },
    ]},
    options: Object.assign({}, base, { scales: {
      ...base.scales,
      y: { ...base.scales.y, min: -axis, max: axis,
           ticks: { color: TICK, callback: v => (v > 0 ? "+" : "") + v.toFixed(2) + "s" },
           title: { display: true, text: "秒差", color: TICK, font: { size: 10 } } },
    },
    }),
  });
  const gap = slow.lap.total_ms - fast.lap.total_ms;
  $("cmp-hint").textContent =
    `快圈 圈${fast.lap.lap_no} ${fmtMs(fast.lap.total_ms)}，慢圈 圈${slow.lap.lap_no} ${fmtMs(slow.lap.total_ms)}，总差 +${(gap / 1000).toFixed(3)}s`;
}

async function pollLaps() {
  try {
    const r = await fetch("/api/laps");
    const data = await r.json();
    renderLaps(data.laps);
    if (data.session_id != null) $("s-session").textContent = `会话 #${data.session_id}`;
  } catch (e) { /* ignore */ }
}

async function pollSession() {
  try {
    const r = await fetch("/api/session");
    const data = await r.json();
    $("s-car").textContent = data.car || "—";
    setPressureRange(data.car);
    refreshChart("pressure");   // 车型可能变化，立即按新范围重绘
    $("s-track").textContent = data.track || "—";
    $("s-laps").textContent = data.laps_done ? `${data.laps_done} 圈` : "";
  } catch (e) { /* ignore */ }
}

setInterval(pollLive, POLL_MS);
setInterval(pollLaps, LAPS_POLL_MS);
setInterval(pollSession, SESSION_POLL_MS);
$("replay-hint").onclick = backToLive;
$("btn-compare").onclick = compareLaps;
$("btn-cleanup").onclick = cleanupSessions;
pollLive(); pollLaps(); pollSession();
