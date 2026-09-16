# -*- coding: utf-8 -*-
"""从 config.py 的 CHANGELOG 生成 CHANGELOG.md。

单一数据源是 config.py（GUI 点右上角版本号显示的也是它），
本脚本只负责把它导出成仓库里的 CHANGELOG.md，避免两处手写不一致。

用法:
    python tools/gen_changelog.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as cfg  # noqa: E402

# 已知的版本发布日期（未知的留空，不猜）
RELEASE_DATES = {
    "v2.0": "2026-09-16",
}


def ver_key(v: str) -> tuple:
    """把 'v2.0' 变成 (2, 0) 便于版本排序（而不是字符串排序）。"""
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def main() -> int:
    lines = [
        "# 更新日志",
        "",
        "> 本文件由 [`config.py`](config.py) 的 `CHANGELOG` 生成——仪表盘里点右上角版本号"
        "看到的就是同一份内容。",
        "> 新增条目请改 `config.py`，然后运行 `python tools/gen_changelog.py` 重新生成本文件。",
        "",
    ]
    for ver in sorted(cfg.CHANGELOG.keys(), key=ver_key, reverse=True):
        date = RELEASE_DATES.get(ver)
        lines.append(f"## {ver}" + (f" ({date})" if date else ""))
        lines.append("")
        for item in cfg.CHANGELOG[ver]:
            lines.append(f"- {item}")
        lines.append("")

    out = ROOT / "CHANGELOG.md"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"已生成 {out}（{len(cfg.CHANGELOG)} 个版本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
