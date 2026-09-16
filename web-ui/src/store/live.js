// 核心数据层：帧仓库 + 轮询 + 回放 + 对比 + 统计（从 v1.2 app.js 迁移）
// 模块级状态（非 Vue reactive——高频轮询下性能优先），UI 通过 pub/sub 订阅更新
import { CHART_COLORS, ERS_BATTERY_KJ, WHEELS } from '../lib/chartDefs'
import { fmtMs } from '../lib/fmt'

export const POLL_MS = 200
const LAPS_POLL_MS = 2000
const SESSION_POLL_MS = 5000
const MAX_PTS = 4000
const DISPLAY_PTS = 400
const PRESSURE_RANGES = { "vrc_formula_alpha_2024_csp": [15, 28] }

// ---------------- 事件 pub/sub ----------------
const listeners = {}
export function on(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn) }
function emit(ev, data) { for (const fn of (listeners[ev] || [])) fn(data) }

// ---------------- 状态 ----------------
let afterSeq = 0
let curLapNo = -1
let lastDataT = 0
let maxFuelSeen = 0
let lapDistBase = 0
let currentCar = null
let liveTrackPts = []        // 实时轨迹：当前圈位置点缓冲
let lastTrackEmit = 0
export let replayMode = false
let replayLapNo = null
let lastKers = null, lastKersT = null
const lapCache = new Map()
const LAP_CACHE_MAX = 3
export const extOk = { tyreO: false, brakeT: false, wear: false }

// ---------------- 帧仓库 ----------------
// 裁剪策略：允许超出上限 400 帧再一次性批量删——200Hz 数据流下每帧 splice(0,1)
// 是 O(n)（17 个仓库 × 4000 长度 ≈ 每秒千万次元素搬移），批量摊还后接近 O(1)
const TRIM_SLACK = 400
function makeRepo() { return { x: [], series: {} } }
const repos = {
  speed: makeRepo(), rpm: makeRepo(), gear: makeRepo(), pedals: makeRepo(),
  susp: makeRepo(), ride: makeRepo(), kers: makeRepo(), kerskj: makeRepo(),
  accg: makeRepo(), tyre: makeRepo(), pressure: makeRepo(), turbo: makeRepo(),
  tyreO: makeRepo(), slip: makeRepo(), brakeT: makeRepo(), wear: makeRepo(),
  steer: makeRepo(),
}

function pushFrame(repo, x, vals) {
  repo.x.push(x)
  for (const k of Object.keys(vals)) (repo.series[k] = repo.series[k] || []).push(vals[k])
  if (!replayMode && repo.x.length > MAX_PTS + TRIM_SLACK) {
    const drop = repo.x.length - MAX_PTS
    repo.x.splice(0, drop)
    for (const k of Object.keys(repo.series)) repo.series[k].splice(0, drop)
  }
}

function resetRepos() {
  for (const k of Object.keys(repos)) {
    repos[k].x = []
    repos[k].series = {}
  }
  hoverFrames.length = 0   // 换圈/回放切换坐标系，旧帧缓存作废
}

