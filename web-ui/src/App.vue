<template>
  <div class="app">
    <header>
      <div class="brand">
        <h1>AC 遥测仪表盘</h1>
        <span class="ver-tag">v2.0</span>
      </div>
      <div class="session-info">
        <span id="s-session">{{ sessionId ? `会话 #${sessionId}` : "—" }}</span>
        <span id="s-car">{{ session.car }}</span>
        <span id="s-track">{{ session.track }}</span>
        <span id="s-laps" class="dim">{{ session.lapsDone ? `${session.lapsDone} 圈` : "" }}</span>
        <span class="badge" :class="statusCls">{{ statusText }}</span>
      </div>
    </header>

    <div class="banner" :class="{ hidden: !banner }">
      ⚠ 5 秒未收到 AC 数据 —— 请确认已开启 UDP 广播（运行 <code>python main.py doctor</code> 检查）
    </div>

    <TileBar />
    <section class="charts">
      <ChartCard v-for="def in visibleDefs" :key="def.key" :def="def" />
    </section>
    <LapsPanel />
    <ComparePanel />
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { CHART_DEFS } from './lib/chartDefs'
import { on as subscribe, start } from './store/live'
import TileBar from './components/TileBar.vue'
import ChartCard from './components/ChartCard.vue'
import LapsPanel from './components/LapsPanel.vue'
import ComparePanel from './components/ComparePanel.vue'

const sessionId = ref(null)
const session = ref({ car: "—", track: "—", lapsDone: 0 })
const banner = ref(false)
const status = ref({ live: false, in_pit: false })
const extOk = ref({ tyreO: false, brakeT: false, wear: false })

const statusText = computed(() => {
  if (!status.value.live) return "未连接/回放"
  if (status.value.in_pit) return "进站中"
  return "LIVE"
})
const statusCls = computed(() => {
  if (!status.value.live) return "badge off"
  if (status.value.in_pit) return "badge paused"
  return "badge live"
})
// 自检测图表：数据可靠才显示（tyreO/brakeT/wear）
const visibleDefs = computed(() => CHART_DEFS.filter(d =>
  d.initialVisible === false ? extOk.value[d.key] : true))

function onTiles(t) { status.value = { live: t.live, in_pit: t.in_pit } }
function onBanner(b) { banner.value = b }
function onSession(s) { session.value = s }
function onLaps({ sessionId: sid }) { sessionId.value = sid }
function onExt(e) { extOk.value = e }

onMounted(() => {
  subscribe('tiles', onTiles)
  subscribe('banner', onBanner)
  subscribe('session', onSession)
  subscribe('laps', onLaps)
  subscribe('ext', onExt)
  start()
})
</script>
