<template>
  <div id="cmp-panel" v-show="shown">
    <div style="font-size:12px;color:var(--dim);padding:6px 2px">点击卡片放大后，滚轮缩放 X 轴 · 按住拖拽平移</div>
    <div v-for="(g, i) in graphs" :key="g.key" class="card cmp-card" :class="{ zoom: g.zoomed }"
         :style="cmpStyle(g)" @click="toggleZoom(g, i)">
      <h3 v-html="g.title"></h3>
      <div class="cmp-box" :style="{ height: g.h + 'px', position: 'relative' }">
        <canvas :ref="el => setCv(i, el)" style="position:absolute;left:0;top:0;width:100%;height:100%"></canvas>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { Chart, registerables } from 'chart.js'
import zoomPlugin from 'chartjs-plugin-zoom'
import { on as subscribe } from '../store/live'
import { smooth } from '../lib/compareUtil'
import { fmtMs } from '../lib/fmt'

Chart.register(...registerables, zoomPlugin)

const shown = ref(false)
const hint = ref("")
const cvList = ref([])
const charts = {}
const graphs = ref([
  { key: "speed", h: 220, zoomed: false, title: '速度对比 km/h（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>）' },
  { key: "gas", h: 160, zoomed: false, title: '油门对比 %（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>）' },
  { key: "brake", h: 160, zoomed: false, title: '刹车对比 %（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>）' },
  { key: "turbo", h: 160, zoomed: false, title: '涡轮压力对比 bar（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>）' },
  { key: "ers", h: 160, zoomed: false, title: 'ERS 放电功率对比 kW（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>）' },
  { key: "accx", h: 160, zoomed: false, title: '纵向 G 对比（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>）' },
  { key: "acy", h: 160, zoomed: false, title: '横向 G 对比（快圈 <span style="color:#f85149">■</span> / 慢圈 <span style="color:#8b949e">■</span>，正负对称）' },
  { key: "steer", h: 160, zoomed: false, title: '方向盘角度 °（左打 <span style="color:#f85149">■</span> 右打 <span style="color:#2ea043">■</span>，快圈实线 / 慢圈虚线）' },
  { key: "delta", h: 220, zoomed: false, title: '累计秒差（基准=快圈 0 线，正值=慢圈落后）' },
])

function setCv(i, el) { if (el) cvList.value[i] = el }
function cmpStyle(g) { return { background: "var(--panel)", border: "1px solid var(--border)", borderRadius: "10px", padding: "12px", marginTop: "10px" } }

const TICK = "#8b949e", GRID = { color: "rgba(139,148,158,0.15)" }

function makeBase(xMax) {
  return {
    animation: false, responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: "#e6edf3", boxWidth: 10, font: { size: 10 } } },
      zoom: { zoom: { wheel: { enabled: false }, pinch: { enabled: false }, mode: "x" }, pan: { enabled: false, mode: "x" } },
    },
    scales: {
      x: { type: "linear", min: 0, max: xMax || 1,
           ticks: { color: TICK, maxTicksLimit: 6, callback: v => v >= 1 ? v.toFixed(1) + "km" : Math.round(v * 1000) + "m" },
           grid: GRID, title: { display: true, text: "圈内距离", color: TICK, font: { size: 10 } } },
      y: { ticks: { color: TICK }, grid: GRID },
    },
  }
}
function yTitle(o, text) { o.scales.y.title = { display: true, text, color: TICK, font: { size: 10 } }; return o }
function dsF(label, data, color, w, extra) {
  const d = { label, data, borderColor: color, backgroundColor: color, borderWidth: w, pointRadius: 0, tension: 0.15, spanGaps: true }
  if (extra) Object.assign(d, extra)
  return d
}

