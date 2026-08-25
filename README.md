# AC 遥测仪表盘（Assetto Corsa Telemetry）

神力科莎（Assetto Corsa）的实时遥测仪表盘：跑圈时实时显示车速、转速、G 值、胎温、ERS 充放电、方向盘角度等数据；自动记录每一圈，支持回放、单圈报告下载与两圈对比分析。

纯本地运行，数据不出本机；提供打包版（免安装双击即用）与源码版（命令行/图形界面）。

## 功能

- **实时仪表盘**（浏览器 Web 界面）
  - 顶部卡片：车速 / 转速 / 档位 / 圈速 / 轮胎配方 / KERS 电量 / 方向盘角度（左打红、右打绿）
  - 曲线图：速度 / 油门刹车 / 转速 / 挡位 / 纵向横向 G / 滑移率 / 胎温 / 涡轮压力 / ERS 充放电…
- **自动圈记录**：每圈用时、S1/S2/S3 分段、极速、轮胎配方；理论最快圈（各段最优相加）与提升空间
- **单圈报告**：下载 HTML 报告（文件名 = 赛道 + 圈速，如 `红牛环_1-08.024.html`）
- **两圈对比**：速度 / 油门 / 刹车 / 涡轮 / ERS 放电 / 纵向 G / 横向 G / 方向盘 / 秒差 九张对比图
- **任意圈回放**；图表卡片点击放大细看
- **数据自管理**：自动保留最近 8 个会话，更早自动清理，防止数据库无限膨胀变卡

## 快速开始（源码版）

```bash
pip install -r requirements.txt   # 唯一第三方依赖：numpy（Python 3.10+）
python main.py doctor              # 检查/开启 AC 的遥测广播（UDP 或共享内存）
python main.py                     # 启动录制 + 仪表盘，浏览器打开 http://127.0.0.1:8080
python main.py gui                 # 图形界面版
```

### 游戏侧要求

- Assetto Corsa（正版，推荐 Content Manager / CSP）
- 开启 `ENABLE_DEV_APPS`（`python main.py doctor` 自动写入并提示重启游戏）
- 数据源：优先共享内存（无需额外配置），也可用 UDP 广播（`cfg/udp.ini`）

## 打包（PyInstaller）

```bash
pyinstaller --onefile --windowed --name "AC遥测" \
  --add-data "web;web" \
  --add-binary "C:\ProgramData\miniconda3\Library\bin;." \
  gui.py
```

打包版为单文件 exe，双击即用，数据自动存放在 exe 旁 `data/` 目录。

## 项目结构

```
ac_udp.py         UDP 广播接收
shared_mem.py     共享内存读取（AC 的 physics/graphic/static 内存映射）
lap_detector.py   圈完成检测（completedLaps / 扇区 / 兜底）
recorder.py       批量落库 + 自动清理
storage.py        SQLite 存取（sessions/laps/frames）
dashboard.py      HTTP 服务（/api/* + 静态页面 + 单圈报告生成）
gui.py            Tkinter 图形界面（开始/停止/检查配置/打开仪表盘）
web/              仪表盘前端（HTML/JS/CSS，Chart.js）
simulate.py       无游戏模拟 UDP 广播（测试用）
```

## 数据

- 存放位置：`data/` 目录（SQLite，`sessions` / `laps` / `frames` 三表）
- 备份：整个 `data/` 文件夹拷走即完整备份
- 容量：自动清理保留最近 8 个会话

## License

MIT
