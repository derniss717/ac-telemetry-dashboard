"""可视化验证（PCA引导版）：把轨迹用 4 个高 score 候选 + 之前 hardcode 的 (98, T) 画到赛道图上。"""
import sys, sqlite3, glob, math
import numpy as np
from PIL import Image, ImageDraw
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ac.db"
sys.path.insert(0, str(ROOT))
import config as cfg
from trackalign import _transform

def load_track_pts(limit=30000):
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("SELECT pos_x, pos_z FROM frames WHERE pos_x IS NOT NULL AND pos_z IS NOT NULL "
                "AND session_id IN (SELECT id FROM sessions WHERE track='zandvoort2020') "
                "ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return np.asarray(rows, dtype=float)

def load_image(png_path):
    img = Image.open(png_path).convert("RGBA")
    return img, np.asarray(img)

def render(track_pts, img, iw, ih, theta, flip_x, flip_z, tag):
    canvas = img.copy().convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    mp = _transform(track_pts.astype(float), theta, flip_x, flip_z, iw, ih)
    pts2 = list(zip(mp[:, 0], mp[:, 1]))
    for i in range(len(pts2) - 1):
        d.line([pts2[i], pts2[i + 1]], fill=(80, 170, 255, 220), width=2)
    d.ellipse([pts2[-1][0]-5, pts2[-1][1]-5, pts2[-1][0]+5, pts2[-1][1]+5], fill=(255, 80, 80, 255))
    out = Image.alpha_composite(canvas, overlay)
    return out

def main():
    track_key = "zandvoort2020"
    tracks_root = cfg.find_ac_tracks_dir()
    td = None
    for d in Path(tracks_root).iterdir():
        if d.is_dir() and track_key.lower() in d.name.lower():
            td = d; break
    pngs = sorted(glob.glob(str(td / "ui" / "**" / "outline_cropped.png"), recursive=True))
    pngs = [p for p in pngs if "outline_1" not in p]
    img, arr = load_image(pngs[0])
    iw, ih = img.size
    pts = load_track_pts()
    print(f"pts={len(pts)} img={iw}x{ih}")

    outdir = ROOT / "tools" / "align_vis"
    outdir.mkdir(exist_ok=True)
    # 多组候选：之前 hardcode 的 + 穷举最优 + 几个有代表性的
    cands = [
        (98.0, True, False, "hardcode_98_T"),
        (178.0, True, True, "exhaust_178_TT"),
        (6.0, False, False, "exhaust_6_FF"),
        (0.0, False, False, "exhaust_0_FF"),
        (90.0, True, False, "exhaust_90_TF"),
        (36.0, False, False, "pca_36_FF"),  # PCA 角
        (90.0, False, False, "exhaust_90_FF"),
    ]
    for ang, fx, fz, tag in cands:
        out = render(pts, img, iw, ih, ang, fx, fz, tag=tag)
        out.save(outdir / f"zandvoort2020_30k_{tag}.png")
        print("saved", tag)
    # 拼成 2x4 网格
    files = sorted(outdir.glob("zandvoort2020_30k_*.png"))
    from PIL import ImageDraw, ImageFont
    cols, rows = 4, 2
    cell = 256
    grid = Image.new("RGB", (cell * cols, cell * rows + 20 * rows), (20, 20, 20))
    for i, f in enumerate(files[:cols*rows]):
        t = Image.open(f).resize((cell, cell))
        label = f.stem.split("_30k_", 1)[-1]
        grid.paste(t, ((i % cols) * cell, (i // cols) * (cell + 20)))
    grid.save(outdir / "zandvoort2020_30k_grid.png")
    print("grid saved")

if __name__ == "__main__":
    main()
