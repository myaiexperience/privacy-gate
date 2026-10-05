#!/usr/bin/env python3
"""
规则路由回归测试 v6
- 验证 rules_engine.py 对各类输入的判定是否符合预期
- 内置用例 + 外部用例（keywords/test_cases.json，由 correct.py 纠正回流自动追加）
- 含多轮隐私继承用例
- 含决策日志容错用例（日志写不进去时分级必须照常成功）
- 含规则模型 v4 契约用例（匹配原语、模式级豁免、lint）
- 用法: python test_routes.py
- 每次修改 rules.json / rules_engine.py / rules_model.py / correct.py 后必须跑一遍
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
import rules_engine  # noqa: E402
import rules_model  # noqa: E402
import paths  # noqa: E402

ENGINE = rules_engine
MODEL = rules_model
RULES_PATH = os.path.join(os.path.dirname(__file__), "keywords", "rules.json")
CASES_PATH = os.path.join(os.path.dirname(__file__), "keywords", "test_cases.json")


def classify(text: str) -> str:
    rules = ENGINE.load_rules(RULES_PATH)
    result = ENGINE.check_privacy(text, rules)
    return result["level"]


def load_external_cases() -> list:
    """读取 correct.py 纠正回流自动追加的回归用例。"""
    if not os.path.exists(CASES_PATH):
        return []
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [
        (c["input"], c["expected"], c.get("note", "外部用例"))
        for c in data
        if isinstance(c, dict)
    ]


CASES = [
    # (输入, 期望级别, 说明)
    # ---- high 命中 ----
    ("帮我写一份保密协议", "high", "保密→high"),
    ("看看这份股权分配方案", "high", "股权→high"),
    ("薪酬结构调整", "high", "薪酬→high"),
    ("评估一下收购XX公司的可行性", "high", "收购→high"),
    ("内部财务报表分析", "high", "财务报表→high"),
    ("核对这些API密钥", "high", "API密钥→high"),
    ("数据库密码忘了怎么办", "high", "数据库密码→high"),
    ("这份报价单发给谁", "high", "报价单→high"),
    ("客户问我们的对外报价能不能再降", "high", "对外报价→high"),
    ("这批货的底价是多少", "high", "底价→high"),

    # ---- v4 模式级豁免（修掉诚实清单第 1 条：公开新闻里的"收购"被误拦）----
    ("看看某公司收购的公开新闻", "none", "收购+新闻 → 豁免 → none"),
    ("帮我写保密协议，顺便看看收购的新闻", "high",
     "★同时含「保密」+已豁免的「收购」+触发词「新闻」：豁免不得连带掐掉「保密」"),
    ("商业机密的保护措施", "high", "商业秘密→high"),
    ("NDA的条款有什么要求", "high", "NDA→high"),
    ("核心算法专利草案", "high", "核心算法+专利草案→high"),
    ("融资计划书帮我改一下", "high", "融资计划→high"),
    ("竞业协议条款解释", "high", "竞业协议→high"),

    # ---- medium 命中 ----
    ("帮我写封邮件给员工", "medium", "员工→medium"),
    ("明年预算怎么做", "medium", "预算→medium"),
    ("这个税务问题怎么处理", "medium", "税务→medium"),
    ("股东会材料整理", "medium", "股东→medium"),
    ("客户信息录入模板", "medium", "客户→medium"),
    ("这个月的销售数据统计", "medium", "销售数据→medium"),
    ("会议纪要帮我整理", "medium", "会议纪要→medium"),
    ("招标文件注意事项", "medium", "招标→medium"),

    # ---- none: 纯公开 ----
    ("今天天气怎么样", "none", "天气→none"),
    ("Python列表怎么用", "none", "公开编程→none"),
    ("帮我翻译一段英文", "none", "翻译→none"),
    ("Docker和K8s的区别", "none", "公开技术对比→none"),
    ("什么是RAG", "none", "公开概念→none"),

    # ---- 边界: 语义涉密但无关键词(默认保守兜底场景) ----
    ("上次老王那份文件帮我看看", "none", "语义涉密但规则未命中→none(靠默认保守)"),
    ("帮我改一下咱们这个方案", "none", "含糊引用→none(靠默认保守)"),

    # ---- 英文 ----
    ("How to write a contract", "high", "contract→high"),
    ("explain NDA", "high", "NDA 大小写→high"),

    # ---- ASCII 词边界（v4 新增）----
    ("the word veranda appears here", "none", "词边界: veranda 内的 nda 不得命中 NDA"),
    ("this brand and agenda are clear", "none", "词边界: agenda/brand 内的 nda 不得命中 NDA"),
]

# 多轮隐私继承用例（v4 新增）
# (本轮输入, 上轮级别, 期望生效级别, 说明)
INHERIT_CASES = [
    ("那第三条怎么改", "high", "high", "上一轮 high，追问无关键词 → 继承 high"),
    ("换个话题，聊聊Docker", "high", "none", "话题切换 → 重置继承"),
    ("预算怎么做", "medium", "medium", "上一轮 medium，本轮命中 medium → medium"),
    ("预算怎么做", "high", "high", "上一轮 high，本轮命中 medium → 取高"),
    ("这个月销售数据发我", "none", "medium", "prev none，本轮命中 medium → medium"),
]

PASS = 0
FAIL = 0


def check_v4_model():
    """规则模型 v4 契约测试。

    最有价值的一条是"豁免不得溢出"：模式级豁免如果按规则级实现，
    「帮我写一份保密协议，顺便看看新闻」会因为命中「新闻」把整条 high 规则
    豁免掉，「保密」跟着失效——那是安全漏洞，不是小瑕疵。
    """
    global PASS, FAIL
    print("-" * 60)
    print("规则模型 v4 契约测试")
    print("-" * 60)

    rules = MODEL.normalize({
        "schema": "v4",
        "rules": [
            {"id": "r-sub", "level": "high", "action": "block_remote",
             "match": {"type": "substring", "patterns": ["保密", "收购"]}},
            {"id": "r-re", "level": "medium", "action": "prefer_local",
             "match": {"type": "regex", "patterns": [r"底价\s*是\s*多少"]}},
            {"id": "r-word", "level": "high", "action": "block_remote",
             "match": {"type": "word", "patterns": ["nda"]}},
        ],
        "exceptions": [
            {"id": "news", "demote_to": "none",
             "applies_to": [{"rule": "r-sub", "patterns": ["收购"]}],
             "when": {"type": "substring", "patterns": ["新闻"]}},
            {"id": "soft", "demote_to": "medium",
             "applies_to": ["r-re"],
             "when": {"type": "substring", "patterns": ["大概"]}},
        ],
    })

    checks = [
        ("模式级豁免只掐点名的模式", MODEL.evaluate("保密协议 收购 新闻", rules)["level"], "high"),
        ("被豁免的模式确实不拦了", MODEL.evaluate("收购 新闻", rules)["level"], "none"),
        ("豁免未触发时照常拦", MODEL.evaluate("收购 可行性", rules)["level"], "high"),
        ("regex 原语生效", MODEL.evaluate("底价 是 多少", rules)["level"], "medium"),
        ("regex 不匹配则 none", MODEL.evaluate("底价底价", rules)["level"], "none"),
        ("word 原语生效", MODEL.evaluate("explain NDA", rules)["level"], "high"),
        ("word 原语不误伤 veranda", MODEL.evaluate("veranda", rules)["level"], "none"),
        ("demote_to=medium 是降级不是清除",
         MODEL.evaluate("底价是 多少 大概", rules)["level"], "medium"),
    ]
    for note, got, want in checks:
        ok = got == want
        PASS += 1 if ok else 0
        FAIL += 0 if ok else 1
        print("[%s] %s → %s (期望 %s)" % ("PASS" if ok else "FAIL", note, got, want))

    # v3 自动转换：转换后分级与动作都要保持
    conv = MODEL.normalize({
        "_schema": "v3",
        "privacy_high": {"action": "block_remote", "keywords": ["保密"]},
        "privacy_medium": {"action": "prefer_local", "keywords": ["预算"]},
    })
    ok = (MODEL.evaluate("保密", conv)["level"] == "high"
          and MODEL.evaluate("预算", conv)["level"] == "medium"
          and MODEL.evaluate("天气", conv)["level"] == "none"
          and conv["rules"][0]["action"] == "block_remote")
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1
    print("[%s] v3 自动转 v4 后分级与动作保持" % ("PASS" if ok else "FAIL"))

    # ★ v3 文件被 correct.py **写回**过一次之后，一个词都不能丢。
    #
    # 上面那条只覆盖了"读时转换"（一个词的合成文件）。而真实场景是：
    # 老用户的词表是 v3（活体那份就是），他做的第一次纠正会走
    # "读时转 v4 → 改 → 写回 v4"这条路——**文件格式当场就变了**。
    # 这条要守的是那次写回不丢词、且行尾/末尾换行都规整。
    tk = tempfile.mkdtemp(prefix="privacy-gate-v3write-")
    try:
        v3_path = os.path.join(tk, "rules.json")
        v3 = {
            "_schema": "v3",
            "privacy_high": {"action": "block_remote",
                             "keywords": ["保密", "合同", "股权"]},
            "privacy_medium": {"action": "prefer_local",
                               "keywords": ["预算", "排期"]},
        }
        with open(v3_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(v3, ensure_ascii=False))
        env = dict(os.environ)
        env["PRIVACY_GATE_RULES"] = v3_path
        env["PRIVACY_GATE_DATA"] = tk
        env["PYTHONUTF8"] = "1"
        subprocess.run(
            [sys.executable, os.path.join(_ROOT, "tools", "correct.py")],
            input=json.dumps({"action": "add", "keyword": "新增词", "level": "high",
                              "user_input": "新增词 出现了一次"}).encode("utf-8"),
            capture_output=True, env=env, timeout=60)
        raw = open(v3_path, "rb").read()
        after = json.loads(raw.decode("utf-8"))
        words = []
        for r in after.get("rules") or []:
            words += (r.get("match") or {}).get("patterns") or []
        wanted = ("保密", "合同", "股权", "预算", "排期", "新增词")
        ok = (after.get("schema") == "v4"
              and all(w in words for w in wanted)
              and b"\r\n" not in raw and raw.endswith(b"\n"))
        PASS += 1 if ok else 0
        FAIL += 0 if ok else 1
        print("[%s] ★v3 词表被 correct.py 写回后不丢词（%d 个词，schema=%s）"
              % ("PASS" if ok else "FAIL", len(words), after.get("schema")))
    finally:
        shutil.rmtree(tk, ignore_errors=True)

    # lint 必须抓出的问题（否则"契约可见"就是空话）
    lint_cases = [
        ("未知顶层字段", {"schema": "v4", "rules": [], "bogus": 1}),
        ("规则缺 id", {"schema": "v4", "rules": [
            {"level": "high", "match": {"type": "substring", "patterns": ["x"]}}]}),
        ("非法正则", {"schema": "v4", "rules": [
            {"id": "a", "level": "high", "match": {"type": "regex", "patterns": ["("]}}]}),
        ("重复 id", {"schema": "v4", "rules": [
            {"id": "a", "level": "high", "match": {"type": "substring", "patterns": ["x"]}},
            {"id": "a", "level": "medium", "match": {"type": "substring", "patterns": ["y"]}}]}),
        ("豁免点名不存在的模式", {"schema": "v4",
            "rules": [{"id": "a", "level": "high",
                       "match": {"type": "substring", "patterns": ["x"]}}],
            "exceptions": [{"id": "e", "applies_to": [{"rule": "a", "patterns": ["zzz"]}],
                            "when": {"type": "substring", "patterns": ["w"]}}]}),
        ("空规则表", {"schema": "v4", "rules": []}),
    ]
    for note, raw in lint_cases:
        sevs = sorted({s for s, _ in MODEL.lint(raw)})
        ok = "error" in sevs
        PASS += 1 if ok else 0
        FAIL += 0 if ok else 1
        print("[%s] lint 抓出「%s」 (%s)"
              % ("PASS" if ok else "FAIL", note, "、".join(sevs) or "没抓到"))

    # 发布包自带的规则文件必须 lint 干净
    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        errs = [m for s, m in MODEL.lint(raw) if s == "error"]
        ok = not errs
        PASS += 1 if ok else 0
        FAIL += 0 if ok else 1
        print("[%s] 仓库自带 rules.json lint 无错误%s"
              % ("PASS" if ok else "FAIL", ("：" + "; ".join(errs[:2])) if errs else ""))
    except Exception as e:
        FAIL += 1
        print("[FAIL] 读取 rules.json 失败: %s" % e)


def check_log_failsoft():
    """决策日志容错回归（v5.1 起）。

    日志是副产物，分级是核心职责。日志写失败（只读目录 / 容器只读挂载 /
    CI checkout / 受限沙箱）绝不能让引擎以非零退出码结束——否则插件侧
    fail-closed 兜底成 medium，会把每条消息都当敏感内容、拦掉所有远程工具。
    与 DECISIONS.md D11 记录的"引擎失败连锁锁死"同族，触发条件不同而已。
    """
    global PASS, FAIL
    print("-" * 60)
    print("决策日志容错测试")
    print("-" * 60)

    tmpdir = tempfile.mkdtemp(prefix="privacy-gate-test-")
    checks = []
    try:
        # 1) 可写路径：返回 True，落盘内容为 LF 结尾的 jsonl
        good = os.path.join(tmpdir, "ok.jsonl")
        wrote = ENGINE.log_decision({"session_id": "test", "level": "high"}, good)
        body = ""
        if os.path.isfile(good):
            with open(good, "rb") as f:
                body = f.read().decode("utf-8", "replace")
        checks.append((
            "日志可写 → 返回 True，落盘为 LF 结尾的 jsonl",
            wrote is True and body.endswith("\n") and "\r\n" not in body and "high" in body,
        ))

        # 2) 不可写路径（父路径是普通文件）→ 返回 False，绝不抛异常
        blocker = os.path.join(tmpdir, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        bad_log = os.path.join(blocker, "routing_log.jsonl")
        raised = False
        wrote_bad = None
        try:
            wrote_bad = ENGINE.log_decision({"level": "high"}, bad_log)
        except Exception:
            raised = True
        checks.append((
            "日志不可写 → 返回 False 且不抛异常",
            (not raised) and wrote_bad is False,
        ))

        # 3) 端到端：日志不可写时，引擎仍给出正确级别且退出码为 0
        engine = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "tools", "rules_engine.py")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        r = subprocess.run(
            [sys.executable, engine, "--json", "--log", "--log-path", bad_log, "--stdin"],
            input="帮我写一份保密协议".encode("utf-8"),
            capture_output=True, env=env, timeout=60,
        )
        level = None
        try:
            level = json.loads(r.stdout.decode("utf-8", "replace").strip()).get("effective_level")
        except Exception:
            pass
        checks.append((
            f"日志不可写 → 引擎仍判 high 且退出码 0（实得 {level}/{r.returncode}）",
            r.returncode == 0 and level == "high",
        ))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    for note, ok in checks:
        if ok:
            PASS += 1
        else:
            FAIL += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {note}")


def main():
    global PASS, FAIL
    paths.ensure_utf8_stdio()   # 中文输出；cp1252 控制台（如 GitHub 的 windows runner）会崩
    print("=" * 60)
    print("规则路由回归测试 v5")
    print("=" * 60)

    cases = CASES + load_external_cases()
    for text, expected, note in cases:
        actual = classify(text)
        ok = actual == expected
        if ok:
            PASS += 1
        else:
            FAIL += 1
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {text!r:40} → {actual:6} (期望 {expected})  {note}")

    print("-" * 60)
    print("多轮隐私继承测试")
    print("-" * 60)
    rules = ENGINE.load_rules(RULES_PATH)
    for text, prev, expected, note in INHERIT_CASES:
        level = ENGINE.check_privacy(text, rules)["level"]
        effective, inherited, shift = ENGINE.apply_inheritance(level, prev, text)
        ok = effective == expected
        if ok:
            PASS += 1
        else:
            FAIL += 1
        mark = "PASS" if ok else "FAIL"
        print(
            f"[{mark}] {text!r:40} prev={prev:6} → {effective:6} (期望 {expected}) "
            f"继承={inherited} 话题切换={shift}  {note}"
        )

    check_v4_model()
    check_log_failsoft()

    print("=" * 60)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("注意: 语义涉密但无关键词的用例, 由 worker prompt 的'默认保守'策略兜底")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
