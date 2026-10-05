#!/usr/bin/env python3
"""
纠正回流工具 v2（规则模型 v4）

用户在对话里纠正分类之后，把结果写回规则库，并自动生成回归用例。

v5.1 只能追加（append）。但**误伤比漏检更常见**——"搜某公司收购的公开新闻被拦成
high"就是本项目诚实清单里记录的第一条局限。一个"边界由你定"的工具链只支持放大、
不支持收窄，是开放性的直接缺口。v2 补齐四种动作。

用法（stdin 传 JSON，避免 shell 转义）：

  # 1) 扩充：漏标的词加进对应级别
  {"user_input": "帮我看看股权对赌条款", "level": "high", "keyword": "股权对赌",
   "correction_type": "privacy_underestimate", "note": "用户指出这也算机密"}

  # 2) 收窄：把误伤的词整个删掉
  {"action": "remove", "keyword": "底价", "note": "公开行情讨论被误拦"}

  # 3) 降级：从 high 挪到 medium
  {"action": "demote", "keyword": "收购", "to": "medium", "note": "不该直接拦死"}

  # 4) 豁免：只在特定语境下不拦（不删词，其他语境照常保护）
  {"action": "exempt", "keyword": "收购", "when": ["新闻", "公告", "公开报道"],
   "demote_to": "none", "note": "公开新闻里的收购是误伤"}

动作：写回规则库（v4，锁定 LF + 末尾换行）+ 追加回归用例 + 落纠正记录。
退出码：0 成功 / 2 入参不合法 / 1 规则文件缺失
"""

import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import rules_model  # noqa: E402

BASE = os.path.join(_HERE, "..")
RULES_PATH = os.path.join(BASE, "keywords", "rules.json")
CASES_PATH = os.path.join(BASE, "keywords", "test_cases.json")
CORR_PATH = os.path.join(BASE, "data", "corrections.jsonl")

VALID_LEVELS = ("high", "medium")
ACTIONS = ("add", "remove", "demote", "exempt")


def fail(msg, code=2):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(code)


def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def rules_containing(rules, keyword):
    """返回含该词（大小写不敏感）的规则列表。"""
    kw = keyword.lower()
    out = []
    for r in rules["rules"]:
        for p in r.get("match", {}).get("patterns") or []:
            if p.lower() == kw:
                out.append(r)
    return out


def level_rule(rules, level):
    """找到承载某级别的规则；没有就建一条。"""
    for r in rules["rules"]:
        if r.get("id") == "%s-default" % level:
            return r
    for r in rules["rules"]:
        if r.get("level") == level:
            return r
    rule = {
        "id": "%s-default" % level,
        "level": level,
        "action": "block_remote" if level == "high" else "prefer_local",
        "match": {"type": "substring", "patterns": []},
        "note": "由 correct.py 自动创建",
    }
    rules["rules"].append(rule)
    return rule


def prune_dangling_scopes(rules):
    """删词/挪词之后，清掉指向已不存在模式的豁免作用域。

    不做这一步，规则文件里会留下悬空的 applies_to，lint 会直接报错。
    返回被丢弃的作用域，供调用方如实告知用户（不静默吞掉）。
    """
    known = {r["id"]: set(r.get("match", {}).get("patterns") or []) for r in rules["rules"]}
    dropped, kept = [], []
    for exc in rules.get("exceptions") or []:
        scopes = exc.get("applies_to")
        if scopes is None:
            kept.append(exc)
            continue
        new_scopes = []
        for s in scopes:
            rid = s.get("rule")
            if rid not in known:
                dropped.append({"exception": exc.get("id", ""), "rule": rid,
                                "reason": "规则已不存在"})
                continue
            if s.get("patterns") is None:
                new_scopes.append(s)
                continue
            live = [p for p in s["patterns"] if p in known[rid]]
            for p in [p for p in s["patterns"] if p not in known[rid]]:
                dropped.append({"exception": exc.get("id", ""), "rule": rid,
                                "pattern": p, "reason": "模式已被删除"})
            if live:
                new_scopes.append({"rule": rid, "patterns": live})
        if new_scopes:
            exc["applies_to"] = new_scopes
            kept.append(exc)
        else:
            dropped.append({"exception": exc.get("id", ""),
                            "reason": "作用域已全部失效，整条豁免被移除"})
    rules["exceptions"] = kept
    return dropped


