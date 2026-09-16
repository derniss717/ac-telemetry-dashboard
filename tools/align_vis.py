"""可视化验证：把轨迹点用不同变换叠加到赛道图上，存 PNG 供人工确认。"""
import sys, sqlite3, glob, math
import numpy as np
from PIL import Image, ImageDraw
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ac.db"
sys.path.insert(0, str(ROOT))
import config as cfg

def load_track_pts(limit=6000):
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("SELECT pos_x, pos_z FROM frames WHERE pos_x IS NOT NULL AND pos_z IS NOT NULL ORDER BY id LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return np.asarray(rows, dtype=float)

def load_image(png_path):
    img = Image.open(png_path).convert("RGBA")
    return img, np.asarray(img)

def image_mask(a):
    mask = a[..., 3] > 128
    if mask.sum() < 100:
        gray = a[..., :3].mean(axis=2)
        mask = gray > 30
    return mask

def render(track_pts, img, iw, ih, angle_deg, flip_x, flip_y, scale_extra=1.0, tag=""):
    """把世界坐标轨迹变换到图像坐标系并画在图上。返回 PIL Image。"""
    canvas = img.copy().convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    pts = track_pts.astype(float)
    # 中心化
    c = pts.mean(axis=0)
    pts = pts - c
    # 旋转
    th = math.radians(angle_deg)
    cs, sn = math.cos(th), math.sin(th)
    R = np.array([[cs, -sn], [sn, cs]])
    pts = pts @ R.T
    # 翻转
    if flip_x: pts[:, 0] *= -1
    if flip_y: pts[:, 1] *= -1
    # 缩放：让 bbox 铺满图的 78%（留边），保持纵横比
    x0, x1 = pts[:, 0].min(), pts[:, 0].max()
    z0, z1 = pts[:, 1].min(), pts[:, 1].max()
    span_x = max(x1 - x0, 1e-6); span_z = max(z1 - z0, 1e-6)
    s = min((iw * 0.78) / span_x, (ih * 0.78) / span_z) * scale_extra
    # 平移到图像中心
    tx = iw / 2 - (x0 + x1) / 2 * s
    tz = ih / 2 - (z0 + z1) / 2 * s
    px = pts[:, 0] * s + tx
    pz = pts[:, 1] * s + tz
    # 画线
    pts2 = list(zip(px, pz))
    for i in range(len(pts2) - 1):
        d.line([pts2[i], pts2[i + 1]], fill=(80, 170, 255, 255), width=2)
    d.ellipse([px[-1]-5, pz[-1]-5, px[-1]+5, pz[-1]+5], fill=(255, 80, 80, 255))
    out = Image.alpha_composite(canvas, overlay)
    return out

def main():
    track_key = sys.argv[1] if len(sys.argv) > 1 else "zandvoort2020"
    tracks_root = cfg.find_ac_tracks_dir()
    td = None
    for d in Path(tracks_root).iterdir():
        if d.is_dir() and track_key.lower() in d.name.lower():
            td = d; break
    if not td:
        print("track dir not found"); return
    pngs = sorted(glob.glob(str(td / "ui" / "**" / "outline_cropped.png"), recursive=True)) or \
           sorted(glob.glob(str(td / "ui" / "**" / "outline.png"), recursive=True))
    pngs = [p for p in pngs if "outline_1" not in p]
    png = pngs[0]
    print("png:", png)
    img, arr = load_image(png)
    iw, ih = img.size
    pts = load_track_pts()
    print(f"pts={len(pts)} img={iw}x{ih}")

    outdir = ROOT / "tools" / "align_vis"
    outdir.mkdir(exist_ok=True)
    cands = [
        (76.8, False, False, "rot+76.8"),
        (76.8+180, False, False, "rot+256.8"),
        (76.8, True, False, "rot+76.8_flipX"),
        (76.8, False, True, "rot+76.8_flipZ"),
        (102.9, False, False, "rot+102.9"),
        (102.9+180, False, False, "rot+282.9"),
        (0, False, False, "raw"),
        (0, True, False, "flipX_raw"),
        (90, False, False, "rot+90"),
        (90, False, True, "rot+90_flipZ"),
    ]
    for ang, fx, fz, tag in cands:
        out = render(pts, img, iw, ih, ang, fx, fz, tag=tag)
        p = outdir / f"{track_key}_{tag}.png"
        out.save(p)
        print("saved", p)

if __name__ == "__main__":
    main()
