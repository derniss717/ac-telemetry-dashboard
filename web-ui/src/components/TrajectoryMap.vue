<template>
  <div class="card traj-card">
    <h3>赛道轨迹 <span class="traj-sub">{{ subtitle }}</span></h3>
    <div class="traj-box">
      <svg v-if="ok" :viewBox="vb" preserveAspectRatio="xMidYMid meet" class="traj-svg">
        <image v-if="img" :x="img.x" :y="img.y" :width="img.w" :height="img.h"
               :href="img.src" opacity="0.9" preserveAspectRatio="none"/>
        <path v-if="trackBase.d" :d="trackBase.d" stroke="#30363d" stroke-width="10" fill="none"
              stroke-linecap="round" opacity="0.35"/>
        <g v-for="(l, i) in layers" :key="i">
          <path v-for="(seg, j) in l.segs" :key="j" :d="seg.d" :stroke="seg.color"
                stroke-width="2.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        </g>
        <text v-for="(t, i) in turnLabels" :key="i" :x="t.x" :y="t.y" class="traj-turn">{{ t.label }}</text>
        <circle v-if="livePt" :cx="livePt[0]" :cy="livePt[1]" r="5" fill="#58a6ff">
          <animate attributeName="r" values="5;9;5" dur="1.2s" repeatCount="indefinite"/>
        </circle>
        <line v-if="livePt && liveDir" :x1="livePt[0]" :y1="livePt[1]"
              :x2="livePt[0] + liveDir[0] * 14" :y2="livePt[1] + liveDir[1] * 14"
              stroke="#58a6ff" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <div v-else class="traj-empty">无位置数据 — 跑圈后回放/对比可查看走线</div>
    </div>
    <div v-if="ok" class="traj-legend">
      <span v-for="(l, i) in layers" :key="i" class="legend-item">
        <i :style="{ background: l.color }"></i>{{ l.name }}
      </span>
      <span class="legend-item"><i style="background:#58a6ff"></i>实时位置</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'

const props = defineProps({
  laps: { type: Array, default: () => [] },   // [{ id, lap_no, total_ms, frames:[{pos:[x,y,z], speed, dist}] }]
  livePts: { type: Array, default: () => [] }, // 实时 pos 点 [[x,y,z],...]
  turns: { type: Array, default: () => [] },   // [{ n, name, km }]
  trackMap: { type: Object, default: null },   // { src, w, h } 赛道 outline 图
})

const W = 680, H = 380, PAD = 36
const vb = `${0} ${0} ${W} ${H}`

const ok = computed(() => {
  if (props.livePts.length >= 3) return true
  return props.laps.some(l => (l.frames || []).some(f => f && f.pos))
})

