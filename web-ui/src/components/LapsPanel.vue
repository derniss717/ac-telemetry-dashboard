<template>
  <section class="laps-panel">
    <h2>本会话全部圈 <span class="hint" :class="{ 'replay-on': replayText }" @click="onReplayClick">{{ replayText }}</span></h2>
    <div class="laps-stats" v-html="statsHtml"></div>
    <div class="lap-cards">
      <div v-if="!laps.length" class="lap-empty dim">暂无圈数据 — 完成一圈后出现在这里</div>
      <div v-for="row in rows" :key="row.l.id" class="lap-card" :class="row.cls" :title="'点击回放该圈遥测'" @click="onRowClick(row.l)">
        <div class="lap-card-head">
          <span class="lap-no">圈 {{ row.label }}</span>
          <span class="lap-status" :class="statusCls(row)">{{ row.status }}</span>
        </div>
        <div class="lap-time" :class="{ unfinished: row.unfinished }">{{ row.unfinished ? "—" : fmtMs(row.l.total_ms) }}</div>
        <div class="lap-segs">
          <span :class="segCls(row.l.s1_ms, row.minS1)"><i>S1</i>{{ row.unfinished ? "—" : fmtMs(row.l.s1_ms) }}</span>
          <span :class="segCls(row.l.s2_ms, row.minS2)"><i>S2</i>{{ row.unfinished ? "—" : fmtMs(row.l.s2_ms) }}</span>
          <span :class="segCls(row.l.s3_ms, row.minS3)"><i>S3</i>{{ row.unfinished ? "—" : fmtMs(row.l.s3_ms) }}</span>
        </div>
        <div class="lap-meta">
          <span class="lap-top">极速 <b>{{ (row.l.top_speed_kmh || 0).toFixed(1) }}</b> km/h</span>
          <span class="lap-actions">
            <label class="lap-chk-label" :class="{ disabled: row.unfinished }" title="勾选用于两圈对比">
              <input type="checkbox" class="lap-chk" :data-id="row.l.id" :disabled="row.unfinished" :checked="selected.has(String(row.l.id))" @change="onCheck($event, row.l)">
              对比
            </label>
            <button v-if="row.unfinished" class="dl" disabled title="该圈未完成，无法生成报告">报告</button>
            <button v-else class="dl" @click.stop="downloadLap(row.l.id)">报告</button>
          </span>
        </div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-top:12px">
      <button id="btn-compare" :disabled="selected.size !== 2" @click="onCompare">⛭ 对比选中的两圈</button>
      <button id="btn-cleanup" style="margin-left:auto" title="永久删除旧会话数据，释放硬盘空间" @click="cleanupSessions">🗑 清理旧会话</button>
      <span class="hint" id="cmp-hint">{{ cmpHint }}</span>
    </div>
    <div class="chk">点击任意卡片可回放该圈遥测曲线；点「回到实时」恢复；「报告」下载该圈 HTML 单圈报告（图表直观）。</div>
  </section>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { on as subscribe, renderLapsStatsHtml, loadLap, backToLive, compareLaps, downloadLap, cleanupSessions } from '../store/live'
import { fmtMs } from '../lib/fmt'

const laps = ref([])
const statsHtml = ref("")
const selected = ref(new Set())
const replayText = ref("")
const cmpHint = ref("勾选任意两圈（勾选框），对比秒差曲线")

function onLaps({ laps: data }) {
  laps.value = data || []
  statsHtml.value = renderLapsStatsHtml(data)
  // 只保留仍在列表里的勾选
  const ids = new Set((data || []).map(l => String(l.id)))
  const keep = new Set([...selected.value].filter(id => ids.has(id)))
  selected.value = keep
  updateCompareHint()
}
function onReplay({ text, on }) { replayText.value = on ? text : "" }

const rows = computed(() => {
  const ls = laps.value
  let best = Infinity
  for (const l of ls) if (l.is_valid && l.total_ms && l.total_ms < best) best = l.total_ms
  let minS1 = Infinity, minS2 = Infinity, minS3 = Infinity
  for (const l of ls) {
    if (!(l.is_valid && !l.outlap && !l.is_inlap && l.total_ms > 0)) continue
    if (l.s1_ms > 0 && l.s1_ms < minS1) minS1 = l.s1_ms
    if (l.s2_ms > 0 && l.s2_ms < minS2) minS2 = l.s2_ms
    if (l.s3_ms > 0 && l.s3_ms < minS3) minS3 = l.s3_ms
  }
  const lapNoCount = {}
  for (const l of ls) lapNoCount[l.lap_no] = (lapNoCount[l.lap_no] || 0) + 1
  const lapNoSeen = {}
  return ls.map(l => {
    const unfinished = !l.total_ms || l.total_ms <= 0
    const isBest = l.total_ms === best
    const cls = []
    if (isBest) cls.push("best-row")
    if (l.outlap || l.is_inlap || unfinished) cls.push("pit-row")
    if (replayText.value && l.lap_no === replayLapNoRef()) cls.push("replay-row")
    lapNoSeen[l.lap_no] = (lapNoSeen[l.lap_no] || 0) + 1
    let label = String(l.lap_no)
    if (lapNoCount[l.lap_no] > 1) label += "abcdefgh"[lapNoSeen[l.lap_no] - 1]
    let status
    if (unfinished) status = "未完成"
    else if (l.outlap) status = "出场圈"
    else if (l.is_inlap) status = "进站圈"
    else status = l.is_valid ? "有效" : "无效/重开"
    return { l, label, status, cls: cls.join(" "), unfinished, minS1, minS2, minS3 }
  })
})