def main():
    # Windows 下 stdin 默认按 ANSI 代码页解码，调用方传的是 UTF-8。
    # 按字节读再显式解码，避免乱码与 surrogate 字符导致的 UnicodeEncodeError。
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    try:
        req = json.loads(raw)
    except Exception as e:
        fail("stdin 不是合法 JSON: %s" % e)
    if not isinstance(req, dict):
        fail("stdin 必须是 JSON 对象")

    action = str(req.get("action") or "add").strip().lower()
    keyword = str(req.get("keyword") or "").strip()
    level = str(req.get("level") or "").strip().lower()
    to_level = str(req.get("to") or "").strip().lower()
    demote_to = str(req.get("demote_to") or "none").strip().lower()
    when = req.get("when") or []
    user_input = str(req.get("user_input") or "").strip()
    correction_type = req.get("correction_type", "general")
    note = req.get("note", "")

    if action not in ACTIONS:
        fail("action 必须是 %s（收到 %r）" % ("|".join(ACTIONS), action))
    if not keyword:
        fail("keyword 必填，level 必须为 high|medium（收到 level=%r）" % req.get("level"))
    if action == "add" and level not in VALID_LEVELS:
        fail("action=add 时 level 必须为 high|medium（收到 %r）" % req.get("level"))
    if action == "demote" and to_level not in VALID_LEVELS:
        fail("action=demote 时需要 to=high|medium（收到 %r）" % req.get("to"))
    if action == "exempt":
        if isinstance(when, str):
            when = [when]
        when = [str(w).strip() for w in when if str(w).strip()]
        if not when:
            fail("action=exempt 时需要 when=[语境词…]——没有触发条件的豁免永远不会生效")
        if demote_to not in rules_model.LEVELS:
            fail("demote_to 必须是 %s（收到 %r）" % ("|".join(rules_model.LEVELS), demote_to))

    if not os.path.exists(RULES_PATH):
        fail("规则文件不存在: %s" % RULES_PATH, code=1)

    rules = rules_model.normalize(load_json(RULES_PATH))
    before = json.dumps(rules, ensure_ascii=False, sort_keys=True)
    changed = []

    # ── 1) 改规则 ──────────────────────────────────────────
    if action == "add":
        rule = level_rule(rules, level)
        pats = rule["match"]["patterns"]
        if any(p.lower() == keyword.lower() for p in pats):
            changed.append({"op": "add", "rule": rule["id"], "keyword": keyword,
                            "result": "已存在，未重复添加"})
        else:
            pats.append(keyword)
            changed.append({"op": "add", "rule": rule["id"], "keyword": keyword,
                            "result": "已加入"})

    elif action in ("remove", "demote"):
        hits = rules_containing(rules, keyword)
        if not hits:
            fail("规则库里没有 %r 这个词，无需%s"
                 % (keyword, "删除" if action == "remove" else "降级"))
        for r in hits:
            r["match"]["patterns"] = [p for p in r["match"]["patterns"]
                                      if p.lower() != keyword.lower()]
            changed.append({"op": action, "from_rule": r["id"], "from_level": r["level"],
                            "keyword": keyword, "result": "已移出"})
        if action == "demote":
            target = level_rule(rules, to_level)
            if not any(p.lower() == keyword.lower() for p in target["match"]["patterns"]):
                target["match"]["patterns"].append(keyword)
            changed.append({"op": "demote", "to_rule": target["id"], "to_level": to_level,
                            "keyword": keyword, "result": "已加入"})

    elif action == "exempt":
        hits = rules_containing(rules, keyword)
        if not hits:
            fail("规则库里没有 %r 这个词；豁免只对已存在的模式有意义"
                 "（想新增请用 action=add）" % keyword)
        exc_id = str(req.get("id") or ("%s-in-%s" % (keyword, "-".join(when[:2])))).strip()
        scopes = [{"rule": r["id"], "patterns": [keyword]} for r in hits]
        existing = None
        for e in rules["exceptions"]:
            if e.get("id") == exc_id:
                existing = e
                break
        if existing is None:
            rules["exceptions"].append({
                "id": exc_id, "demote_to": demote_to, "applies_to": scopes,
                "when": {"type": "substring", "patterns": when},
                "note": note or "由 correct.py 添加",
            })
            changed.append({"op": "exempt", "exception": exc_id, "result": "已新建豁免"})
        else:
            have = dict(((s["rule"], p) for s in (existing.get("applies_to") or [])
                         for p in (s.get("patterns") or [])))
            for s in scopes:
                if (s["rule"], keyword) not in have:
                    existing.setdefault("applies_to", []).append(s)
            wpat = existing.setdefault("when", {}).setdefault("patterns", [])
            for w in when:
                if w not in wpat:
                    wpat.append(w)
            existing["demote_to"] = demote_to
            changed.append({"op": "exempt", "exception": exc_id, "result": "已并入现有豁免"})

    dropped = prune_dangling_scopes(rules) if action in ("remove", "demote") else []
    problems = [m for s, m in rules_model.lint(rules) if s == "error"]
    if problems:
        fail("改动会让规则文件不合法，已放弃写入：%s" % "; ".join(problems))

    after = json.dumps(rules, ensure_ascii=False, sort_keys=True)
    rules_written = after != before
    if rules_written:
        rules_model.dump_rules(RULES_PATH, rules)

    # ── 2) 回归用例 ────────────────────────────────────────
    # 期望级别按**改动后**的规则实算，而不是照抄入参：
    # 回归用例的职责是锁住"改完之后是什么样"，将来谁改坏了能立刻发现。
    test_case_added = False
    expected = None
    mismatch = None
    if user_input:
        expected = rules_model.evaluate(user_input, rules)["level"]
        if action == "add" and expected != level:
            mismatch = {"declared": level, "actual": expected}
        cases = load_json(CASES_PATH, []) or []
        dup = any(isinstance(c, dict) and c.get("input") == user_input
                  and c.get("expected") == expected for c in cases)
        if not dup:
            cases.append({
                "input": user_input,
                "expected": expected,
                "note": "纠正回流[%s] | %s | %s" % (action, keyword, correction_type),
            })
            rules_model.dump_rules(CASES_PATH, cases)
            test_case_added = True

    # ── 3) 纠正记录 ────────────────────────────────────────
    rec = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "user_input": user_input,
        "keyword": keyword,
        "level": level or None,
        "to": to_level or None,
        "when": when or None,
        "demote_to": demote_to if action == "exempt" else None,
        "correction_type": correction_type,
        "note": note,
        "changed": changed,
        "rules_written": rules_written,
        "test_case_added": test_case_added,
        "expected_level": expected,
    }
    try:
        os.makedirs(os.path.dirname(CORR_PATH), exist_ok=True)
        with open(CORR_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[correct] 纠正记录写入失败（不影响规则库改动）: %s" % e, file=sys.stderr)

    out = {"ok": True, "action": action, "changed": changed,
           "rules_written": rules_written,
           "test_case_added": test_case_added, "expected_level": expected}
    if mismatch:
        out["warning"] = ("按改动后的规则实算，「%s」的级别是 %s，与你声明的 %s 不一致——"
                          "可能有别的词也在其中起作用，建议核对"
                          % (user_input, mismatch["actual"], mismatch["declared"]))
    if dropped:
        out["dropped_scopes"] = dropped
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