// 收集所有点 + 归一化映射（x/z → viewBox，保持纵横比）
// 优先用后端返回的 align（theta/flipX/flipZ）把世界坐标变换到赛道图像素系，
// 再 bbox 轴对齐到图（不变形）。没有 align 时退化为原始轴对齐映射。
const geom = computed(() => {
  const pts = []
  for (const l of props.laps) for (const f of l.frames || []) {
    if (f && f.pos && f.pos.length >= 2) pts.push({ p: f.pos, d: f.dist, v: f.speed, lap: l })
  }
  for (const p of props.livePts) if (p && p.length >= 2) pts.push({ p, d: null, v: null, lap: null })
  if (!pts.length) return null
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity
  for (const q of pts) {
    const [x, , z] = q.p
    if (x < x0) x0 = x; if (x > x1) x1 = x
    if (z < z0) z0 = z; if (z > z1) z1 = z
  }
  const spanX = Math.max(x1 - x0, 1e-6), spanZ = Math.max(z1 - z0, 1e-6)
  // 有赛道 outline 图：图 contain 放置 + 走线映射到图（放大 1.18 留赛道宽度余量）
  if (props.trackMap && props.trackMap.w && props.trackMap.h) {
    const imgW = props.trackMap.w, imgH = props.trackMap.h
    const sc = Math.min((W - PAD * 2) / imgW, (H - PAD * 2) / imgH)
    const iw = imgW * sc, ih = imgH * sc
    const ix = (W - iw) / 2, iy = (H - ih) / 2
    const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2
    const align = props.trackMap.align
    let map
    if (align) {
      // 旋转 + 镜像 到赛道图坐标系（旋转后 bbox 变化，重算 sx/sz）
      const th = (align.theta || 0) * Math.PI / 180
      const cs = Math.cos(th), sn = Math.sin(th)
      const fx = align.flipX ? -1 : 1, fz = align.flipZ ? -1 : 1
      // 先把所有点中心化+旋转+镜像
      const r = pts.map(q => {
        const dx = q.p[0] - cx, dz = q.p[2] - cz
        return [fx * (cs * dx - sn * dz), fz * (sn * dx + cs * dz)]
      })
      let rx0 = Infinity, rx1 = -Infinity, rz0 = Infinity, rz1 = -Infinity
      for (const [u, v] of r) {
        if (u < rx0) rx0 = u; if (u > rx1) rx1 = u
        if (v < rz0) rz0 = v; if (v > rz1) rz1 = v
      }
      const sx = Math.max(rx1 - rx0, 1e-6) * 1.18
      const sz = Math.max(rz1 - rz0, 1e-6) * 1.18
      // 统一缩放保持纵横比（min 取小者），与后端 trackalign._transform 的 pad=0.85 一致。
      // 切勿 X/Y 独立缩放（iw/sx 与 ih/sz）——赛道图与轨迹 bbox 宽高比不同时会拉伸变形，形状对不上。
      const s = Math.min(iw / sx, ih / sz)
      map = q => {
        const dx = q.p[0] - cx, dz = q.p[2] - cz
        return [ix + (fx * (cs * dx - sn * dz) - rx0) * s,
                iy + (fz * (sn * dx + cs * dz) - rz0) * s]
      }
    } else {
      const s = Math.min(iw / (spanX * 1.18), ih / (spanZ * 1.18))
      map = q => [ix + (q.p[0] - cx) * s + iw / 2, iy + (q.p[2] - cz) * s + ih / 2]
    }
    return { pts, map, img: { src: props.trackMap.src, x: ix, y: iy, w: iw, h: ih } }
  }
  const scale = Math.min((W - PAD * 2) / spanX, (H - PAD * 2) / spanZ)
  const ox = (W - spanX * scale) / 2, oz = (H - spanZ * scale) / 2
  const map = q => [ox + (q.p[0] - x0) * scale, oz + (q.p[2] - z0) * scale]
  return { pts, map }
})

const img = computed(() => (geom.value && geom.value.img) || null)

// 赛道轮廓：点最多的一圈
const trackBase = computed(() => {
  const g = geom.value
  if (!g) return { d: "" }
  let best = null, bestN = 0
  for (const l of props.laps) {
    const n = (l.frames || []).filter(f => f && f.pos).length
    if (n > bestN) { bestN = n; best = l }
  }
  if (!best) return { d: "" }
  return { d: linePath((best.frames || []).filter(f => f && f.pos), g.map) }
})

// 各圈走线层：单圈速度色阶，多圈每圈单色
const layers = computed(() => {
  const g = geom.value
  if (!g) return []
  const mkPath = (list, colorFn) => {
    const arr = list.filter(f => f && f.pos)
    if (arr.length < 3) return []
    const segs = []
    for (let i = 0; i + 1 < arr.length; i++) {
      const a = g.map({ p: arr[i].pos }), b = g.map({ p: arr[i + 1].pos })
      segs.push({ d: `M${a[0]} ${a[1]} L${b[0]} ${b[1]}`, color: colorFn(arr[i], arr[i + 1]) })
    }
    return segs
  }
  const single = props.laps.length <= 1
  const out = []
  if (single && props.laps.length === 1) {
    const fr = props.laps[0].frames || []
    const spd = fr.map(f => f.speed).filter(v => v != null && !isNaN(v))
    const lo = spd.length ? percentile(spd, 0.3) : 80
    const hi = spd.length ? percentile(spd, 0.85) : 250
    out.push({
      name: `圈 ${props.laps[0].lap_no ?? ""}`,
      color: "#3fb950",
      segs: mkPath(fr, (a, b) => speedColor((a.speed ?? 0 + b.speed ?? 0) / 2, lo, hi)),
    })
  } else if (props.laps.length > 1) {
    const colors = ["#f85149", "#58a6ff", "#e3b341", "#d2a8ff", "#3fb950"]
    props.laps.forEach((l, i) => {
      out.push({
        name: `圈 ${l.lap_no ?? ""} ${fmtT(l.total_ms)}`,
        color: colors[i % colors.length],
        segs: mkPath(l.frames || [], () => colors[i % colors.length]),
      })
    })
  }
  // 实时走线层（无回放/对比数据时画当前圈已跑线路）
  if (props.livePts.length >= 3) {
    const segs = []
    for (let i = 0; i + 1 < props.livePts.length; i++) {
      const a = g.map({ p: props.livePts[i] }), b = g.map({ p: props.livePts[i + 1] })
      segs.push({ d: `M${a[0]} ${a[1]} L${b[0]} ${b[1]}`, color: "#58a6ff" })
    }
    out.push({ name: "实时走线", color: "#58a6ff", segs })
  }
  return out
})

