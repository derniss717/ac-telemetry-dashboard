# AC 遥测仪表盘（Assetto Corsa Telemetry）

神力科莎（Assetto Corsa）的实时遥测仪表盘：跑圈时实时显示车速、转速、G 值、胎温、ERS 充放电、方向盘角度等数据；自动记录每一圈，支持回放、单圈报告下载与两圈对比分析。

纯本地运行，数据不出本机；电脑浏览器与手机都能看。提供打包版（免安装双击即用）与源码版（命令行/图形界面）。

## 功能

- **实时仪表盘**（浏览器，电脑 / 手机自适应）
  - 顶部数据条：车速圆环 / 转速 / 档位 / 圈速 / 扇区 / 方向盘 / KERS / 油量 / 轮胎配方 / 世界坐标 / 朝向 / 速度矢量
  - 曲线图：速度 / 油门刹车 / 转速 / 挡位 / 纵向横向 G / 滑移率 / 胎温 / 刹车盘温 / 磨损 / 胎压 / 涡轮 / ERS 充放电 / 底板高度 / 悬架行程
  - **鼠标悬停十字线联动**：在任意图表卡片上滑动，竖线跟随鼠标，各条曲线圆点按横轴位置插值显示，顶部数据条同步显示该位置的真实数据（MoTeC 风格）
  - 图表点击放大后可滚轮缩放 X 轴、按住拖拽平移；点图例可单独隐藏/显示曲线
- **自动圈记录**：每圈用时、S1/S2/S3 分段、极速、轮胎配方；理论最快圈（各段最优相加）与提升空间
- **单圈报告**：下载 HTML 报告（文件名 = 赛道 + 圈速，如 `红牛环_1-08.024.html`）
- **两圈对比**：速度 / 油门 / 刹车 / 涡轮 / ERS 放电 / 纵向 G / 横向 G / 方向盘 / 秒差 九张对比图
- **任意圈回放**；DRS 开启区间在速度曲线上标红
- **数据自管理**：自动保留最近 8 个会话，更早自动清理，防止数据库无限膨胀变卡

## 快速开始（源码版）

```bash
pip install -r requirements.txt   # 必需：numpy + websockets（Python 3.10+）
python main.py doctor             # 检查数据源与 AC 配置
python main.py                    # 启动录制 + 仪表盘 → http://127.0.0.1:8080
python main.py gui                # 图形界面版（开始/停止/检查配置/生成报告）
```

**手机上看**：手机连同一局域网，浏览器打开 `http://<电脑IP>:8080`（如 `http://192.168.1.100:8080`）。
首次可能需要放行 Windows 防火墙的 8080 / 8081 入站。

### 游戏侧要求

- Assetto Corsa（正版，推荐 Content Manager / CSP）
- **默认数据源是共享内存，无需任何配置**：游戏一启动就能读到（UDP 广播仅在旧环境下才需要，
  `python main.py doctor` 可帮忙写入 `cfg/udp.ini`）
- 世界坐标 / 朝向 / 速度矢量来自 RT 遥测订阅（游戏常驻监听 9996，握手后回发 RTCarInfo）

### 前端开发（可选）

`web-ui/dist` 已入库，直接跑 `python main.py` 就是新版界面，**不改前端就不需要装 Node**。
要改界面时：

```bash
cd web-ui
npm install
npm run dev      # 开发服务器 5173，/api 自动代理到 8080（需同时跑 python main.py）
npm run build    # 产物写入 web-ui/dist，提交前记得一并 commit
```

## 打包（PyInstaller）

打包环境用 conda（含 tkinter）：

```bash
C:\ProgramData\miniconda3\python.exe -m PyInstaller ac-telemetry-v2.0.spec \
  --distpath dist-2.0 --workpath build-2.0 --noconfirm
```

spec 里两个关键点：`binaries` 全量打入 conda 的 `Library/bin`（否则缺 sqlite3 / tcl-tk / libffi），
`datas` 同时收 `web-ui/dist`（新版界面）与 `web/`（旧版兜底）。
打包版为单文件 exe，双击即用，数据自动存放在 exe 旁 `data/` 目录。

## 技术架构

```
数据源            共享内存（优先）/ UDP 广播 → ac_udp.py 解析
圈状态机          lap_detector.py  过线检测 + S1/S2/S3 + 出/进站圈 + 无效圈判定
缓冲/落库         ring_buffer.py（线程安全环形缓冲）→ recorder.py 批量写 SQLite
存储              storage.py  sessions / laps / frames 三表，WAL + 读写分离
HTTP/实时推送     dashboard.py  8080 HTTP + 8081 WebSocket（缺 websockets 时回退 HTTP 轮询）
前端              web-ui/  Vue3 + Vite + Chart.js（全部本地依赖，无 CDN）
```

前端优先连 WebSocket（200ms 一批增量帧），连不上自动回退 HTTP 轮询、断开自动重连；
SQLite 读写分离（录制走写连接、查询走读连接），所以高频落库不会卡住仪表盘刷新。

## 项目结构

```
main.py           命令行入口（start / gui / analyze / export / list / doctor / config）
gui.py            Tkinter 图形界面（双击 exe 的界面）
dashboard.py      HTTP 服务 + 路由表 + WebSocket 推送 + 单圈报告生成
ac_udp.py         AC 数据包解析（共享内存 / UDP 共用偏移表）+ 接收器 + RT 遥测客户端
shared_mem.py     共享内存读取（physics / graphic / static）
lap_detector.py   圈完成检测（completedLaps 权威边界 + 计时骤降兜底）
recorder.py       会话/圈生命周期 + 录制线程 + 自动清理
storage.py        SQLite 存取（WAL、读写分离、旧库兼容补列）
ring_buffer.py    线程安全环形缓冲
trackalign.py     赛道轨迹对齐（位置数据 → 赛道蓝图，模板匹配）
analyze.py        赛后分析报告（plotly，可选依赖）
simulate.py       无游戏时的模拟数据源（测试用）
selftest.py       自检脚本
web-ui/           新版前端（Vue3 + Vite 源码 + dist 产物）
web/              旧版前端（兜底，dashboard 找不到 dist 时使用）
templates/        报告模板
tools/            开发期辅助脚本（轨迹对齐参数搜索、弯角数据去重等）
```

## 数据

- 存放位置：`data/` 目录（SQLite，`sessions` / `laps` / `frames` 三表）
- 备份：整个 `data/` 文件夹拷走即完整备份
- 每次启动为全新会话：程序打开时自动清空历史记录（想留着就先把 `data/` 拷走）
- 容量：运行中自动保留最近 8 个会话，超限删最旧

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)（与程序内「点右上角版本号」看到的内容同源，
由 `config.py` 的 `CHANGELOG` 生成，改了之后跑 `python tools/gen_changelog.py` 重新导出）。

## License

MIT
