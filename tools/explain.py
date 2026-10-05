#!/usr/bin/env python3
"""
规则解释器 —— 策略层可观测（v4）

给一段文本，逐条说明**为什么**是这个级别。

为什么它是必需品而不是配套
-------------------------
出厂的粗策略，作者知道它的脾气；一旦边界交给使用者，工具就必须让"这条策略实际
会怎么判"变得可见——否则你开放出去的是一把不知道打哪的枪。使用者写了 200 条规则，
必须能问：这段话为什么被判 high？我配的那条豁免为什么没生效？

它回答三类问题：
  1. 哪条规则命中了、命中了哪个模式
  2. 哪条豁免生效了、掐掉了什么、**为什么同一条规则里的其他模式没被牵连**
  3. 哪条豁免"触发了但没有可豁免的命中"（配错了但不会报错的典型情况）

用法：
  python tools/explain.py "帮我写一份保密协议"
  python tools/explain.py --stdin <<'EOF'
  ...
  EOF
  python tools/explain.py --prev-level high "那第三条呢"     # 带上多轮继承
  python tools/explain.py --rules 候选规则.json "试一下"      # 先试候选规则再决定要不要装
  python tools/explain.py --json "..."                       # 给脚本用

隐私说明：默认**不回显输入原文**，只显示字数。
本工具是给隐私门禁做诊断的，它自己不该把待判定的文本留在终端回滚里。
要回显加 --echo。

v6 会把它包成 `privacy-gate explain`。
"""

import argparse
import json
import os
import sys

try:
    from . import paths
except ImportError:  # 脚本模式
    import paths

rules_model = paths.sibling("rules_model")
rules_engine = paths.sibling("rules_engine")

DEFAULT_RULES = paths.rules_path()


def explain(text, rules, prev_level=None):
    """返回结构化解释。"""
    text = text or ""
    lowered = text.lower()

    # 逐规则：命中的记下模式，未命中的也列出来（"为什么没命中"同样重要）
    rule_rows = []
    for rule in rules.get("rules") or []:
        hits = rules_model.match_hits(rule.get("match") or {}, text, lowered)
        rule_rows.append({
            "id": rule.get("id", ""),
            "level": rule.get("level", ""),
            "action": rule.get("action", ""),
            "type": (rule.get("match") or {}).get("type", ""),
            "patterns": len((rule.get("match") or {}).get("patterns") or []),
            "hit": bool(hits),
            "terms": hits,
        })

    hit_terms = {r["id"]: list(r["terms"]) for r in rule_rows if r["hit"]}

    # 逐豁免：触发条件是否命中、作用域里有没有可掐的东西、实际掐掉了什么
    exc_rows = []
    for exc in rules.get("exceptions") or []:
        when_hits = rules_model.match_hits(exc.get("when") or {}, text, lowered)
        scopes = exc.get("applies_to")
        effective_scopes = scopes if scopes is not None else [
            {"rule": rid, "patterns": None} for rid in hit_terms]
        suppressed = []
        for s in effective_scopes:
            terms = hit_terms.get(s.get("rule"))
            if not terms:
                continue
            picked = terms if s.get("patterns") is None \
                else [t for t in terms if t in s["patterns"]]
            if picked:
                suppressed.append({"rule": s["rule"], "terms": sorted(picked)})
        exc_rows.append({
            "id": exc.get("id", ""),
            "demote_to": exc.get("demote_to", "none"),
            "when": (exc.get("when") or {}).get("patterns") or [],
            "when_hits": when_hits,
            "applies_to": scopes,
            "suppressed": suppressed,
            "effective": bool(when_hits) and bool(suppressed),
            "triggered_noop": bool(when_hits) and not suppressed,
        })

    result = rules_model.evaluate(text, rules)
    out = {
        "input_length": len(text),
        "schema": rules.get("schema"),
        "level": result["level"],
        "matched_keywords": result["matched_keywords"],
        "rules": rule_rows,
        "exceptions": exc_rows,
        "inheritance": None,
    }

    if prev_level:
        effective, inherited, shift = rules_engine.apply_inheritance(
            result["level"], prev_level, text, rules.get("topic_shift_keywords"))
        out["inheritance"] = {
            "prev_level": prev_level,
            "effective_level": effective,
            "inherited": inherited,
            "topic_shift": shift,
        }

    # 最终级别由谁贡献的
    contributors = []
    for c in result["contributions"]:
        if c.get("active_terms"):
            contributors.append({"rule_id": c["rule_id"], "level": c["level"],
                                 "action": c.get("action", ""),
                                 "terms": c["active_terms"]})
    out["contributors"] = contributors
    return out