const livePt = computed(() => {
  const g = geom.value
  const last = props.livePts[props.livePts.length - 1]
  return g && last ? g.map({ p: last }) : null
})
const liveDir = computed(() => {
  const g = geom.value, n = props.livePts.length
  if (!g || n < 4) return null
  const a = g.map({ p: props.livePts[n - 4] }), b = g.map({ p: props.livePts[n - 1] })
  const dx = b[0] - a[0], dy = b[1] - a[1]
  const len = Math.hypot(dx, dy)
  return len > 0.001 ? [dx / len, dy / len] : null
})

// 弯角标注：按 dist 就近映射到轨迹点
const turnLabels = computed(() => {
  const g = geom.value
  if (!g || !props.turns.length) return []
  // 用第一圈（或实时）建立 dist → 位置 的查表
  const fr = (props.laps[0] && props.laps[0].frames) || []
  const withDist = fr.filter(f => f && f.pos && f.dist != null)
  if (!withDist.length) return []
  const out = []
  for (const t of props.turns) {
    let best = null, bestD = Infinity
    for (const f of withDist) {
      const dd = Math.abs((f.dist / 1000) - t.km)
      if (dd < bestD) { bestD = dd; best = f }
    }
    if (best && bestD < 0.25) {
      const [x, y] = g.map({ p: best.pos })
      out.push({ x: x + 8, y: y - 6, label: `T${t.n}${t.name ? " " + t.name : ""}` })
    }
  }
  return out
})

const subtitle = computed(() => {
  if (props.livePts.length >= 3) return "实时走线"
  if (props.laps.length > 1) return `${props.laps.length} 圈叠加`
  if (props.laps.length === 1) return `圈 ${props.laps[0].lap_no ?? ""}`
  return ""
})

function linePath(list, map) {
  const arr = list.filter(f => f && f.pos)
  if (arr.length < 2) return ""
  return arr.map((f, i) => {
    const [x, y] = map({ p: f.pos })
    return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1)
  }).join(" ")
}
function percentile(arr, q) {
  const s = [...arr].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor(s.length * q))]
}
function speedColor(v, lo, hi) {
  const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo || 1)))
  const hue = (1 - t) * 130
  return `hsl(${hue} 70% 55%)`
}
function fmtT(ms) {
  if (ms == null) return ""
  const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000), f = ms % 1000
  return `${m}:${String(s).padStart(2, "0")}.${String(f).padStart(3, "0")}`
}
</script>

<style scoped>
.traj-card h3 { font-size: 13px; color: var(--dim); margin-bottom: 8px; }
.traj-sub { color: var(--dim); font-size: 11px; margin-left: 8px; }
.traj-box {
  height: 340px; border: 1px solid var(--border); border-radius: var(--radius);
  background: rgba(13, 17, 23, 0.5); position: relative; overflow: hidden;
}
.traj-svg { width: 100%; height: 100%; }
.traj-empty {
  height: 100%; display: flex; align-items: center; justify-content: center;
  color: var(--dim); font-size: 13px;
}
.traj-turn { fill: #8b949e; font-size: 11px; }
.traj-legend { margin-top: 8px; display: flex; gap: 14px; flex-wrap: wrap; }
.legend-item { font-size: 11px; color: var(--dim); display: inline-flex; align-items: center; gap: 5px; }
.legend-item i { width: 14px; height: 4px; border-radius: 2px; display: inline-block; }
</style>
