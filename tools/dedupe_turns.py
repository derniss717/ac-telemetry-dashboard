"""底层重构收尾：统一赛道弯角数据（消除 TRACK_TURNS_DATA 与 _laphtml 内 TRACK_TURNS 重复）。

- 以 _laphtml 内详细版（含弯名注释，报告验证过）为准
- 替换模块级 TRACK_TURNS_DATA 内容
- _laphtml 删除局部 TRACK_TURNS + 内联 track_key 匹配，改用模块级 match_track_key + TRACK_TURNS_DATA
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "dashboard.py"
src = SRC.read_text(encoding="utf-8")

# ---- 1. 提取 _laphtml 内详细版 TRACK_TURNS（含注释行，到匹配的 \n        } 结束）----
m = re.search(r"TRACK_TURNS = \{(.*?)\n        \}", src, re.S)
if not m:
    raise SystemExit("找不到 _laphtml 内 TRACK_TURNS")
detailed = m.group(1)
# 去 8 空格缩进 → 4 空格（模块级 dict 内容缩进）
lines = []
for ln in detailed.splitlines():
    if ln.startswith("                "):   # 8 空格 + 注释/条目更深的缩进保留相对
        pass
    # 统一：条目行（以 "  " 开头即 8 空格）去 4 空格，注释行（8 空格）去 4
    if ln.startswith("        "):
        ln = ln[4:]
    lines.append(ln)
new_data = "\n".join(lines)

# ---- 2. 替换模块级 TRACK_TURNS_DATA（括号匹配定位 dict 结束）----
start = src.index("TRACK_TURNS_DATA: dict = {")
dict_start = src.index("{", start)
depth = 0
end = None
for i in range(dict_start, len(src)):
    if src[i] == "{":
        depth += 1
    elif src[i] == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
if end is None:
    raise SystemExit("模块级 TRACK_TURNS_DATA 未闭合")
module_dict = src[start:end]
new_module = "TRACK_TURNS_DATA: dict = {" + new_data + "\n}"
src = src[:start] + new_module + src[end:]

# ---- 3. 删除 _laphtml 内局部 TRACK_TURNS 定义 + 内联 track_key 匹配串 ----
# 局部定义：从 "TRACK_TURNS = {" 到其 "}"（替换后位置变了，重新定位）
m2 = re.search(r"        TRACK_TURNS = \{(.*?)\n        \}", src, re.S)
if m2:
    src = src[:m2.start()] + src[m2.end():]

# 内联匹配串：从 "# ---- 弯角分析" 注释后的 track_key 构建段
# 定位：track_key = "" 到 turns = TRACK_TURNS.get(...) 之间
m3 = re.search(r"(        track_key = \"\"\n.*?        turns = )TRACK_TURNS\.get\(track_key, \[\]\)", src, re.S)
if m3:
    head = m3.group(1)
    replacement = (
        "        track_key = match_track_key((session or {}).get(\"track\") or \"\")\n"
        "        turns = TRACK_TURNS_DATA.get(track_key, [])"
    )
    # head 里包含旧匹配串，重建
    # 简单方式：删掉旧块（track_key="" 到 turns=... 整段），插入新两行
    src = src[:m3.start()] + replacement + src[m3.end():]
else:
    print("[warn] 内联匹配串模式未匹配，手动检查")

SRC.write_text(src, encoding="utf-8")
print("OK: 已统一弯角数据（模块级=详细版，_laphtml 引用模块级）")
