<template>
  <div class="card" :class="{ zoom: zoomed }" @click="toggleZoom" ref="cardEl">
    <h2>{{ def.title }}<span v-if="def.key === 'kerskj'" class="hint"> 正=放电 负=充电</span></h2>
    <div class="chart-wrap"><canvas ref="cv"></canvas></div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { Chart, registerables } from 'chart.js'
import zoomPlugin from 'chartjs-plugin-zoom'
import { baseOptions } from '../lib/chartDefs'
import { regChart, unregChart, chartHover, chartLeave } from '../store/live'

Chart.register(...registerables, zoomPlugin)

const props = defineProps({ def: { type: Object, required: true } })
const cv = ref(null)
let chart = null
const zoomed = ref(false)

// def.datasets[].segment 为函数 (值)=>颜色，转成 Chart.js segment 配置
function makeDatasets() {
  return props.def.datasets.map(d => {
    const ds = {
      label: d.label, borderColor: d.color, backgroundColor: d.color,
      data: [], borderWidth: 1.5, pointRadius: 0,
      pointHoverRadius: 0,   // 关掉原生 hover 圆点（与自定义游标二选一，避免两套打架）
      tension: 0.15, spanGaps: true,
      _key: d.key, _repoKey: d.key,
    }
    if (d.yAxis) ds.yAxisID = d.yAxis
    if (d.segment) {
      ds.segment = { borderColor: ctx => {
        const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex
        const v = ctx.chart.data.datasets[ctx.datasetIndex].data[i]
        return d.segment(v)
      } }
    }
    return ds
  })
}

// 游标插值：x 落在两个采样点之间时线性插值，null 值取另一侧
function yAt(labels, data, x) {
  const n = labels.length
  if (!n || !data || !data.length) return null
  const m = data.length
  const val = i => data[Math.min(i, m - 1)]
  if (x <= labels[0]) return val(0)
  if (x >= labels[n - 1]) return val(n - 1)
  let lo = 0, hi = n - 1
  while (lo < hi - 1) { const mid = (lo + hi) >> 1; if (labels[mid] < x) lo = mid; else hi = mid }
  const a = val(lo), b = val(hi)
  const na = a == null || isNaN(a), nb = b == null || isNaN(b)
  if (na && nb) return null
  if (na) return b
  if (nb) return a
  const f = (x - labels[lo]) / ((labels[hi] - labels[lo]) || 1)
  return a + (b - a) * f
}

onMounted(() => {
  const o = baseOptions(props.def.unit, props.def.secondAxis)
  // 游标式 hover 联动（MoTeC 风格十字线）：
  // 鼠标在整张卡片任意位置 → 竖线钉在鼠标 x 像素（永远跟手，不吸附数据点），
  // 各可见曲线按当前数据插值画圆点，顶部数字条显示该 x 最近一帧数据。
  // 渲染走 rAF + chart.draw() 轻量重绘（不走 update，不与实时数据刷新抢渲染）
  const cursorPlugin = {
    id: "cursor",
    afterDatasetsDraw(chart) {
      if (chart._cursorPx == null) return
      const area = chart.chartArea
      if (!area) return
      const px = Math.max(area.left, Math.min(area.right, chart._cursorPx))
      const x = chart.scales.x.getValueForPixel(px)
      if (x == null || isNaN(x)) return
      const ctx = chart.ctx
      ctx.save()
      ctx.strokeStyle = "rgba(88,166,255,0.6)"
      ctx.lineWidth = 1
      ctx.setLineDash([4, 3])
      ctx.beginPath(); ctx.moveTo(px + 0.5, area.top); ctx.lineTo(px + 0.5, area.bottom); ctx.stroke()
      ctx.setLineDash([])
      const labels = chart.data.labels || []
      chart.data.datasets.forEach((ds, di) => {
        if (!chart.isDatasetVisible(di)) return
        const y = yAt(labels, ds.data, x)
        if (y == null || isNaN(y)) return
        const sc = chart.scales[ds.yAxisID || "y"]
        if (!sc) return
        const py = sc.getPixelForValue(y)
        if (py < area.top - 4 || py > area.bottom + 4) return
        ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2)
        ctx.fillStyle = ds.borderColor; ctx.fill()
        ctx.lineWidth = 1.5; ctx.strokeStyle = "rgba(13,17,23,0.9)"; ctx.stroke()
      })
      ctx.restore()
      // 数字条联动：每次重绘按当前比例尺换算，实时模式下数据刷新游标值也跟着变
      if (labels.length) chartHover(x)
    },
  }
  let raf = 0
  const redraw = () => { raf = 0; if (chart) chart.draw() }
  const requestRedraw = () => { if (!raf) raf = requestAnimationFrame(redraw) }
  const cardEl = cv.value.closest(".card")
  cardEl.addEventListener("mousemove", (ev) => {
    if (!chart) return
    chart._cursorPx = ev.clientX - cv.value.getBoundingClientRect().left
    requestRedraw()
  })
  cardEl.addEventListener("mouseleave", () => {
    if (chart) chart._cursorPx = null
    chartLeave()
    requestRedraw()
  })
  chart = new Chart(cv.value, { type: "line", data: { datasets: makeDatasets() }, options: o, plugins: [cursorPlugin] })
  chart._auto = props.def.auto || { mode: "pad" }
  if (props.def.auto1) chart._auto1 = props.def.auto1
  if (props.def.smooth != null) chart._smooth = props.def.smooth
  if (props.def.key === "kerskj") {
    chart.options.plugins.tooltip.callbacks.label = (ctx) => {
      const v = ctx.parsed.y
      if (v == null || isNaN(v)) return ""
      return (v >= 0 ? "放电 " : "充电 ") + Math.abs(v).toFixed(1) + " kW"
    }
  }
  regChart(props.def.key, chart)
})

onUnmounted(() => { unregChart(props.def.key); chartLeave(); if (chart) chart.destroy() })

function toggleZoom(ev) {
  // 点击图例（canvas 内 legend 区域）切换曲线显隐时，不应触发放大
  if (ev && ev.target === cv.value && chart && chart.chartArea) {
    const r = cv.value.getBoundingClientRect()
    if (ev.clientY - r.top < chart.chartArea.top) return
  }
  zoomed.value = !zoomed.value
  // 等 DOM 高度切换完再让 Chart.js 按新容器尺寸重算（否则放大后图表还是 220px 贴顶）
  nextTick(() => {
    if (chart) chart.resize()
    if (chart && chart.options.plugins && chart.options.plugins.zoom) {
      const z = chart.options.plugins.zoom
      z.zoom.wheel.enabled = zoomed.value
      z.zoom.pinch.enabled = zoomed.value
      z.pan.enabled = zoomed.value
      if (zoomed.value) {
        const sx = chart.scales.x
        if (sx && sx.max != null && sx.min != null && sx.max > sx.min) {
          z.limits = { x: { min: sx.min, max: sx.max } }
        }
        if (chart.resetZoom) chart.resetZoom()
      } else {
        delete z.limits
        if (chart.resetZoom) chart.resetZoom()
      }
      chart.update("none")
    }
  })
}
</script>

<style scoped>
.chart-wrap { height: 220px; position: relative; }
.chart-wrap canvas { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
/* 放大态：图表容器占满屏幕高度，图表居中不贴顶 */
.card.zoom .chart-wrap { height: calc(100vh - 130px); }
.card.zoom .chart-wrap canvas { width: 100% !important; height: 100% !important; }
</style>
