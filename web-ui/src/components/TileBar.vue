<template>
  <section class="tiles" :class="{ hovering: !!hoverTiles }">
    <div class="tile big speed-gauge">
      <svg class="gauge" viewBox="0 0 120 120">
        <circle class="g-bg" cx="60" cy="60" r="52"></circle>
        <circle class="g-val" cx="60" cy="60" r="52"
                :stroke-dasharray="`${gaugeLen} ${CIRC}`" :stroke="gaugeColor"
                transform="rotate(-90 60 60)"></circle>
      </svg>
      <div class="gauge-value" id="t-speed">{{ tiles.speed }}</div>
      <div class="gauge-unit">{{ hoverTiles ? "悬停 km/h" : "km/h" }}</div>
    </div>
    <div class="tile"><div class="label">转速 rpm</div><div class="value" id="t-rpm">{{ tiles.rpm }}</div></div>
    <div class="tile"><div class="label">档位</div><div class="value" id="t-gear">{{ gearText(tiles.gear) }}</div></div>
    <div class="tile"><div class="label">当前圈</div><div class="value" id="t-curlap">{{ fmtMs(tiles.curlap) }}</div></div>
    <div class="tile"><div class="label">上一圈</div><div class="value" id="t-lastlap">{{ fmtMs(tiles.lastlap) }}</div></div>
    <div class="tile"><div class="label">最佳圈</div><div class="value" id="t-bestlap">{{ fmtMs(tiles.bestlap) }}</div></div>
    <div class="tile"><div class="label">扇区</div><div class="value">{{ tiles.sector }}</div></div>
    <div class="tile"><div class="label">方向盘°</div><div class="value" id="t-steer" :style="steerStyle">{{ steerText }}</div></div>
    <div class="tile"><div class="label">KERS 电量</div><div class="value" id="t-kers">{{ tiles.kers == null ? "--%" : tiles.kers + "%" }}</div></div>
    <div class="tile"><div class="label">轮胎配方</div><div class="value">{{ tiles.compound }}</div></div>
    <div class="tile"><div class="label">油量</div><div class="value" id="t-fuel">{{ fuelText }}</div></div>
    <div class="tile wide"><div class="label">世界坐标 (m)</div><div class="value mono" id="t-pos">{{ posText }}</div></div>
    <div class="tile wide"><div class="label">朝向</div><div class="value mono" id="t-orient">{{ yawText }}</div></div>
    <div class="tile wide"><div class="label">速度矢量 (m/s)</div><div class="value mono" id="t-vel">{{ velText }}</div></div>
    <div class="tile gap-tile"><div class="label">最后圈 vs 最快圈</div><div class="value">{{ gapText }}</div></div>
  </section>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { on as subscribe } from '../store/live'
import { fmtMs, gearText } from '../lib/fmt'

const realtime = ref({ speed: 0, rpm: 0, gear: null, curlap: null, lastlap: null,
  bestlap: null, sector: "-", steer_deg: null, kers: null, compound: "--", fuel: null,
  pos: null, orient: null, vel: null })
const hoverTiles = ref(null)   // 图表悬停时的帧数据（非空=正在悬停）
// 显示源：悬停优先（合并实时值，避免 hover 时上一圈/最佳圈/配方显示 "--"）
const HOVER_PASSTHROUGH = ["lastlap", "bestlap", "compound"]
const tiles = computed(() => {
  const h = hoverTiles.value
  if (!h) return realtime.value
  const out = { ...realtime.value, ...h }
  // hover 帧不含这些字段（frameToTiles 置空/--），保留实时值
  for (const k of HOVER_PASSTHROUGH) {
    if (h[k] == null || h[k] === "--") out[k] = realtime.value[k]
  }
  return out
})
let maxFuelSeen = 0
const gapText = ref("--:--.---")