function ingestFrame(fr) {
  if (fr.lap_no !== curLapNo && !replayMode) {
    curLapNo = fr.lap_no
    resetRepos()
    if (fr.dist != null) lapDistBase = fr.dist
  }
  // 实时轨迹收集（轨迹图功能，UI 已摘除——每帧 push 纯开销，暂注释；恢复轨迹图时打开）
  // if (fr.pos && !replayMode) {
  //   liveTrackPts.push(fr.pos)
  // }
  // 调试钩子（轨迹图排查用，UI 摘除后停用）
  // if (typeof window !== "undefined") {
  //   window.__trajDbg = {
  //     pts: liveTrackPts.length,
  //     lastEmit: lastTrackEmit,
  //     replay: replayMode,
  //     lastFrPos: fr.pos ? fr.pos[0] : null,
  //   }
  // }
  const x = (fr.dist != null && lapDistBase != null) ? (fr.dist - lapDistBase) : fr.lap_ms
  pushHoverFrame(fr)   // 图表 hover 联动：缓存最近帧（按 x 索引）
  pushFrame(repos.speed, x, { speed: fr.speed, drs: fr.drs > 0.5 ? 1 : 0 })
  pushFrame(repos.rpm, x, { rpm: fr.rpms })
  pushFrame(repos.gear, x, { gear: fr.gear - 1 })   // AC 偏移编码：2=1档→1，9=8档→8
  pushFrame(repos.pedals, x, { gas: fr.gas * 100, brake: fr.brake * 100 })
  pushFrame(repos.susp, x, { FL: fr.susp[0] * 1000, FR: fr.susp[1] * 1000, RL: fr.susp[2] * 1000, RR: fr.susp[3] * 1000 })
  pushFrame(repos.ride, x, { F: fr.ride[0] * 1000, R: fr.ride[1] * 1000 })
  if (fr.fuel != null && !isNaN(fr.fuel)) maxFuelSeen = Math.max(maxFuelSeen, fr.fuel)
  const fuelPct = maxFuelSeen > 0 ? Math.max(0, Math.min(100, fr.fuel / maxFuelSeen * 100)) : 0
  pushFrame(repos.kers, x, { kers: fr.kers * 100, fuel: fuelPct })
  let ersPwr = null
  if (fr.kers != null && fr.t != null && lastKers != null && lastKersT != null && fr.t > lastKersT) {
    ersPwr = -(fr.kers - lastKers) / (fr.t - lastKersT) * ERS_BATTERY_KJ
  }
  if (fr.kers != null) { lastKers = fr.kers; lastKersT = fr.t }
  pushFrame(repos.kerskj, x, { kj: fr.kers, pwr: ersPwr })
  pushFrame(repos.accg, x, { X: fr.accG[0], Y: fr.accG[1] })
  pushFrame(repos.tyre, x, { FL: fr.tyreT[0], FR: fr.tyreT[1], RL: fr.tyreT[2], RR: fr.tyreT[3] })
  if (fr.pressure) pushFrame(repos.pressure, x, { FL: fr.pressure[0], FR: fr.pressure[1], RL: fr.pressure[2], RR: fr.pressure[3] })
  pushFrame(repos.turbo, x, { turbo: fr.turbo })
  if (fr.tyreO) pushFrame(repos.tyreO, x, { FL: fr.tyreO[0], FR: fr.tyreO[1], RL: fr.tyreO[2], RR: fr.tyreO[3] })
  if (fr.slip) pushFrame(repos.slip, x, { FL: fr.slip[0] * 100, FR: fr.slip[1] * 100, RL: fr.slip[2] * 100, RR: fr.slip[3] * 100 })
  if (fr.brakeT) pushFrame(repos.brakeT, x, { FL: fr.brakeT[0], FR: fr.brakeT[1], RL: fr.brakeT[2], RR: fr.brakeT[3] })
  if (fr.wear) pushFrame(repos.wear, x, { FL: fr.wear[0], FR: fr.wear[1], RL: fr.wear[2], RR: fr.wear[3] })
  checkExtData(fr)
  if (fr.steer != null) pushFrame(repos.steer, x, { steer: fr.steer })
}

// ---------------- 扩展数据自检测 ----------------
function _inRange(v, lo, hi) { return v != null && !isNaN(v) && v >= lo && v <= hi }
function _all4Ok(arr, lo, hi) { return Array.isArray(arr) && arr.length >= 4 && arr.slice(0, 4).every(v => _inRange(v, lo, hi)) }
function checkExtData(fr) {
  if (!extOk.tyreO && _all4Ok(fr.tyreO, 20, 200)) extOk.tyreO = true
  if (!extOk.brakeT && _all4Ok(fr.brakeT, 50, 900)) extOk.brakeT = true
  if (!extOk.wear && _all4Ok(fr.wear, 0, 100)) extOk.wear = true
  emit('ext', { ...extOk })
}

