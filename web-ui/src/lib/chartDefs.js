// 图表定义配置（从 v1.2 app.js 迁移）
import { fmtDist } from './fmt'

export const CHART_COLORS = {
  speed: "#58a6ff", rpm: "#d2a8ff", gear: "#8b949e",
  gas: "#2ea043", brake: "#f85149", steer: "#e3b341",
  susp: ["#58a6ff", "#f0883e", "#3fb950", "#da3633"],
  ride: ["#f0f6fc", "#f0883e"],
  kers: "#7ee787", fuel: "#e3b341",
  accX: "#58a6ff", accY: "#f85149",
  tyre: ["#58a6ff", "#f0883e", "#3fb950", "#da3633"],
  turbo: "#d2a8ff",
}
export const WHEELS = ["FL", "FR", "RL", "RR"]
export const ERS_BATTERY_KJ = 4000

// 仪表盘图表配置：ChartCard 组件按此创建图表
export const CHART_DEFS = [
  { key: "speed", title: "车速 (km/h)", unit: "km/h", auto: { mode: "pad", zero: true }, smooth: 3,
    datasets: [{ key: "speed", label: "车速 km/h", color: CHART_COLORS.speed }] },
  { key: "rpm", title: "转速 (rpm)", unit: "rpm", auto: { mode: "pad", zero: true },
    datasets: [{ key: "rpm", label: "转速 rpm", color: CHART_COLORS.rpm }] },
  { key: "gear", title: "挡位", unit: "档", auto: { mode: "fixed", min: 1, max: 8 }, smooth: 1,   // 显示 1~8 档（数据已 -1 换算）
    datasets: [{ key: "gear", label: "档位", color: CHART_COLORS.gear }] },
  { key: "pedals", title: "油门 / 刹车 (%)", unit: "%", auto: { mode: "fixed", min: 0, max: 100 },
    datasets: [
      { key: "gas", label: "油门 %", color: CHART_COLORS.gas },
      { key: "brake", label: "刹车 %", color: CHART_COLORS.brake },
    ] },
  { key: "susp", title: "悬架行程 (mm)", unit: "mm", auto: { mode: "pad" },
    datasets: WHEELS.map((w, i) => ({ key: w, label: `悬架 ${w} mm`, color: CHART_COLORS.susp[i] })) },
  { key: "ride", title: "底板高度 (mm)", unit: "mm", auto: { mode: "pad" },
    datasets: [
      { key: "F", label: "底板 前 mm", color: CHART_COLORS.ride[0] },
      { key: "R", label: "底板 后 mm", color: CHART_COLORS.ride[1] },
    ] },
  { key: "kers", title: "电量 / 油量 (%)", unit: "%", auto: { mode: "fixed", min: 1, max: 100 },
    datasets: [
      { key: "kers", label: "KERS 电量 %", color: CHART_COLORS.kers },
      { key: "fuel", label: "油量 %", color: CHART_COLORS.fuel },
    ] },
  { key: "kerskj", title: "KERS 充放电 (kW)", unit: "kW", auto: { mode: "pad", zero: true }, smooth: 5,
    datasets: [{ key: "pwr", label: "充放电 kW", color: "#d2a8ff", segment: seg => seg >= 0 ? "#f85149" : "#7ee787" }] },
  { key: "accg", title: "加速度 (g)", unit: "g", secondAxis: "g",
    auto: { mode: "fixed", min: -6, max: 6 }, auto1: { mode: "sym", padAbs: 0.5 }, smooth: 5,
    datasets: [
      { key: "X", label: "纵向 g", color: CHART_COLORS.accX },
      { key: "Y", label: "横向 g", color: CHART_COLORS.accY, yAxis: "y1" },
    ] },
  { key: "steer", title: "方向盘角度 °（左打红 / 右打绿）", unit: "°", auto: { mode: "pad" },
    datasets: [{ key: "steer", label: "方向盘 °", color: "#e3b341", segment: v => (v != null && v < 0) ? "#f85149" : "#2ea043" }] },
  { key: "tyre", title: "胎温 (°C)", unit: "°C", auto: { mode: "pad" },
    datasets: WHEELS.map((w, i) => ({ key: w, label: `胎温 ${w} °C`, color: CHART_COLORS.tyre[i] })) },
  { key: "tyreO", title: "外层胎温 (°C)", unit: "°C", auto: { mode: "pad" }, initialVisible: false,
    datasets: WHEELS.map((w, i) => ({ key: w, label: `外层胎温 ${w} °C`, color: CHART_COLORS.tyre[i] })) },
  { key: "brakeT", title: "刹车盘温 (°C)", unit: "°C", auto: { mode: "pad", loCap: 0 }, initialVisible: false,
    datasets: WHEELS.map((w, i) => ({ key: w, label: `刹车盘温 ${w} °C`, color: CHART_COLORS.tyre[i] })) },
  { key: "wear", title: "轮胎磨损 (%)", unit: "%", auto: { mode: "pad" }, initialVisible: false,
    datasets: WHEELS.map((w, i) => ({ key: w, label: `磨损 ${w} %`, color: CHART_COLORS.tyre[i] })) },
  { key: "slip", title: "滑移率 (%)", unit: "%", auto: { mode: "pad", zero: true },
    datasets: WHEELS.map((w, i) => ({ key: w, label: `滑移率 ${w} %`, color: CHART_COLORS.tyre[i] })) },
  { key: "pressure", title: "胎压 (psi)", unit: "psi", auto: { mode: "pad", loCap: 0 }, smooth: 3,
    datasets: WHEELS.map((w, i) => ({ key: w, label: `胎压 ${w} psi`, color: CHART_COLORS.tyre[i] })) },
  { key: "turbo", title: "涡轮增压 (bar)", unit: "bar", auto: { mode: "pad", loCap: 0 }, smooth: 3,
    datasets: [{ key: "turbo", label: "涡轮增压 bar", color: CHART_COLORS.turbo }] },
]

// Chart.js 基础 options（对比图也用它，由 ComparePanel 再定制）
export function baseOptions(unit, secondAxis, tickColor = "#8b949e", gridColor = "rgba(139,148,158,0.15)") {
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
      zoom: {
        zoom: { wheel: { enabled: false }, pinch: { enabled: false }, mode: "x" },
        pan: { enabled: false, mode: "x" },
      },
    },
  }
  if (secondAxis) {
    o.scales.y1 = { position: "right", ticks: { color: tickColor }, grid: { drawOnChartArea: false },
                    title: { display: true, text: secondAxis, color: tickColor, font: { size: 10 } } }
  }
  return o
}
