"""赛道图与游戏世界坐标的对齐计算。

原理：AC 赛道 outline.png 与游戏世界坐标方向通常不一致（赛道图作者画图时
方向自由）。给定赛道图和一段轨迹点（世界坐标 x, z），通过旋转 + 镜像搜索
找到使轨迹点最贴合赛道像素的变换。theta 取模 180°（PCA 主轴的 180° 模糊），
flipX/flipZ 处理镜像差异。

策略：先用 PCA 算出数据主轴角（粗略 0-180°），再在 PCA 角 ±30° 范围
内细搜 + 4 种 flip 组合。少数据时也能稳定收敛到正确解附近。

提取赛道像素用 alpha 通道（透明 = 背景）。无 alpha 时用亮度阈值。
最近邻用 scipy.cKDTree 加速；分数=轨迹点映射到图像后落在赛道像素邻域内的比例。
"""
from __future__ import annotations
import io
import math
from typing import Optional, Tuple

import numpy as np
from PIL import Image

try:
    from scipy.spatial import cKDTree
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _extract_track_pixels(png_bytes: bytes) -> Optional[Tuple[np.ndarray, int, int]]:
    """从 PNG bytes 提取赛道像素点 (u, v) 数组 + 图尺寸。"""
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception:
        return None
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    a = arr[..., 3] > 128
    if a.sum() < 100:
        a = arr[..., :3].mean(axis=2) > 30
    ys, xs = np.nonzero(a)
    if len(xs) < 30:
        return None
    return np.stack([xs.astype(float), ys.astype(float)], axis=1), w, h


def _transform(pts: np.ndarray, theta: float, flip_x: bool, flip_z: bool,
               iw: int, ih: int, pad: float = 0.85
               ) -> np.ndarray:
    """世界坐标点 (x, z) → 图像坐标 (u, v)，应用旋转+镜像+归一化到图像中心。"""
    c = pts.mean(axis=0)
    p = pts - c
    th = math.radians(theta)
    cs, sn = math.cos(th), math.sin(th)
    rot = np.array([[cs, -sn], [sn, cs]], dtype=float)
    p = p @ rot.T
    if flip_x:
        p[:, 0] *= -1
    if flip_z:
        p[:, 1] *= -1
    x0, x1 = p[:, 0].min(), p[:, 0].max()
    z0, z1 = p[:, 1].min(), p[:, 1].max()
    sx = max(x1 - x0, 1e-6)
    sz = max(z1 - z0, 1e-6)
    s = min((iw * pad) / sx, (ih * pad) / sz)
    tx = iw / 2 - (x0 + x1) / 2 * s
    tz = ih / 2 - (z0 + z1) / 2 * s
    return np.stack([p[:, 0] * s + tx, p[:, 1] * s + tz], axis=1)


def _score(mapped: np.ndarray, tree, iw: int, ih: int, tol: float) -> float:
    if not _HAS_SCIPY:
        return 0.0
    d, _ = tree.query(mapped, k=1)
    inside = ((mapped[:, 0] >= 0) & (mapped[:, 0] < iw) &
              (mapped[:, 1] >= 0) & (mapped[:, 1] < ih))
    return float(((d <= tol) & inside).mean())


def _pca_angle(pts: np.ndarray) -> float:
    """二维点集第一主成分与 x 轴夹角（弧度）。"""
    c = pts.mean(axis=0)
    cov = np.cov((pts - c).T)
    w, v = np.linalg.eigh(cov)
    return float(np.arctan2(v[1, -1], v[0, -1]))


def compute_align(png_bytes: bytes, world_pts: np.ndarray,
                  fine_window: float = 4.0) -> Optional[dict]:
    """从赛道图 bytes + 世界坐标轨迹点 (N, 2) 算 {theta, flipX, flipZ, score}。

    返回 None 表示数据不足（轨迹点 < 30 或找不到赛道像素）。
    """
    if world_pts is None or len(world_pts) < 30:
        return None
    pix = _extract_track_pixels(png_bytes)
    if pix is None:
        return None
    img_pts, iw, ih = pix
    if not _HAS_SCIPY:
        return None
    tree = cKDTree(img_pts)
    pts = np.asarray(world_pts, dtype=float)

    # PCA 引导：主轴角归到 [0, 180°)
    theta_pca_deg = math.degrees(_pca_angle(pts)) % 180.0
    lo = max(0.0, theta_pca_deg - 30.0)
    hi = min(180.0, theta_pca_deg + 30.0)

    best = (-1.0, None)
    for fx in (False, True):
        for fz in (False, True):
            for deg in np.arange(lo, hi + 0.1, 1.0):
                mp = _transform(pts, float(deg), fx, fz, iw, ih)
                s = _score(mp, tree, iw, ih, tol=5.0)
                if s > best[0]:
                    best = (s, (float(deg), fx, fz))
    if best[1] is None:
        return None
    coarse_deg, fx, fz = best[1]
    # 细搜 ±fine_window
    for d_off in np.arange(-fine_window, fine_window + 0.1, 0.5):
        deg = (coarse_deg + float(d_off)) % 180
        mp = _transform(pts, deg, fx, fz, iw, ih)
        s = _score(mp, tree, iw, ih, tol=4.0)
        if s > best[0]:
            best = (s, (deg, fx, fz))
    return {"theta": float(best[1][0]), "flipX": bool(best[1][1]),
            "flipZ": bool(best[1][2]), "score": float(best[0])}
