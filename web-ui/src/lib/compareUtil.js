// 对比图辅助：轻量滑动平均（与实时图 smooth 一致）
export function smooth(arr, w) {
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
