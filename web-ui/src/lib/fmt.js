// 格式化工具（从 v1.2 app.js 迁移）

export function fmtMs(ms) {
  if (ms == null || isNaN(ms) || ms <= 0) return "--:--.---";
  ms = Math.round(ms);
  const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000), f = ms % 1000;
  return `${m}:${String(s).padStart(2, "0")}.${String(f).padStart(3, "0")}`;
}

export function fmtDist(m) {
  if (m == null || isNaN(m)) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

export function gearText(g) {
  // AC 共享内存 gear 偏移编码（官方：0=R, 1=N, 2=1档, 9=8档）
  if (g == null) return "-";
  if (g <= 0) return "R";      // 0 = 倒挡
  if (g === 1) return "N";     // 1 = 空挡
  return String(g - 1);        // 2+ = 档位-1
}