def render(exp, echo=False, text="", rules_path=""):
    lines = []
    add = lines.append
    add("规则解释")
    add("=" * 62)
    add("规则文件：%s" % rules_path)
    add("schema：%s    输入长度：%d 字" % (exp["schema"], exp["input_length"]))
    if echo:
        add("输入原文：%s" % text)
    add("")
    add("规则判定")
    for r in exp["rules"]:
        mark = "命中  " if r["hit"] else "未命中"
        detail = "、".join(r["terms"]) if r["hit"] else "—"
        add("  [%s] %-20s %-7s %-14s %s"
            % (mark, r["id"] or "(无 id)", r["level"], r["action"] or "-", detail))
    if not exp["rules"]:
        add("  （规则表为空）")
    add("")

    add("豁免判定")
    if not exp["exceptions"]:
        add("  （这份规则表没有配置豁免）")
    for e in exp["exceptions"]:
        if e["effective"]:
            state = "生效"
        elif e["triggered_noop"]:
            state = "空转"
        elif e["when_hits"]:
            state = "触发但无可豁免命中"
        else:
            state = "未触发"
        add("  [%s] %s → demote_to=%s" % (state, e["id"] or "(无 id)", e["demote_to"]))
        add("           触发条件：%s" % ("、".join(e["when"]) or "—"))
        if e["when_hits"]:
            add("           条件命中：%s" % "、".join(e["when_hits"]))
        for s in e["suppressed"]:
            add("           掐掉：%s 的 %s"
                % (s["rule"], "、".join("「%s」" % t for t in s["terms"])))
        if e["triggered_noop"]:
            add("           ⚠ 条件命中了，但作用域里没有可豁免的模式——"
                "多半是 applies_to 写错了规则的 id 或模式名")
    add("")

    add("最终级别：%s" % exp["level"])
    for c in exp["contributors"]:
        add("  由 %s 贡献（%s / %s）：%s"
            % (c["rule_id"], c["level"], c["action"] or "-",
               "、".join(c["terms"])))
    if not exp["contributors"]:
        add("  没有任何规则命中")
    inh = exp.get("inheritance")
    if inh:
        add("")
        add("多轮继承：上一轮 %s → 生效 %s%s"
            % (inh["prev_level"], inh["effective_level"],
               "（继承未降级）" if inh["inherited"] else
               "（话题切换，已重置）" if inh["topic_shift"] else ""))
    return "\n".join(lines)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="解释某个输入为什么被判成这个级别")
    ap.add_argument("text", nargs="?", default=None, help="要解释的文本")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读文本（推荐，避免转义）")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="规则文件路径")
    ap.add_argument("--prev-level", default=None, choices=["none", "medium", "high"],
                    help="模拟多轮对话：上一轮的生效级别")
    ap.add_argument("--echo", action="store_true", help="回显输入原文（默认不回显，见文件头说明）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是人读文本")
    args = ap.parse_args()

    if args.stdin:
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    elif args.text is not None:
        text = args.text
    else:
        ap.error("请给出文本，或用 --stdin 从标准输入读")

    if not os.path.exists(args.rules):
        print("规则文件不存在: %s" % args.rules, file=sys.stderr)
        return 1
    rules = rules_model.load_rules(args.rules)
    exp = explain(text, rules, args.prev_level)

    if args.json:
        payload = dict(exp)
        if args.echo:
            payload["input"] = text
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(exp, echo=args.echo, text=text, rules_path=args.rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