// ---------------- 渲染工具 ----------------
function downsample(arr, maxPts) {
  if (!arr || arr.length <= maxPts) return arr
  const step = Math.ceil(arr.length / maxPts)
  const out = []
  for (let i = 0; i < arr.length; i += step) out.push(arr[i])
  return out
}

function smooth(arr, w) {
  if (!arr || arr.length <= w) return arr
  const out = new Array(arr.length)
  const half = Math.floor(w / 2)
  for (let i = 0; i < arr.length; i++) {
    let s = 0, c = 0
    for (let j = Math.max(0, i - half); j <= Math.min(arr.length - 1, i + half); j++) {
      const v = arr[j]
      if (v == null || isNaN(v)) continue
      s += v; c++
    }
    out[i] = c ? s / c : arr[i]
  }
  return out
}

// ---------------- Chart 注册表（由 ChartCard 组件注册） ----------------
const chartRegistry = new Map()
export function regChart(key, chart) { chartRegistry.set(key, chart) }
export function unregChart(key) { chartRegistry.delete(key) }
export function getChart(key) { return chartRegistry.get(key) }
export function allCharts() { return [...chartRegistry.values()] }

function applyAxisRange(ch, axisId, auto) {
  const all = []
  for (const ds of ch.data.datasets) {
    if (ds.yAxisID && ds.yAxisID !== axisId) continue
    if (!ds.yAxisID && axisId !== "y") continue
    for (const v of ds.data) if (v != null && !isNaN(v)) all.push(v)
  }
  const y = ch.options.scales[axisId]
  if (auto.mode === "fixed") { y.min = auto.min; y.max = auto.max; return }
  if (!all.length) return
  let lo = Math.min(...all), hi = Math.max(...all)
  const span = (hi - lo) || 1
  const pad = (auto.padAbs != null ? auto.padAbs : span * 0.08)
  if (auto.mode === "sym") {
    const m = Math.max(Math.abs(lo), Math.abs(hi))
    y.min = -m - pad; y.max = m + pad
  } else {
    y.min = lo - pad; y.max = hi + pad
    if (auto.loCap != null) y.min = Math.max(y.min, auto.loCap)
    if (auto.hiCap != null) y.max = Math.min(y.max, auto.hiCap)
    if (auto.zero) y.min = Math.min(0, lo)
  }
}

export function applyAutoRange(ch) {
  applyAxisRange(ch, "y", ch._auto || { mode: "pad" })
  if (ch._auto1 && ch.options.scales.y1) applyAxisRange(ch, "y1", ch._auto1)
}

export function setPressureRange(car) {
  const r = car && PRESSURE_RANGES[car]
  const ch = getChart("pressure")
  if (ch) ch._auto = r ? { mode: "fixed", min: r[0], max: r[1] } : { mode: "pad", loCap: 0 }
  if (car) currentCar = car
}

export function refreshChart(name) {
  const repo = repos[name]
  const ch = getChart(name)
  if (!repo || !ch) return
  const dsMap = {}
  for (const ds of ch.data.datasets) dsMap[ds._key] = ds
  ch.data.labels = downsample(repo.x, DISPLAY_PTS)
  const w = ch._smooth || 3
  for (const k of Object.keys(repo.series)) {
    if (dsMap[k]) dsMap[k].data = smooth(downsample(repo.series[k], DISPLAY_PTS), w)
  }
  // DRS 染红走 series.drs（pushFrame 只写 series，drsArr 从未被填充——v2.0 迁移死代码 bug）
  const drs = repo.series.drs
  if (name === "speed" && dsMap.speed && drs && drs.length) {
    const drsDs = downsample(drs, DISPLAY_PTS)
    dsMap.speed._drs = drsDs
    dsMap.speed.segment = {
      borderColor: ctx => {
        const i = ctx.p0DataIndex != null ? ctx.p0DataIndex : ctx.p0.dataIndex
        return (drsDs[i] || 0) > 0.5 ? "#f85149" : CHART_COLORS.speed
      },
    }
  } else if (name === "speed" && dsMap.speed) {
    dsMap.speed.segment = undefined
  }
  applyAutoRange(ch)
  ch.update("none")
}

