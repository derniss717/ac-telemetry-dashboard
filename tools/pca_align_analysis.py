"""分析：轨迹世界坐标主轴 vs 赛道图像素主轴，算出旋转校正角。

用法: python tools/pca_align_analysis.py <track_dir>
输出: 数据主轴角 / 图像主轴角 / 需旋转角度 / 是否疑似镜像
"""
import sys, sqlite3, glob, math
import numpy as np
from PIL import Image
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ac.db"
sys.path.insert(0, str(ROOT))
import config as cfg

def track_dir_for(track_key, tracks_root):
    for d in Path(tracks_root).iterdir():
        if d.is_dir() and track_key.lower() in d.name.lower():
            return d
    return None

def pca_angle(pts):
    """二维点集第一主成分与 x 轴夹角（弧度）。pts: (N,2)"""
    pts = np.asarray(pts, dtype=float)
    c = pts.mean(axis=0)
    cov = np.cov((pts - c).T)
    w, v = np.linalg.eigh(cov)
    ang = math.atan2(v[1, -1], v[0, -1])   # 最大特征值对应向量
    return ang, c, v[:, -1]

def load_track_pts(track_key, limit=4000):
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("SELECT pos_x, pos_z FROM frames WHERE pos_x IS NOT NULL AND pos_z IS NOT NULL ORDER BY id LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    return np.asarray(rows, dtype=float)

def load_image_pts(png_path, sample=12000):
    img = Image.open(png_path).convert("RGBA")
    a = np.asarray(img)
    h, w = a.shape[:2]
    # 取赛道像素：不透明（alpha>128）或非纯黑背景
    mask = a[..., 3] > 128
    if mask.sum() < 100:
        # 可能无 alpha，用亮度阈值
        gray = a[..., :3].mean(axis=2)
        mask = gray > 30
    ys, xs = np.nonzero(mask)
    if len(xs) > sample:
        idx = np.random.default_rng(0).choice(len(xs), sample, replace=False)
        xs, ys = xs[idx], ys[idx]
    pts = np.stack([xs.astype(float), ys.astype(float)], axis=1)
    return pts, w, h

def main():
    track_key = sys.argv[1] if len(sys.argv) > 1 else "zandvoort2020"
    import config as cfg
    tracks_root = cfg.find_ac_tracks_dir()
    print("tracks_root:", tracks_root)
    td = track_dir_for(track_key, tracks_root)
    if not td:
        print("track dir not found")
        return
    pngs = sorted(glob.glob(str(td / "ui" / "**" / "outline_cropped.png"), recursive=True)) or \
           sorted(glob.glob(str(td / "ui" / "**" / "outline.png"), recursive=True))
    pngs = [p for p in pngs if "outline_1" not in p]
    if not pngs:
        print("no outline png")
        return
    png = pngs[0]
    print("png:", png)

    data_pts = load_track_pts(track_key)
    if data_pts is None or len(data_pts) < 30:
        print(f"no enough track data ({0 if data_pts is None else len(data_pts)} pts)")
        return
    img_pts, iw, ih = load_image_pts(png)

    da, dc, dv = pca_angle(data_pts)
    ia, ic, iv = pca_angle(img_pts)
    print(f"track pts: {len(data_pts)}  img pts: {len(img_pts)}  img {iw}x{ih}")
    print(f"data PCA angle: {math.degrees(da):.1f} deg  dir=({dv[0]:.3f},{dv[1]:.3f})")
    print(f"img  PCA angle: {math.degrees(ia):.1f} deg  dir=({iv[0]:.3f},{iv[1]:.3f})")
    d = math.degrees(ia - da) % 180
    print(f"rotate data by: {d:.1f} deg (mod 180; 若 >90 考虑镜像)")

if __name__ == "__main__":
    main()