let replayLapNo = null
function replayLapNoRef() { return replayLapNo }
import { isReplayMode } from '../store/live'

function segCls(v, m) { return (v > 0 && v === m) ? "best-seg" : "" }
function statusCls(row) {
  if (row.unfinished) return "st-un"
  if (row.l.outlap || row.l.is_inlap) return "st-pit"
  return row.l.is_valid ? "st-ok" : "st-bad"
}
function onCheck(e, l) {
  e.stopPropagation()
  const id = String(l.id)
  const next = new Set(selected.value)
  if (e.target.checked) {
    next.add(id)
    if (next.size > 2) { // 最多选两圈：去掉最早选的
      const first = [...next][0]
      next.delete(first)
    }
  } else next.delete(id)
  selected.value = next
  updateCompareHint()
}
function updateCompareHint() {
  cmpHint.value = selected.value.size === 2
    ? "已选两圈，点击「对比」查看速度与秒差曲线"
    : "勾选任意两圈（勾选框），对比秒差曲线"
}
function onRowClick(l) {
  replayLapNo = l.lap_no
  loadLap(l)
}
function onReplayClick() { if (replayText.value) { replayLapNo = null; backToLive() } }
async function onCompare() {
  const sel = laps.value.filter(l => selected.value.has(String(l.id)) && l.total_ms > 0)
  if (sel.length !== 2) return
  const tagMap = {}
  for (const r of rows.value) tagMap[r.l.id] = r.label
  await compareLaps(sel.map(l => ({ id: l.id, no: tagMap[l.id] })))
}

onMounted(() => { subscribe('laps', onLaps); subscribe('replay', onReplay) })
</script>

<style scoped>
/* Volanta 风格圈列表卡片 */
.lap-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
.lap-card {
  background: var(--panel2);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 11px 13px;
  cursor: pointer;
  transition: border-color .2s ease, box-shadow .2s ease, transform .2s ease;
}
.lap-card:hover {
  border-color: var(--border-hover);
  box-shadow: 0 0 18px rgba(88, 166, 255, 0.12);
  transform: translateY(-2px);
}
.lap-card.best-row {
  border-color: rgba(63, 185, 80, 0.45);
  box-shadow: 0 0 18px rgba(63, 185, 80, 0.15);
}
.lap-card.pit-row { opacity: 0.55; }
.lap-card.replay-row { background: rgba(63, 185, 80, 0.12); border-color: rgba(63, 185, 80, 0.5); }
.lap-empty { grid-column: 1 / -1; text-align: center; padding: 26px 0; }
.lap-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.lap-no { font-size: 12px; font-weight: 700; letter-spacing: .4px; color: var(--dim); }
.lap-status {
  font-size: 10px; font-weight: 700; letter-spacing: .5px;
  padding: 2px 8px; border-radius: 999px;
}
.lap-status.st-ok { background: rgba(63, 185, 80, 0.15); color: var(--green); }
.lap-status.st-pit { background: rgba(139, 148, 158, 0.15); color: var(--dim); }
.lap-status.st-un { background: rgba(139, 148, 158, 0.12); color: var(--dim); }
.lap-status.st-bad { background: rgba(248, 81, 73, 0.15); color: var(--red); }
.lap-time {
  font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums;
  color: var(--text); line-height: 1.15; margin-bottom: 8px;
  text-shadow: 0 0 22px rgba(88, 166, 255, 0.18);
}
.lap-time.unfinished { color: var(--dim); text-shadow: none; }
.lap-card.best-row .lap-time { color: var(--green); text-shadow: 0 0 22px rgba(63, 185, 80, 0.3); }
.lap-segs { display: flex; gap: 6px; margin-bottom: 9px; }
.lap-segs span {
  flex: 1; font-size: 11px; color: var(--dim);
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border);
  border-radius: 7px; padding: 5px 7px;
  font-variant-numeric: tabular-nums;
  display: flex; align-items: baseline; gap: 5px;
}
.lap-segs span i { font-style: normal; font-size: 10px; opacity: .7; }
.lap-segs span.best-seg { color: var(--purple); border-color: rgba(210, 168, 255, 0.4); background: rgba(210, 168, 255, 0.08); }
.lap-meta { display: flex; justify-content: space-between; align-items: center; }
.lap-top { font-size: 11px; color: var(--dim); }
.lap-top b { color: var(--text); font-variant-numeric: tabular-nums; }
.lap-actions { display: flex; align-items: center; gap: 7px; }
.lap-chk-label {
  font-size: 11px; color: var(--dim); cursor: pointer;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 8px; border: 1px solid var(--border); border-radius: 6px;
  transition: all .2s ease;
}
.lap-chk-label:hover { color: var(--blue); border-color: var(--border-hover); }
.lap-chk-label.disabled { opacity: .4; cursor: not-allowed; }
.lap-chk { accent-color: var(--blue); }

/* 移动端：卡片单列铺满 */
@media (max-width: 700px) {
  .lap-cards { grid-template-columns: 1fr; gap: 8px; }
  .lap-time { font-size: 22px; }
}
</style>