export function refreshAllCharts() {
  for (const k of Object.keys(repos)) refreshChart(k)
}

// ---------------- 顶部数值卡 ----------------
function updateTiles(cur) {
  emit('tiles', {
    speed: Math.round(cur.speed_kmh || 0),
    rpm: Math.round(cur.rpms || 0),
    gear: cur.gear,
    curlap: cur.lap_ms,
    lastlap: cur.last_lap_ms,
    bestlap: cur.best_lap_ms,
    sector: cur.sector == null ? "-" : (cur.sector + 1),
    steer_deg: cur.steer_deg,
    kers: cur.kers_charge == null ? null : Math.round(cur.kers_charge * 100),
    compound: cur.tyre_compound ? cur.tyre_compound : "--",
    fuel: cur.fuel,
    live: cur.live,
    in_pit: cur.in_pit,
    // UDP 扩展数据（世界坐标/朝向/速度矢量），共享内存模式由混合监听提供
    pos: cur.pos || null,
    orient: cur.orient || null,
    vel: cur.vel || null,
  })
}

// ---------------- 图表 hover 联动（鼠标在图表上移动 → 顶部数字条显示该位置数据） ----------------
// 帧缓存：按 x（dist 或 lap_ms，与图表 x 轴同坐标系）索引，供 hover 查询最近一帧
const hoverFrames = []
const HOVER_CACHE_MAX = MAX_PTS   // 与图表展示范围对齐（4000），否则鼠标查左半段查不到帧
function frameX(fr) {
  return (fr.dist != null && lapDistBase != null) ? (fr.dist - lapDistBase) : fr.lap_ms
}
function pushHoverFrame(fr) {
  const x = frameX(fr)
  if (x == null || isNaN(x)) return
  hoverFrames.push({ x, fr })
  if (hoverFrames.length > HOVER_CACHE_MAX + TRIM_SLACK) {
    hoverFrames.splice(0, hoverFrames.length - HOVER_CACHE_MAX)   // 批量裁剪（shift 每帧 O(n)）
  }
}
function findFrameByX(x) {
  const n = hoverFrames.length
  if (!n || x == null || isNaN(x)) return null
  if (x <= hoverFrames[0].x) return hoverFrames[0].fr
  if (x >= hoverFrames[n - 1].x) return hoverFrames[n - 1].fr
  let lo = 0, hi = n - 1
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1
    if (hoverFrames[mid].x < x) lo = mid; else hi = mid
  }
  return (x - hoverFrames[lo].x) <= (hoverFrames[hi].x - x) ? hoverFrames[lo].fr : hoverFrames[hi].fr
}
function frameToTiles(fr) {
  return {
    speed: Math.round(fr.speed || 0),
    rpm: Math.round(fr.rpms || 0),
    gear: fr.gear,
    curlap: fr.lap_ms,
    lastlap: null,
    bestlap: null,
    sector: fr.sector == null ? "-" : (fr.sector + 1),
    steer_deg: fr.steer,
    kers: fr.kers == null ? null : Math.round(fr.kers * 100),
    compound: "--",
    fuel: fr.fuel,
    live: true,
    in_pit: false,
    pos: fr.pos || null,
    orient: fr.orient || null,
    vel: fr.vel || null,
  }
}
export function chartHover(x) {
  const fr = findFrameByX(x)
  if (fr) emit('hover', frameToTiles(fr))
}
export function chartLeave() {
  emit('hover', null)
}

