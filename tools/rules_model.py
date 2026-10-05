#!/usr/bin/env python3
"""
规则模型 v4 —— 策略层契约

为什么要有这个模块
------------------
v5.1 的 keywords/rules.json 看着像契约，其实**只有 keywords 被引擎读**：
对全仓库 .py 检索 action|description|_schema|_note → 零匹配。
后果是使用者要写自己的边界，得先读引擎源码才知道什么有效——这跟"开放"是相反的。
开放的第一步是契约可见。

v4 把契约显式化
--------------
  * 匹配原语可配：substring / word / regex
      - substring：ASCII 字母词自动加词边界（延续 v5.1 的防误伤，见 test_routes 里
        "veranda 不得命中 NDA" 的用例）；中文照旧子串匹配
      - word：强制词边界
      - regex：交给正则，按原文本 + IGNORECASE 匹配
  * action 真正生效：block_remote / prefer_local / allow
  * exceptions + when：能表达"这个词在这个语境里别拦"（v5.1 完全做不到）
      - applies_to 省略          → 作用于全部规则（慎用）
      - applies_to: ["rule-a"]   → 作用于 rule-a 的全部模式
      - applies_to: [{"rule": "rule-a", "patterns": ["收购"]}]
                                 → 只作用于 rule-a 里的某几个模式

      **为什么必须有模式级豁免**：规则的粒度就是豁免的粒度。如果只支持规则级，
      那么给"收购"配一条"公开新闻"豁免，会让"帮我写一份**保密**协议，顺便看看**新闻**"
      里的"保密"也一起失效——整条规则被豁免掉了。这是安全漏洞，不是小瑕疵。
      需要精确豁免的词，要么单独成一条规则，要么用上面带 patterns 的写法。
  * id：让决策日志能指向具体规则，统计才做得出来

v3 兼容
-------
读时自动转换（privacy_high / privacy_medium → rules），不强迫老用户改文件。

零第三方依赖，Python 3.9+。
"""

import json
import os
import re

SCHEMA = "v4"
LEVELS = ("none", "medium", "high")
RANK = {"none": 0, "medium": 1, "high": 2}
MATCH_TYPES = ("substring", "word", "regex")

# 话题切换信号：检测到就重置多轮继承（视为开启新话题）
DEFAULT_TOPIC_SHIFT_KEYWORDS = [
    "换个话题", "不谈这个了", "另外开一个", "新的话题", "不聊这个了",
    "下一个任务", "新任务", "说点别的", "换一个主题",
]

# 远程工具名匹配模式（网关剥夺远程能力时用；适配器也可复用）
DEFAULT_REMOTE_TOOL_PATTERNS = [
    "*web*", "*fetch*", "*browse*", "*search*", "*crawl*", "*http*",
]

# v3 顶层键 → v4 规则
_V3_SECTIONS = (("privacy_high", "high"), ("privacy_medium", "medium"))


class RuleError(ValueError):
    """规则文件不合法（lint 抓到 error 级问题时抛）。"""


# ── 匹配原语 ───────────────────────────────────────────────

def _patterns_of(match):
    """把 match 归一成字符串列表（兼容 pattern / patterns 两种写法）。"""
    if not isinstance(match, dict):
        return []
    pats = match.get("patterns")
    if pats is None:
        single = match.get("pattern")
        pats = [] if single is None else [single]
    if isinstance(pats, (str, bytes)):
        pats = [pats]
    out = []
    for p in pats or []:
        p = "" if p is None else str(p)
        if p.strip():
            out.append(p)
    return out


def _needs_word_boundary(term):
    """ASCII 且含字母 → 加词边界。

    这是 v5.1 用实测换来的行为：不做词边界时 "veranda" 会命中 NDA。
    """
    return term.isascii() and any(c.isalpha() for c in term)


def match_hits(match, text, lowered=None):
    """返回命中的模式列表（用于分级、日志与 explain）。

    text    原始文本（regex 按它匹配）
    lowered 小写文本（substring / word 用；缺省时自行小写）
    """
    if lowered is None:
        lowered = (text or "").lower()
    mtype = (match or {}).get("type", "substring")
    hits = []
    for pat in _patterns_of(match):
        if mtype == "regex":
            try:
                if re.search(pat, text or "", re.IGNORECASE):
                    hits.append(pat)
            except re.error:
                # 非法正则由 lint 报错；运行期静默跳过，不让它拖垮分级
                continue
            continue
        if mtype == "word" or (mtype == "substring" and _needs_word_boundary(pat)):
            rx = r"(?<![a-z0-9])" + re.escape(pat.lower()) + r"(?![a-z0-9])"
            if re.search(rx, lowered):
                hits.append(pat)
        else:
            if pat.lower() in lowered:
                hits.append(pat)
    return hits


