"""自动找最优世界坐标→赛道图变换：θ + (flipX?, flipZ?)，分数=轨迹点映射后落在赛道像素上的比例。"""
import sys, sqlite3, glob, math
import numpy as np
from PIL import Image
from pathlib import Path
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ac.db"
sys.path.insert(0, str(ROOT))
import config as cfg

def load_track_pts(limit=12000):
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("SELECT pos_x, pos_z FROM frames WHERE pos_x IS NOT NULL AND pos_z IS NOT NULL ORDER BY id LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return np.asarray(rows, dtype=float)

def load_image(png_path):
    img = Image.open(png_path).convert("RGBA")
    arr = np.asarray(img)
    a = arr[..., 3] > 128
    if a.sum() < 100:
        a = arr[..., :3].mean(axis=2) > 30
    ys, xs = np.nonzero(a)
    pts = np.stack([xs.astype(float), ys.astype(float)], axis=1)
    return np.asarray(img), pts, img.size

def transform_pts(pts, theta, flip_x, flip_z, iw, ih, pad=0.85):
    c = pts.mean(axis=0)
    p = pts - c
    th = math.radians(theta)
    cs, sn = math.cos(th), math.sin(th)
    R = np.array([[cs, -sn], [sn, cs]])
    p = p @ R.T
    if flip_x: p[:, 0] *= -1
    if flip_z: p[:, 1] *= -1
    x0, x1 = p[:, 0].min(), p[:, 0].max()
    z0, z1 = p[:, 1].min(), p[:, 1].max()
    sx = max(x1 - x0, 1e-6); sz = max(z1 - z0, 1e-6)
    s = min((iw * pad) / sx, (ih * pad) / sz)
    tx = iw / 2 - (x0 + x1) / 2 * s
    tz = ih / 2 - (z0 + z1) / 2 * s
    return np.stack([p[:, 0] * s + tx, p[:, 1] * s + tz], axis=1)

def score(mapped, tree, iw, ih, tol=4.0):
    d, _ = tree.query(mapped, k=1)
    in_img = (mapped[:, 0] >= 0) & (mapped[:, 0] < iw) & (mapped[:, 1] >= 0) & (mapped[:, 1] < ih)
    hit = (d <= tol) & in_img
    return float(hit.mean())

def main():
    track_key = sys.argv[1] if len(sys.argv) > 1 else "zandvoort2020"
    tracks_root = cfg.find_ac_tracks_dir()
    td = None
    for d in Path(tracks_root).iterdir():
        if d.is_dir() and track_key.lower() in d.name.lower():
            td = d; break
    pngs = sorted(glob.glob(str(td / "ui" / "**" / "outline_cropped.png"), recursive=True)) or \
           sorted(glob.glob(str(td / "ui" / "**" / "outline.png"), recursive=True))
    pngs = [p for p in pngs if "outline_1" not in p]
    img, img_pts, (iw, ih) = load_image(pngs[0])
    pts = load_track_pts()
    print(f"pts={len(pts)} img={iw}x{ih} img_pts={len(img_pts)}")
    tree = cKDTree(img_pts)

    best = (-1, None)
    for flip_x in (False, True):
        for flip_z in (False, True):
            for deg in range(0, 180, 2):
                mp = transform_pts(pts, deg, flip_x, flip_z, iw, ih)
                s = score(mp, tree, iw, ih, tol=5.0)
                if s > best[0]:
                    best = (s, (deg, flip_x, flip_z))
    print("粗搜(步长2°):", best)
    # 细搜 ±4° 步长 0.5°
    coarse_deg, fx, fz = best[1]
    for d_off in np.arange(-4, 4.1, 0.5):
        deg = (coarse_deg + d_off) % 180
        mp = transform_pts(pts, deg, fx, fz, iw, ih)
        s = score(mp, tree, iw, ih, tol=4.0)
        if s > best[0]:
            best = (s, (deg, fx, fz))
    print("细搜:", best)
    print(f"推荐: theta={best[1][0]:.1f}°  flipX={best[1][1]}  flipZ={best[1][2]}  score={best[0]:.3f}")

if __name__ == "__main__":
    main()