// ---------------- 圈列表统计（返回 HTML 片段） ----------------
export function renderLapsStatsHtml(laps) {
  if (!laps || laps.length === 0) return ""
  const valid = laps.filter(l => l.is_valid && !l.outlap && !l.is_inlap
    && l.total_ms > 0 && l.s1_ms > 0 && l.s2_ms > 0 && l.s3_ms > 0)
  if (!valid.length) return '<span class="st dim">暂无有效圈数据</span>'
  const best = valid.reduce((a, b) => b.total_ms < a.total_ms ? b : a)
  const avg = valid.reduce((s, l) => s + l.total_ms, 0) / valid.length
  const slow = valid.reduce((a, b) => b.total_ms > a.total_ms ? b : a)
  const span = slow.total_ms - best.total_ms
  const segOk = valid.filter(l => Math.abs(l.s1_ms + l.s2_ms + l.s3_ms - l.total_ms) <= 5000)
  const parts = [`<span class="st">最快 <b>${fmtMs(best.total_ms)}</b> <i>圈${best.lap_no}</i></span>`]
  if (segOk.length) {
    const bS1 = segOk.reduce((a, b) => b.s1_ms < a.s1_ms ? b : a)
    const bS2 = segOk.reduce((a, b) => b.s2_ms < a.s2_ms ? b : a)
    const bS3 = segOk.reduce((a, b) => b.s3_ms < a.s3_ms ? b : a)
    const ideal = bS1.s1_ms + bS2.s2_ms + bS3.s3_ms
    const gap = best.total_ms - ideal
    parts.push(
      `<span class="st">理论最快 <b>${fmtMs(ideal)}</b> <i>(S1 圈${bS1.lap_no} + S2 圈${bS2.lap_no} + S3 圈${bS3.lap_no})</i></span>`,
      gap > 0 ? `<span class="st">提升空间 <b class="up">${fmtMs(gap)}</b></span>`
              : `<span class="st">已达成 <b class="dn">理论值</b></span>`,
    )
  } else {
    parts.push('<span class="st">理论最快 <b class="dim">—</b> <i>（此赛道扇区线未覆盖全圈，无法计算）</i></span>')
  }
  parts.push(
    `<span class="st sep">│</span>`,
    `<span class="st">有效 <b>${valid.length}</b>/${laps.length} 圈</span>`,
    `<span class="st">平均 <b>${fmtMs(avg)}</b></span>`,
    `<span class="st">最快↔最慢 <b>${fmtMs(span)}</b></span>`,
  )
  return parts.join("")
}

// ---------------- 轮询 ----------------
// ---------------- 轮询 / WebSocket 实时 ----------------
const WS_PORT = 8081
let ws = null
let liveTimer = null
let wsRetryTimer = null

// WS 消息与 HTTP 轮询共用同一套数据处理
function handleLiveData(data) {
  lastDataT = Date.now()
  emit('banner', false)
  if (replayMode) return
  updateTiles(data.current)
  if (data.full || !afterSeq) {
    resetRepos()
    curLapNo = -1
  }
  for (const fr of data.frames) ingestFrame(fr)
  afterSeq = data.latest_seq || afterSeq
  refreshAllCharts()
  if (data.full) afterSeq = data.latest_seq
}

async function pollLive() {
  try {
    const r = await fetch(`/api/live?after=${afterSeq}`)
    handleLiveData(await r.json())
  } catch (e) {
    console.error("pollLive:", e)
    if (Date.now() - lastDataT > 5000) emit('banner', true)
  }
}

function startPollLive() { if (!liveTimer) liveTimer = setInterval(pollLive, POLL_MS) }
function stopPollLive() { if (liveTimer) { clearInterval(liveTimer); liveTimer = null } }

function wsConnect() {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws"
    ws = new WebSocket(`${proto}://${location.hostname}:${WS_PORT}`)
    ws.onopen = () => { stopPollLive() }
    ws.onmessage = (ev) => { try { handleLiveData(JSON.parse(ev.data)) } catch (e) { /* ignore */ } }
    ws.onclose = () => {
      ws = null
      startPollLive()   // 断开回退 HTTP 轮询
      clearTimeout(wsRetryTimer)
      wsRetryTimer = setTimeout(wsConnect, 3000)
    }
    ws.onerror = () => { try { ws.close() } catch (e) { /* ignore */ } }
  } catch (e) {
    startPollLive()
  }
}