# ── v3 → v4 转换 ───────────────────────────────────────────

def is_v4(raw):
    if not isinstance(raw, dict):
        return False
    if str(raw.get("schema") or "").lower() == SCHEMA:
        return True
    # 有 rules 列表但没有 v3 的 privacy_* 分区 → 当作 v4
    return "rules" in raw and not any(k in raw for k, _ in _V3_SECTIONS)


def convert_v3(raw):
    """把 v3（privacy_high / privacy_medium）就地转成 v4 结构。"""
    rules = []
    for section, level in _V3_SECTIONS:
        block = raw.get(section) or {}
        kws = [str(k) for k in (block.get("keywords") or []) if str(k).strip()]
        if not kws:
            continue
        rules.append({
            "id": "%s-default" % level,
            "level": level,
            "action": block.get("action") or ("block_remote" if level == "high" else "prefer_local"),
            "match": {"type": "substring", "patterns": kws},
            "note": "由 v3 的 %s 自动转换" % section,
        })
    return {
        "schema": SCHEMA,
        "rules": rules,
        "exceptions": [],
        "topic_shift_keywords": list(DEFAULT_TOPIC_SHIFT_KEYWORDS),
        "remote_tool_patterns": list(DEFAULT_REMOTE_TOOL_PATTERNS),
    }


def _normalize_applies(applies):
    """把 applies_to 归一成 [{"rule": id, "patterns": [...]|None}]。

    接受三种写法：
      None / 省略                       → None（作用于全部规则）
      "rule-a" / ["rule-a"]            → 该规则的全部模式
      [{"rule":"rule-a","patterns":["收购"]}] → 只作用于这几个模式
    """
    if applies is None:
        return None
    if isinstance(applies, (str, dict)):
        applies = [applies]
    out = []
    for entry in applies or []:
        if isinstance(entry, str):
            if entry.strip():
                out.append({"rule": entry, "patterns": None})
            continue
        if isinstance(entry, dict):
            rid = str(entry.get("rule") or entry.get("rule_id") or "").strip()
            pats = entry.get("patterns", entry.get("pattern"))
            if isinstance(pats, str):
                pats = [pats]
            pats = [str(p) for p in pats if str(p).strip()] if pats else None
            if rid:
                out.append({"rule": rid, "patterns": pats})
    return out or None


def normalize(raw):
    """任意版本 → 归一化 v4 结构（不改动传入对象）。"""
    if not isinstance(raw, dict):
        raise RuleError("规则文件顶层必须是对象")
    if not is_v4(raw):
        return convert_v3(raw)

    rules = []
    for item in raw.get("rules") or []:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "medium").lower()
        match = item.get("match") or {}
        rules.append({
            "id": str(item.get("id") or ""),
            "level": level if level in LEVELS else "medium",
            "action": str(item.get("action") or ""),
            "match": {
                "type": str(match.get("type") or "substring").lower(),
                "patterns": _patterns_of(match),
            },
            "note": str(item.get("note") or ""),
        })

    exceptions = []
    for item in raw.get("exceptions") or []:
        if not isinstance(item, dict):
            continue
        exceptions.append({
            "id": str(item.get("id") or ""),
            "demote_to": str(item.get("demote_to") or "none").lower(),
            "applies_to": _normalize_applies(item.get("applies_to")),
            "when": {
                "type": str((item.get("when") or {}).get("type") or "substring").lower(),
                "patterns": _patterns_of(item.get("when") or {}),
            },
            "note": str(item.get("note") or ""),
        })

    ts = raw.get("topic_shift_keywords")
    tp = raw.get("remote_tool_patterns")
    return {
        "schema": SCHEMA,
        "rules": rules,
        "exceptions": exceptions,
        "topic_shift_keywords": [str(x) for x in ts] if ts else list(DEFAULT_TOPIC_SHIFT_KEYWORDS),
        "remote_tool_patterns": [str(x) for x in tp] if tp else list(DEFAULT_REMOTE_TOOL_PATTERNS),
    }


def load_rules(path):
    """读规则文件（v3 / v4 自动识别）→ 归一化 v4 结构。"""
    with open(path, "r", encoding="utf-8") as f:
        return normalize(json.load(f))


