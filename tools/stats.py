#!/usr/bin/env python3
"""
决策日志统计 —— 策略层可观测（v4）

从 data/routing_log.jsonl 出报告：级别分布、多轮继承生效次数、最吵的规则与关键词。

为什么它在这个版本里是必需品而不是配套
------------------------------------
v6 的默认策略会把敏感会话**无声重路由**到本地模型。这带来一个真实代价：
v5.1 里误命中只是"拦一下，你看得见"；重路由之后，误命中会把请求悄悄丢给更弱的
本地模型，用户拿到更差的答案却不知道发生了什么。**等于把门禁的有效性从"召回"
转移到"精确"上，而精确恰恰是关键词法最弱的一环。**

所以必须有一个地方能回答："到底哪条规则最吵？" 这份报告就是那个地方。

隐私：本工具**只输出聚合量**，绝不打印 user_input。
日志文件里含用户原文，那是给本机排查用的，不是给报告用的。

用法：
  python tools/stats.py
  python tools/stats.py --log 别的日志.jsonl --top 20
  python tools/stats.py --json

v6 会把它包成 `privacy-gate stats`。
"""

import argparse
import json
import os
import sys
from collections import Counter

try:
    from . import paths
except ImportError:  # 脚本模式
    import paths

DEFAULT_LOG = (os.environ.get("PRIVACY_GATE_LOG")
               or os.path.join(paths.data_dir(), "routing_log.jsonl"))
LEVELS = ("none", "medium", "high")


def read_log(path):
    """读 jsonl，坏行跳过并计数（日志是追加写的，被截断很正常）。"""
    entries, bad = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                bad += 1
                continue
            if isinstance(obj, dict):
                entries.append(obj)
            else:
                bad += 1
    return entries, bad


def _bar(count, total, width=24):
    if not total:
        return ""
    filled = int(round(width * count / total))
    return "█" * filled + "·" * (width - filled)


def summarize(entries, top=10):
    total = len(entries)
    levels = Counter()
    effective = Counter()
    sources = Counter()
    rules_hit = Counter()
    rules_exempt = Counter()
    keywords = Counter()
    exemption_ids = Counter()
    sessions = set()
    inherited = shifted = 0
    with_rule_trace = 0
    stamps = []

    for e in entries:
        lv = e.get("level")
        if lv in LEVELS:
            levels[lv] += 1
        ev = e.get("effective_level")
        if ev in LEVELS:
            effective[ev] += 1
        sources[str(e.get("source") or "unknown")] += 1
        sid = e.get("session_id")
        if sid:
            sessions.add(str(sid))
        if e.get("inherited"):
            inherited += 1
        if e.get("topic_shift"):
            shifted += 1
        if e.get("timestamp"):
            stamps.append(str(e["timestamp"]))
        for k in e.get("matched_keywords") or []:
            keywords[str(k)] += 1

        mr = e.get("matched_rules")
        if isinstance(mr, list) and mr:
            with_rule_trace += 1
            for item in mr:
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id") or "(无 id)")
                rules_hit[rid] += 1
                if item.get("exempted_by"):
                    rules_exempt[rid] += 1
        for eid in e.get("exemptions") or []:
            exemption_ids[str(eid)] += 1

    return {
        "total": total,
        "sessions": len(sessions),
        "sources": dict(sources),
        "levels": {k: levels.get(k, 0) for k in LEVELS},
        "effective_levels": {k: effective.get(k, 0) for k in LEVELS},
        "inherited": inherited,
        "topic_shift": shifted,
        "with_rule_trace": with_rule_trace,
        "rules_hit": rules_hit.most_common(top),
        "rules_exempted": rules_exempt.most_common(top),
        "keywords": keywords.most_common(top),
        "exemptions": exemption_ids.most_common(top),
        "first_ts": min(stamps) if stamps else None,
        "last_ts": max(stamps) if stamps else None,
    }


def render(summary, log_path, bad_lines=0):
    L = []
    add = L.append
    total = summary["total"]
    add("决策日志统计")
    add("=" * 62)
    add("日志：%s" % log_path)
    if not total:
        add("")
        add("（没有可用记录。日志是运行时生成的：data/routing_log.jsonl）")
        if bad_lines:
            add("（另有 %d 行无法解析，已跳过）" % bad_lines)
        return "\n".join(L)

    add("时间范围：%s → %s" % (summary["first_ts"], summary["last_ts"]))
    add("总决策：%d        会话数：%d" % (total, summary["sessions"]))
    if bad_lines:
        add("（有 %d 行无法解析，已跳过）" % bad_lines)
    add("来源：" + "  ".join("%s=%d" % (k, v) for k, v in sorted(summary["sources"].items())))
    add("")

    add("本轮判定分布")
    for lv in LEVELS:
        n = summary["levels"][lv]
        add("  %-7s %5d  %s  %5.1f%%" % (lv, n, _bar(n, total), 100.0 * n / total))
    add("")

    add("多轮继承（按会话建模的收益）")
    add("  继承未降级   %5d 次  %5.1f%%" % (summary["inherited"],
                                       100.0 * summary["inherited"] / total))
    add("  话题切换重置 %5d 次  %5.1f%%" % (summary["topic_shift"],
                                       100.0 * summary["topic_shift"] / total))
    add("")

    trace = summary["with_rule_trace"]
    add("命中规则 Top %d   （规则级溯源覆盖 %d/%d 条）"
        % (len(summary["rules_hit"]), trace, total))
    if not trace:
        add("  （日志里没有规则级溯源——这些是 v3 时期写下的记录，只有关键词）")
    for rid, n in summary["rules_hit"]:
        add("  %-24s %5d  %s" % (rid, n, _bar(n, trace or 1)))
    add("")

    if summary["rules_exempted"]:
        add("被豁免掐掉的命中（豁免生效次数）")
        for rid, n in summary["rules_exempted"]:
            add("  %-24s %5d" % (rid, n))
        add("")

    if summary["exemptions"]:
        add("生效的豁免")
        for eid, n in summary["exemptions"]:
            add("  %-40s %5d" % (eid, n))
        add("")

    add("命中关键词 Top %d   ← 「最吵的词」就是治理优先级" % len(summary["keywords"]))
    for kw, n in summary["keywords"]:
        add("  %-20s %5d  %s" % (kw, n, _bar(n, total)))
    add("")
    add("说明：本报告只输出聚合量，不打印 user_input（日志里含用户原文，那是给本机排查用的）。")
    return "\n".join(L)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="统计决策日志，找出最吵的规则与关键词")
    ap.add_argument("--log", default=DEFAULT_LOG, help="日志路径（默认 data/routing_log.jsonl）")
    ap.add_argument("--top", type=int, default=10, help="每类最多列几条（默认 10）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是人读文本")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        if args.json:
            print(json.dumps({"ok": False, "error": "日志不存在: %s" % args.log},
                             ensure_ascii=False))
        else:
            print("日志不存在：%s" % args.log)
            print("（日志是运行时生成的：跑一次引擎或插件后就会出现）")
        return 1

    entries, bad = read_log(args.log)
    summary = summarize(entries, args.top)
    if args.json:
        payload = dict(summary)
        payload["log"] = args.log
        payload["bad_lines"] = bad
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(summary, args.log, bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