async function pollLaps() {
  try {
    const r = await fetch("/api/laps")
    const data = await r.json()
    emit('laps', { laps: data.laps || [], sessionId: data.session_id })
  } catch (e) { /* ignore */ }
}

async function pollSession() {
  try {
    const r = await fetch("/api/session")
    const data = await r.json()
    setPressureRange(data.car)
    emit('session', { car: data.car || "—", track: data.track || "—", lapsDone: data.laps_done })
  } catch (e) { /* ignore */ }
}

export function start() {
  setInterval(pollLaps, LAPS_POLL_MS)
  setInterval(pollSession, SESSION_POLL_MS)
  pollLaps(); pollSession()
  // 实时数据：优先 WebSocket（丝滑），连上前先跑 HTTP 轮询兜底，连上后自动切换
  startPollLive()
  wsConnect()
  // 轨迹图定时推送（UI 已摘除，停用；恢复轨迹图时打开 startTrackFlush()）
  // startTrackFlush()
}

// 轨迹图实时推送：每 400ms 把累积的位置点发给 TrajectoryMap（最多 600 点）
let trackTimer = null
function startTrackFlush() {
  if (trackTimer) return
  trackTimer = setInterval(() => {
    if (liveTrackPts.length >= 2) emit('track-live', liveTrackPts.slice(-600))
  }, 400)
}

// ---------------- 圈回放 ----------------
export async function loadLap(lap) {
  try {
    let data = lapCache.get(lap.id)
    if (data) {
      lapCache.delete(lap.id); lapCache.set(lap.id, data)
    } else {
      for (let attempt = 0; attempt < 4; attempt++) {
        const r = await fetch(`/api/lap/${lap.id}`)
        data = await r.json()
        if (data.frames && data.frames.length > 0) break
        if (attempt < 3) await new Promise(res => setTimeout(res, 1000))
      }
      if (data.frames && data.frames.length > 0) {
        lapCache.set(lap.id, data)
        if (lapCache.size > LAP_CACHE_MAX) lapCache.delete(lapCache.keys().next().value)
      }
    }
    if (!data.frames || data.frames.length === 0) {
      alert("该圈还没有数据（帧可能还在写入，稍后再试）")
      return
    }
    replayMode = true
    replayLapNo = lap.lap_no
    resetRepos()
    curLapNo = lap.lap_no
    if (data.frames[0].dist != null) lapDistBase = data.frames[0].dist
    for (const fr of data.frames) ingestFrame(fr)
    let xmin = Infinity, xmax = -Infinity
    for (const fr of data.frames) {
      const x = (fr.dist != null && lapDistBase != null) ? (fr.dist - lapDistBase) : fr.lap_ms
      if (x != null && !isNaN(x)) { if (x < xmin) xmin = x; if (x > xmax) xmax = x }
    }
    if (xmin === Infinity) { xmin = 0; xmax = 0 }
    setPressureRange(lap.car || currentCar)
    for (const ch of allCharts()) {
      ch.options.scales.x.min = xmin
      ch.options.scales.x.max = xmax
    }
    refreshAllCharts()
    const last = data.frames[data.frames.length - 1]
    updateTiles({
      live: false, in_pit: false,
      lap_no: lap.lap_no, lap_ms: last.lap_ms, sector: last.sector,
      last_lap_ms: lap.total_ms, best_lap_ms: null, completed_laps: lap.lap_no,
      speed_kmh: last.speed, gear: last.gear, rpms: last.rpms,
      kers_charge: last.kers, fuel: last.fuel, steer_deg: last.steer,
    })
    emit('replay', { text: `· 回放中：圈 ${lap.lap_no}（${fmtMs(lap.total_ms)}）— 点击这里回到实时`, on: true })
    // 轨迹图：回放单圈走线
    emit('track', { laps: [{ id: lap.id, lap_no: lap.lap_no, total_ms: lap.total_ms, frames: data.frames }] })
  } catch (e) {
    alert("回放加载失败: " + e)
  }
}