def dump_rules(path, rules):
    """写回规则文件：锁定 LF + 末尾换行。

    与 correct.py 的写盘约定一致：json.dump 不写末尾换行，Windows 文本模式
    又会把 \\n 转成 \\r\\n，两者叠加会让一次改动表现为整文件重写，
    而规则库恰恰是最需要靠 diff 审阅的东西（见 DECISIONS.md D13）。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ── 分级 ──────────────────────────────────────────────────

def evaluate(text, rules):
    """按 v4 规则分级。

    豁免是**模式级**的：一条豁免只掐掉它点名的 (规则, 模式) 组合，同一条规则里的
    其他模式照常生效。否则"帮我写一份保密协议，顺便看看新闻"会因为命中"新闻"
    把整条规则豁免掉，"保密"也跟着失效——那是安全漏洞。

    返回：
      level             none | medium | high
      matched_keywords  命中且**未被豁免**的词（兼容 v5.1 的字段名）
      contributions     每条规则的贡献（含被豁免的词与 exempted_by，供 explain 用）
      exemptions        实际生效的豁免
    """
    text = text or ""
    lowered = text.lower()
    rules = rules or {}

    contributions = []
    for rule in rules.get("rules") or []:
        hits = match_hits(rule.get("match") or {}, text, lowered)
        if hits:
            contributions.append({
                "rule_id": rule.get("id", ""),
                "level": rule.get("level", "medium"),
                "action": rule.get("action", ""),
                "terms": hits,
            })

    exempted_pairs = set()   # (rule_id, term)
    demote_of_rule = {}      # rule_id -> demote_to（整条规则被豁免干净时生效）
    exemptions = []
    for exc in rules.get("exceptions") or []:
        if not match_hits(exc.get("when") or {}, text, lowered):
            continue
        applies = exc.get("applies_to")
        pairs = set()
        for c in contributions:
            scopes = applies if applies is not None else [{"rule": c["rule_id"], "patterns": None}]
            for scope in scopes:
                if scope.get("rule") != c["rule_id"]:
                    continue
                picked = c["terms"] if scope.get("patterns") is None \
                    else [t for t in c["terms"] if t in scope["patterns"]]
                for t in picked:
                    pairs.add((c["rule_id"], t))
        if not pairs:
            continue
        demote = exc.get("demote_to", "none")
        if demote not in LEVELS:
            demote = "none"
        for rid, _ in pairs:
            demote_of_rule[rid] = demote
        exempted_pairs |= pairs
        exemptions.append({
            "id": exc.get("id", ""),
            "demote_to": demote,
            "rule_ids": sorted({rid for rid, _ in pairs}),
            "terms": sorted({t for _, t in pairs}),
        })

    level = "none"
    matched = []
    for c in contributions:
        active = [t for t in c["terms"] if (c["rule_id"], t) not in exempted_pairs]
        c["active_terms"] = active
        c["exempted_terms"] = [t for t in c["terms"] if (c["rule_id"], t) in exempted_pairs]
        if active:
            if RANK.get(c["level"], 0) > RANK[level]:
                level = c["level"]
            for t in active:
                if t not in matched:
                    matched.append(t)
        else:
            # 整条规则被豁免干净 → 按 demote_to 贡献（默认 none，等于不贡献）
            d = demote_of_rule.get(c["rule_id"], "none")
            if RANK.get(d, 0) > RANK[level]:
                level = d

    return {
        "level": level,
        "matched_keywords": matched,
        "contributions": contributions,
        "exemptions": exemptions,
    }


# ── 策略体检 ──────────────────────────────────────────────

_TOP_LEVEL_KEYS = {"schema", "rules", "exceptions",
                   "topic_shift_keywords", "remote_tool_patterns"}
_RULE_KEYS = {"id", "level", "action", "match", "note"}
_MATCH_KEYS = {"type", "pattern", "patterns"}
_EXC_KEYS = {"id", "demote_to", "applies_to", "when", "note"}


def _lint_match(match, where, problems):
    if not isinstance(match, dict):
        problems.append(("error", "%s 的 match 必须是对象" % where))
        return
    for k in match:
        if k not in _MATCH_KEYS:
            problems.append(("error", "%s 的 match 含未知字段 %r（可用：%s）"
                             % (where, k, "、".join(sorted(_MATCH_KEYS)))))
    mtype = str(match.get("type") or "substring").lower()
    if mtype not in MATCH_TYPES:
        problems.append(("error", "%s 的 match.type=%r 非法（可用：%s）"
                         % (where, mtype, "、".join(MATCH_TYPES))))
    pats = _patterns_of(match)
    if not pats:
        problems.append(("error", "%s 的 match 没有 pattern/patterns" % where))
    if mtype == "regex":
        for p in pats:
            try:
                re.compile(p)
            except re.error as e:
                problems.append(("error", "%s 的正则 %r 无法编译: %s" % (where, p, e)))


def lint(raw):
    """静态检查规则文件，返回 [(severity, message)]，severity ∈ error|warn。"""
    problems = []
    if not isinstance(raw, dict):
        return [("error", "规则文件顶层必须是对象")]

    is_v3 = any(k in raw for k, _ in _V3_SECTIONS) and not is_v4(raw)
    if is_v3:
        problems.append(("warn", "仍是 v3 格式（privacy_high/privacy_medium）。"
                                 "读时能自动转换，但写回会升级为 v4——届时会产生一次大 diff"))
        for section, _ in _V3_SECTIONS:
            if section not in raw:
                problems.append(("warn", "v3 缺少分区 %s" % section))
        return problems

    try:
        rules = normalize(raw)
    except RuleError as e:
        return [("error", str(e))]

    for k in raw:
        if k not in _TOP_LEVEL_KEYS:
            problems.append(("error", "顶层含未知字段 %r（可用：%s）"
                             % (k, "、".join(sorted(_TOP_LEVEL_KEYS)))))

    seen_ids = set()
    for i, item in enumerate(raw.get("rules") or []):
        where = "rules[%d]" % i
        if not isinstance(item, dict):
            problems.append(("error", "%s 必须是对象" % where))
            continue
        rid = str(item.get("id") or "")
        if not rid:
            problems.append(("error", "%s 缺 id（决策日志要靠它指向具体规则）" % where))
        elif rid in seen_ids:
            problems.append(("error", "%s 的 id=%r 重复" % (where, rid)))
        else:
            seen_ids.add(rid)
            where = "rules[%s]" % rid
        for k in item:
            if k not in _RULE_KEYS:
                problems.append(("error", "%s 含未知字段 %r（可用：%s）"
                                 % (where, k, "、".join(sorted(_RULE_KEYS)))))
        lvl = str(item.get("level") or "").lower()
        if lvl not in ("medium", "high"):
            problems.append(("error", "%s 的 level=%r 非法（规则只能是 medium 或 high；"
                                      "'none' 请用 exceptions 表达）" % (where, item.get("level"))))
        act = str(item.get("action") or "")
        if act not in ("block_remote", "prefer_local", "allow", ""):
            problems.append(("warn", "%s 的 action=%r 不是已知动作（引擎目前不据此分支，"
                                     "仅作声明）" % (where, act)))
        _lint_match(item.get("match") or {}, where, problems)

    exc_ids = set()
    for i, item in enumerate(raw.get("exceptions") or []):
        where = "exceptions[%d]" % i
        if not isinstance(item, dict):
            problems.append(("error", "%s 必须是对象" % where))
            continue
        eid = str(item.get("id") or "")
        if not eid:
            problems.append(("warn", "%s 缺 id（不影响功能，但日志里看不出来是哪条豁免生效）" % where))
        elif eid in exc_ids:
            problems.append(("error", "%s 的 id=%r 重复" % (where, eid)))
        else:
            exc_ids.add(eid)
            where = "exceptions[%s]" % eid
        for k in item:
            if k not in _EXC_KEYS:
                problems.append(("error", "%s 含未知字段 %r（可用：%s）"
                                 % (where, k, "、".join(sorted(_EXC_KEYS)))))
        demote = str(item.get("demote_to") or "none").lower()
        if demote not in LEVELS:
            problems.append(("error", "%s 的 demote_to=%r 非法（可用：%s）"
                             % (where, demote, "、".join(LEVELS))))
        scopes = _normalize_applies(item.get("applies_to"))
        if item.get("applies_to") is not None:
            if not scopes:
                problems.append(("error", "%s 的 applies_to 是空列表——它永远不会生效，"
                                          "要去掉这个字段表示作用于全部规则" % where))
            by_id = {r["id"]: r for r in rules["rules"]}
            for scope in scopes or []:
                rid = scope["rule"]
                if rid not in seen_ids:
                    problems.append(("error", "%s 的 applies_to 指向不存在的规则 id=%r"
                                     % (where, rid)))
                    continue
                if scope.get("patterns"):
                    known = set((by_id.get(rid) or {}).get("match", {}).get("patterns") or [])
                    for p in scope["patterns"]:
                        if p not in known:
                            problems.append(("error",
                                             "%s 的 applies_to 点名了规则 %r 里不存在的模式 %r"
                                             "（写错模式名不会报错，只会静默不生效）"
                                             % (where, rid, p)))
        if not item.get("when"):
            problems.append(("error", "%s 缺 when——没有触发条件的豁免永远不会生效" % where))
        else:
            _lint_match(item.get("when") or {}, where + ".when", problems)

    if not rules["rules"]:
        problems.append(("error", "没有任何规则——这等于把门全开"))

    for name, default in (("topic_shift_keywords", DEFAULT_TOPIC_SHIFT_KEYWORDS),
                          ("remote_tool_patterns", DEFAULT_REMOTE_TOOL_PATTERNS)):
        val = raw.get(name)
        if val is None:
            problems.append(("warn", "未定义 %s，将退回内置默认值（%d 项）" % (name, len(default))))
        elif not isinstance(val, list) or not val:
            problems.append(("error", "%s 必须是非空列表" % name))

    return problems


def lint_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return lint(json.load(f))