function onCmp({ pts, fast, slow }) {
  shown.value = true
  const X = pts.map(p => p.d)
  const base = makeBase(X[X.length - 1] || 1)
  const mk = (key, datasets, opts) => {
    const i = graphs.value.findIndex(g => g.key === key)
    if (charts[key]) charts[key].destroy()
    charts[key] = new Chart(cvList.value[i], { type: "line", data: { labels: X, datasets }, options: opts })
  }
  // 速度：Y 轴 5%~95% 百分位自适应 + 峰值保护
  const spdAll = pts.flatMap(p => [p.vA, p.vB]).filter(v => v != null && !isNaN(v))
  let sLo, sHi
  if (spdAll.length >= 10) {
    const ss = [...spdAll].sort((a, b) => a - b)
    const nS = ss.length
    const sp05 = ss[Math.max(0, Math.floor(nS * 0.05))]
    const sp95 = ss[Math.min(nS - 1, Math.floor(nS * 0.95))]
    const sSpan = (sp95 - sp05) || 10
    sLo = sp05 - sSpan * 0.15; sHi = sp95 + sSpan * 0.15
    const sMin = Math.min(...spdAll), sMax = Math.max(...spdAll)
    if (sMin < sLo) sLo = sMin - 2
    if (sMax > sHi) sHi = sMax + 2
  } else { sLo = 0; sHi = 300 }
  mk("speed", [
    dsF(`圈${fast.lap.lap_no}（快 ${fast.tag}）`, pts.map(p => p.vA), "#f85149", 1.6),
    dsF(`圈${slow.lap.lap_no}（慢 ${slow.tag}）`, pts.map(p => p.vB), "#8b949e", 1.2),
  ], yTitle(Object.assign({}, base, { scales: { ...base.scales, y: { ...base.scales.y, min: sLo, max: sHi } } }), "km/h"))
  mk("gas", [
    dsF(`圈${fast.lap.lap_no}（快）`, pts.map(p => (p.gA != null ? p.gA * 100 : null)), "#f85149", 1.4),
    dsF(`圈${slow.lap.lap_no}（慢）`, pts.map(p => (p.gB != null ? p.gB * 100 : null)), "#8b949e", 1.1),
  ], yTitle(Object.assign({}, base, { scales: { ...base.scales, y: { ...base.scales.y, min: 0, max: 100 } } }), "油门 %"))
  mk("brake", [
    dsF(`圈${fast.lap.lap_no}（快）`, pts.map(p => (p.bA != null ? p.bA * 100 : null)), "#f85149", 1.4),
    dsF(`圈${slow.lap.lap_no}（慢）`, pts.map(p => (p.bB != null ? p.bB * 100 : null)), "#8b949e", 1.1),
  ], yTitle(Object.assign({}, base, { scales: { ...base.scales, y: { ...base.scales.y, min: 0, max: 100 } } }), "刹车 %"))
  mk("turbo", [
    dsF(`圈${fast.lap.lap_no}（快）`, pts.map(p => p.tuA), "#f85149", 1.4),
    dsF(`圈${slow.lap.lap_no}（慢）`, pts.map(p => p.tuB), "#8b949e", 1.1),
  ], yTitle(Object.assign({}, base), "bar"))
  // ERS：负值/空 → 0 线
  let ersMax = 0
  for (const p of pts) { if (p.erA != null && p.erA > ersMax) ersMax = p.erA; if (p.erB != null && p.erB > ersMax) ersMax = p.erB }
  const toDis = v => (v != null && v > 0 ? v : 0)
  mk("ers", [
    dsF(`圈${fast.lap.lap_no}（快）`, pts.map(p => toDis(p.erA)), "#f85149", 1.4),
    dsF(`圈${slow.lap.lap_no}（慢）`, pts.map(p => toDis(p.erB)), "#8b949e", 1.1),
  ], yTitle(Object.assign({}, base, { scales: { ...base.scales, y: { ...base.scales.y, min: 0, max: (ersMax * 1.2) || 100 } } }), "kW"))
  mk("accx", [
    dsF(`圈${fast.lap.lap_no}（快）`, smooth(pts.map(p => p.axA), 3), "#f85149", 1.4),
    dsF(`圈${slow.lap.lap_no}（慢）`, smooth(pts.map(p => p.axB), 3), "#8b949e", 1.1),
  ], yTitle(Object.assign({}, base), "纵向 G"))
  const gyAll = pts.flatMap(p => [p.ayA, p.ayB]).filter(v => v != null && !isNaN(v))
  const gyM = gyAll.length ? Math.max(...gyAll.map(v => Math.abs(v))) : 3
  mk("acy", [
    dsF(`圈${fast.lap.lap_no}（快）`, smooth(pts.map(p => p.ayA), 3), "#f85149", 1.4),
    dsF(`圈${slow.lap.lap_no}（慢）`, smooth(pts.map(p => p.ayB), 3), "#8b949e", 1.1),
  ], yTitle(Object.assign({}, base, { scales: { ...base.scales, y: { ...base.scales.y, min: -gyM * 1.15, max: gyM * 1.15 } } }), "横向 G"))
  // 方向盘：左打红/右打绿
  const steerSeg = { segment: { borderColor: ctx => {
    const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex
    const v = ctx.chart.data.datasets[ctx.datasetIndex].data[i]
    return (v != null && v < 0) ? "#f85149" : "#2ea043"
  } } }
  mk("steer", [
    dsF(`圈${fast.lap.lap_no}（快）`, pts.map(p => p.stA), "#f85149", 1.4, steerSeg),
    dsF(`圈${slow.lap.lap_no}（慢）`, pts.map(p => p.stB), "#8b949e", 1.1, { borderDash: [4, 3], ...steerSeg }),
  ], yTitle(Object.assign({}, base), "方向盘 °"))
  // 累计秒差
  const delta = pts.map(p => p.tB - p.tA)
  const sorted = [...delta].sort((a, b) => a - b)
  const nD = delta.length
  const p05 = sorted[Math.max(0, Math.floor(nD * 0.05))]
  const p95 = sorted[Math.min(nD - 1, Math.floor(nD * 0.95))]
  let span = (p95 - p05) || 0.1
  let lo = p05 - span * 0.2, hi = p95 + span * 0.2
  if (lo > 0) lo = -span * 0.05
  if (hi < 0) hi = span * 0.05
  let axis = Math.max(Math.abs(lo), Math.abs(hi))
  const maxAbs = Math.max(0.1, ...delta.map(Math.abs))
  if (maxAbs > axis) axis = maxAbs * 1.05
  mk("delta", [
    dsF("快圈基准 0", Array(X.length).fill(0), "#8b949e", 1.2, { borderDash: [6, 4], fill: false }),
    dsF("慢圈 − 快圈（秒）", delta, "#e3b341", 1.6),
  ], Object.assign({}, base, { scales: { ...base.scales, y: { ...base.scales.y, min: -axis, max: axis,
        ticks: { color: TICK, callback: v => (v > 0 ? "+" : "") + v.toFixed(2) + "s" } } } }))
  const gap = slow.lap.total_ms - fast.lap.total_ms
  hint.value = `快圈 圈${fast.lap.lap_no} ${fmtMs(fast.lap.total_ms)}，慢圈 圈${slow.lap.lap_no} ${fmtMs(slow.lap.total_ms)}，总差 +${(gap / 1000).toFixed(3)}s`
}