export function backToLive() {
  replayMode = false
  replayLapNo = null
  resetRepos()
  curLapNo = -1
  afterSeq = 0
  lapDistBase = 0
  for (const ch of allCharts()) {
    delete ch.options.scales.x.min
    delete ch.options.scales.x.max
  }
  emit('replay', { text: "", on: false })
  setPressureRange(currentCar)
  refreshChart("pressure")
}

// ---------------- 两圈对比 ----------------
export async function compareLaps(sel) {
  if (!sel || sel.length !== 2) return
  const idA = sel[0].id, idB = sel[1].id
  const tagA = sel[0].no, tagB = sel[1].no
  const [d1, d2] = await Promise.all([
    fetch(`/api/lap/${idA}`).then(r => r.json()),
    fetch(`/api/lap/${idB}`).then(r => r.json()),
  ])
  const f1 = d1.frames || [], f2 = d2.frames || []
  if (f1.length < 10 || f2.length < 10) { alert("所选圈数据不足，无法对比"); return }
  const t1 = (d1.lap.total_ms || 0), t2 = (d2.lap.total_ms || 0)
  const fast = t1 <= t2 ? { lap: d1.lap, fr: f1, tag: tagA } : { lap: d2.lap, fr: f2, tag: tagB }
  const slow = t1 <= t2 ? { lap: d2.lap, fr: f2, tag: tagB } : { lap: d1.lap, fr: f1, tag: tagA }
  const pts = alignFrames(fast.fr, slow.fr, fast.lap.total_ms / 1000, slow.lap.total_ms / 1000)
  if (!pts || pts.length < 10) { alert("两圈距离数据无法对齐（需有距离字段）"); return }
  emit('cmp', { pts, fast, slow })
  // 轨迹图：两圈走线叠加
  emit('track', {
    laps: [
      { id: fast.lap.id, lap_no: fast.lap.lap_no, total_ms: fast.lap.total_ms, frames: fast.fr },
      { id: slow.lap.id, lap_no: slow.lap.lap_no, total_ms: slow.lap.total_ms, frames: slow.fr },
    ],
  })
}

