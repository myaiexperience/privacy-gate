#!/usr/bin/env python3
"""
纠正回流工具 v1

用户纠正路由分类后，把漏标关键词写入规则表，并自动追加回归用例。
规则表 / 测试用例 / 纠正记录的单一来源与 rules_engine.py 相同。

用法（stdin 传 JSON，避免 shell 转义）：
  python tools/correct.py <<'EOF'
  {"user_input": "帮我写一份股权分配方案", "level": "high", "keyword": "股权",
   "correction_type": "privacy_underestimate", "note": "用户指出股权是商业机密"}
  EOF

动作：
  1. keyword 不在 privacy_{level}.keywords 时 → 追加（大小写不敏感去重）
  2. 追加回归用例到 keywords/test_cases.json（同输入+期望级别去重）
  3. 追加完整纠正记录到 data/corrections.jsonl
"""

import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RULES_PATH = os.path.join(BASE, "keywords", "rules.json")
CASES_PATH = os.path.join(BASE, "keywords", "test_cases.json")
CORR_PATH = os.path.join(BASE, "data", "corrections.jsonl")

VALID_LEVELS = ("high", "medium")


def load_json(path: str):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def dump_json(path: str, data) -> None:
    """写回 JSON：锁定 LF + 末尾换行。

    json.dump 本身不写末尾换行，而 Windows 文本模式会把 \\n 转成 \\r\\n。
    两者叠加的结果是：用户在 Windows 上做一次纠正，规则库整个文件都变成
    "已修改"，从 diff 里根本看不出真正加了哪个词。这里把行尾定死成 LF，
    并补上末尾换行，让改动始终是"一行一个词"。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    # Windows 下 stdin 默认按 ANSI 代码页（如 cp936）解码，调用方（opencode 插件
    # / bash heredoc）传的是 UTF-8；直接按字节读再显式 UTF-8 解码，避免乱码和
    # surrogate 字符导致的 UnicodeEncodeError。
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    try:
        req = json.loads(raw)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"stdin 不是合法 JSON: {e}"}, ensure_ascii=False))
        sys.exit(2)

    keyword = (req.get("keyword") or "").strip()
    level = (req.get("level") or "").strip()
    user_input = (req.get("user_input") or "").strip()
    correction_type = req.get("correction_type", "general")
    note = req.get("note", "")

    if not keyword or level not in VALID_LEVELS:
        print(json.dumps({
            "ok": False,
            "error": f"keyword 必填，level 必须为 high|medium（收到 level={level!r}）",
        }, ensure_ascii=False))
        sys.exit(2)

    rules = load_json(RULES_PATH)
    if rules is None:
        print(json.dumps({"ok": False, "error": f"规则文件不存在: {RULES_PATH}"}, ensure_ascii=False))
        sys.exit(1)

    # 1. 关键词入库（大小写不敏感去重）
    section = "privacy_" + level
    kws = rules.setdefault(section, {}).setdefault("keywords", [])
    kw_lower = keyword.lower()
    added = not any(k.lower() == kw_lower for k in kws)
    if added:
        kws.append(keyword)
        dump_json(RULES_PATH, rules)

    # 2. 回归用例入库
    cases = load_json(CASES_PATH)
    if cases is None:
        cases = []
    dup = any(
        isinstance(c, dict) and c.get("input") == user_input and c.get("expected") == level
        for c in cases
    )
    test_case_added = False
    if user_input and not dup:
        cases.append({
            "input": user_input,
            "expected": level,
            "note": f"纠正回流 | {keyword} → {level} | {correction_type}",
        })
        dump_json(CASES_PATH, cases)
        test_case_added = True

    # 3. 纠正记录落盘
    os.makedirs(os.path.dirname(CORR_PATH), exist_ok=True)
    rec = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_input": user_input,
        "keyword": keyword,
        "level": level,
        "correction_type": correction_type,
        "note": note,
        "keyword_added": added,
        "test_case_added": test_case_added,
    }
    with open(CORR_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(json.dumps({"ok": True, **rec}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
