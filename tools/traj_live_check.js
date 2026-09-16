// 无头截图：抓取 5173 新版界面，验证轨迹图渲染
const path = require('path')
const puppeteer = require(path.join('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core'))

;(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu'],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1440, height: 900 })

  // 等数据流进来（等 8 秒，让 WS 连上 + 攒轨迹点）
  await page.goto('http://localhost:5173', { waitUntil: 'networkidle2', timeout: 30000 })
  await new Promise(r => setTimeout(r, 8000))

  // 调试信息
  const dbg = await page.evaluate(() => {
    const svg = document.querySelector('.traj-svg')
    const box = document.querySelector('.traj-box')
    const img = svg ? svg.querySelector('image') : null
    const paths = svg ? [...svg.querySelectorAll('path')].map(p => p.getAttribute('d')?.length || 0) : []
    const live = svg ? svg.querySelector('circle') : null
    return {
      svg: !!svg,
      boxWH: box ? [box.clientWidth, box.clientHeight] : null,
      imgSrc: img ? (img.getAttribute('href') || '').slice(0, 30) : null,
      imgXYWH: img ? [img.getAttribute('x'), img.getAttribute('y'), img.getAttribute('width'), img.getAttribute('height')] : null,
      pathLens: paths,
      livePt: live ? [live.getAttribute('cx'), live.getAttribute('cy')] : null,
      trajDbg: window.__trajDbg || null,
      livePts: window.__recvCount || 0,
    }
  })
  console.log('DBG:', JSON.stringify(dbg, null, 2))

  // 只截轨迹图卡片区域
  const box = await page.$('.traj-card')
  if (box) {
    await box.screenshot({ path: 'E:/ac-telemetry/tools/align_vis/traj_live_check.png' })
    console.log('saved traj card screenshot')
  } else {
    await page.screenshot({ path: 'E:/ac-telemetry/tools/align_vis/traj_live_check.png' })
    console.log('saved full page screenshot (no traj-card)')
  }

  await browser.close()
})().catch(e => { console.error('ERR:', e.message); process.exit(1) })