export function alignFrames(fastFr, slowFr, fastTotal, slowTotal, stepM = 10) {
  const prep = (frames) => {
    const valid = frames.filter(f => f.dist > 0 && f.lap_ms != null && f.speed != null)
    if (valid.length < 2) return null
    const base = valid[0].dist
    let prevKers = null, prevT = null
    return valid.map(f => {
      let ers = null
      if (f.kers != null && prevKers != null && prevT != null && f.t > prevT) {
        ers = -(f.kers - prevKers) / (f.t - prevT) * ERS_BATTERY_KJ
      }
      if (f.kers != null) { prevKers = f.kers; prevT = f.t }
      const ag = f.accG || null
      return { d: (f.dist - base) / 1000, t: f.lap_ms / 1000, v: f.speed,
               gas: f.gas, brake: f.brake, turbo: f.turbo, ers,
               accX: ag ? ag[0] : null, accY: ag ? ag[1] : null, steer: f.steer }
    })
  }
  const A = prep(fastFr), B = prep(slowFr)
  if (!A || !B) return null
  const maxD = Math.max(A[A.length - 1].d, B[B.length - 1].d)
  if (maxD < 0.5) return null
  const step = stepM / 1000
  const interp = (arr, d, key, total) => {
    let lo = 0, hi = arr.length - 1
    if (d <= arr[0].d) return arr[0][key]
    if (d >= arr[hi].d) {
      if (key === "t" && total != null) {
        const last = arr[hi]
        const span = maxD - last.d
        if (span > 0.001) return last.t + (total - last.t) * (d - last.d) / span
      }
      return arr[hi][key]
    }
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1
      if (arr[mid].d < d) lo = mid; else hi = mid
    }
    const a = arr[lo], b = arr[hi]
    const f = (b.d - a.d) ? (d - a.d) / (b.d - a.d) : 0
    return a[key] + (b[key] - a[key]) * f
  }
  const pts = []
  for (let d = 0; d <= maxD; d += step) {
    pts.push({ d, tA: interp(A, d, "t", fastTotal), tB: interp(B, d, "t", slowTotal),
               vA: interp(A, d, "v", null), vB: interp(B, d, "v", null),
               gA: interp(A, d, "gas", null), gB: interp(B, d, "gas", null),
               bA: interp(A, d, "brake", null), bB: interp(B, d, "brake", null),
               tuA: interp(A, d, "turbo", null), tuB: interp(B, d, "turbo", null),
               erA: interp(A, d, "ers", null), erB: interp(B, d, "ers", null),
               axA: interp(A, d, "accX", null), axB: interp(B, d, "accX", null),
               ayA: interp(A, d, "accY", null), ayB: interp(B, d, "accY", null),
               stA: interp(A, d, "steer", null), stB: interp(B, d, "steer", null) })
  }
  if (pts.length === 0 || pts[pts.length - 1].d < maxD - step / 2) {
    pts.push({ d: maxD, tA: interp(A, maxD, "t", fastTotal), tB: interp(B, maxD, "t", slowTotal),
               vA: interp(A, maxD, "v", null), vB: interp(B, maxD, "v", null),
               gA: interp(A, maxD, "gas", null), gB: interp(B, maxD, "gas", null),
               bA: interp(A, maxD, "brake", null), bB: interp(B, maxD, "brake", null),
               tuA: interp(A, maxD, "turbo", null), tuB: interp(B, maxD, "turbo", null),
               erA: interp(A, maxD, "ers", null), erB: interp(B, maxD, "ers", null),
               axA: interp(A, maxD, "accX", null), axB: interp(B, maxD, "accX", null),
               ayA: interp(A, maxD, "accY", null), ayB: interp(B, maxD, "accY", null),
               stA: interp(A, maxD, "steer", null), stB: interp(B, maxD, "steer", null) })
  }
  return pts
}

// ---------------- 下载 / 清理 ----------------
export async function downloadLap(lapId) {
  try {
    let r = null
    for (let attempt = 0; attempt < 4; attempt++) {
      r = await fetch(`/api/laphtml/${lapId}`)
      if (r.ok) break
      if (attempt < 3) await new Promise(res => setTimeout(res, 1000))
    }
    if (!r.ok) { alert("下载失败（帧可能还在写入，稍后再试）"); return }
    const blob = await r.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `lap_${lapId}.html`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) { alert("下载失败: " + e) }
}

export async function cleanupSessions() {
  const keepStr = prompt("保留最近几个会话？（更早的会话将被永久删除，无法恢复）", "3")
  if (keepStr === null) return
  const keep = parseInt(keepStr, 10)
  if (isNaN(keep) || keep < 1) { alert("请输入 ≥1 的整数"); return }
  if (!confirm(`确定永久删除最旧的会话、只保留最近 ${keep} 个吗？\n（当前正在录制的会话不会被删）`)) return
  try {
    const r = await fetch(`/api/cleanup?keep=${keep}`)
    const d = await r.json()
    alert(`清理完成：删除 ${d.deleted} 个旧会话，释放 ${(d.freed_frames * 0.4 / 1048576).toFixed(1)} MB 帧数据`)
    pollLaps(); pollSession()
  } catch (e) { alert("清理失败: " + e) }
}

// 暴露给组件用的读接口
export function isReplayMode() { return replayMode }
export function getCurrentCar() { return currentCar }