// ---------------- UDP 扩展卡片（世界坐标/朝向/速度矢量） ----------------
const fmt3 = v => (v == null || isNaN(v)) ? "—" : v.toFixed(1)
const posText = computed(() => {
  const p = tiles.value.pos
  return p && p.length >= 3
    ? `x ${fmt3(p[0])}  y ${fmt3(p[1])}  z ${fmt3(p[2])}`
    : "—"
})
const yawText = computed(() => {
  const o = tiles.value.orient
  if (!o || o.length < 2) return "—"
  // orientation[0..2] 为前向向量（官方布局 forward 3 分量），换算航向角
  const yaw = Math.atan2(o[1], o[0]) * 180 / Math.PI
  return `${((yaw + 360) % 360).toFixed(1)}°`
})
const velText = computed(() => {
  const v = tiles.value.vel
  return v && v.length >= 3
    ? `x ${fmt3(v[0])}  y ${fmt3(v[1])}  z ${fmt3(v[2])}`
    : "—"
})

// ---------------- 速度圆环仪表（F1 风格） ----------------
// 车型极速上限：已知车型固定（F1 ~350），未知车型按数据动态自适应
const CIRC = 2 * Math.PI * 52   // r=52 周长
const SPEED_MAX = { "vrc_formula_alpha_2024_csp": 350 }
let speedMax = 300
const gaugeLen = computed(() => CIRC * Math.min(1, tiles.value.speed / speedMax))
const gaugeColor = computed(() => {
  const p = Math.min(1, tiles.value.speed / speedMax)
  if (p >= 0.9) return "#f85149"     // 接近极速：红
  if (p >= 0.7) return "#e3b341"     // 高速：黄
  return "#3fb950"                   // 常态：绿
})
watch(() => tiles.value.speed, (v) => {
  if (v > speedMax) speedMax = Math.ceil(v / 10) * 10   // 未知车型数据超限时上调
})
function onSession(s) {
  if (s.car && SPEED_MAX[s.car]) speedMax = SPEED_MAX[s.car]
}

// 数值变化闪烁（轻量反馈，替代数字滚动——车速变化太频繁不适合逐帧滚动）
const FLASH_KEYS = ["speed", "rpm", "gear", "curlap", "sector", "kers"]
watch(tiles, (n, o) => {
  if (!o) return
  for (const k of FLASH_KEYS) {
    if (n[k] !== o[k]) flashEl(`t-${k}`)
  }
})
function flashEl(id) {
  const el = document.getElementById(id)
  if (!el) return
  el.classList.remove("flash")
  void el.offsetWidth   // 重启动画
  el.classList.add("flash")
  setTimeout(() => el.classList.remove("flash"), 400)
}

const steerText = computed(() => tiles.value.steer_deg == null ? "-" : tiles.value.steer_deg.toFixed(1) + "°")
const steerStyle = computed(() => tiles.value.steer_deg == null ? {} : { color: tiles.value.steer_deg < 0 ? "var(--red)" : "var(--green)" })
const fuelText = computed(() => {
  const f = tiles.value.fuel
  if (f == null) return "--%"
  if (maxFuelSeen > 0 && f <= maxFuelSeen) return Math.round(Math.max(0, Math.min(100, f / maxFuelSeen * 100))) + "%"
  return f.toFixed(1) + "L"
})

function onTiles(t) {
  realtime.value = t
  if (t.fuel != null && !isNaN(t.fuel)) maxFuelSeen = Math.max(maxFuelSeen, t.fuel)
}
function onHover(t) { hoverTiles.value = t || null }
function onLaps({ laps }) {
  let best = Infinity
  for (const l of laps) if (l.is_valid && l.total_ms && l.total_ms < best) best = l.total_ms
  const valid = laps.filter(l => l.is_valid && l.total_ms)
  if (valid.length >= 1 && best !== Infinity) {
    const last = valid[valid.length - 1]
    const gap = last.total_ms - best
    const sign = gap > 0 ? "+" : (gap < 0 ? "−" : "±")
    gapText.value = `${sign}${fmtMs(Math.abs(gap))}  (末圈 ${fmtMs(last.total_ms)} / 最快 ${fmtMs(best)})`
  } else gapText.value = "--:--.---"
}

onMounted(() => { subscribe('tiles', onTiles); subscribe('laps', onLaps); subscribe('session', onSession); subscribe('hover', onHover) })
</script>