function toggleZoom(g, i, ev) {
  // 点击图例切换曲线显隐时，不应触发放大
  if (ev && ev.target === charts[g.key]?.canvas && charts[g.key]?.chartArea) {
    const r = ev.target.getBoundingClientRect()
    if (ev.clientY - r.top < charts[g.key].chartArea.top) return
  }
  g.zoomed = !g.zoomed
  const ch = charts[g.key]
  if (ch && ch.options.plugins && ch.options.plugins.zoom) {
    const z = ch.options.plugins.zoom
    z.zoom.wheel.enabled = g.zoomed
    z.zoom.pinch.enabled = g.zoomed
    z.pan.enabled = g.zoomed
    if (g.zoomed) {
      const sx = ch.scales.x
      if (sx && sx.max != null && sx.min != null && sx.max > sx.min) z.limits = { x: { min: sx.min, max: sx.max } }
      if (ch.resetZoom) ch.resetZoom()
    } else {
      delete z.limits
      if (ch.resetZoom) ch.resetZoom()
    }
    ch.update("none")
  }
}

onMounted(() => subscribe('cmp', onCmp))
onUnmounted(() => { for (const k of Object.keys(charts)) charts[k].destroy() })
</script>

<style scoped>
.cmp-card { cursor: pointer; }
.cmp-card.zoom { position: fixed; inset: 12px; z-index: 200; width: auto; height: auto; overflow: auto; }
.cmp-card.zoom h3 { font-size: 15px; color: var(--blue); }
.cmp-card.zoom .cmp-box { height: calc(100vh - 130px) !important; }
</style>
