#!/usr/bin/env python3
"""
privacy-gate 命令行入口（裸 CLI 适配器）

把散在 `tools/` 下的工具收成一个命令。v6 的解包（`pip install`）之后，
这里会变成 console_scripts 里的 `privacy-gate`；在那之前它就是仓库根目录的脚本，
零依赖、直接能跑。

子命令
------
    classify   给一段文本分级
    explain    解释"为什么是这个级别"
    stats      决策日志统计（最吵的规则与关键词）
    correct    纠正回流（扩充 / 收窄 / 降级 / 豁免）
    lint       规则文件契约检查
    gateway    启动本地网关（传输层门禁）
    doctor     一键体检
    version    版本

例子
----
    python privacy_gate.py classify --json --stdin <<'EOF'
    帮我写一份保密协议
    EOF

    python privacy_gate.py explain "帮我写保密协议，顺便看看收购的新闻"
    python privacy_gate.py stats --top 20
    python privacy_gate.py lint
    python privacy_gate.py gateway --cloud-upstream https://api.example.com/v1 \\
        --local-upstream http://127.0.0.1:11434/v1 --local-model "qwen3:35b"
    python privacy_gate.py doctor

为什么是 subprocess 而不是 import
--------------------------------
这些工具各自带 argparse 和 `main()`，直接 import 会因为 argparse 的 sys.argv
解析互相打架；而且它们本来就被设计成"可以单独执行"的形式（插件、worker 提示词、
shim 都是这么调它们的）。包一层 subprocess 也顺便保证了 stdio 原样透传，
管道和 heredoc 都能正常工作。

零第三方依赖。
"""

import json
import os
import subprocess
import sys

__version__ = "6.0.0-dev"

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_HERE, "tools")
DEFAULT_RULES = os.path.join(_HERE, "keywords", "rules.json")

# 子命令 → 脚本（相对仓库根）
SCRIPTS = {
    "classify": os.path.join(_TOOLS, "rules_engine.py"),
    "explain": os.path.join(_TOOLS, "explain.py"),
    "stats": os.path.join(_TOOLS, "stats.py"),
    "correct": os.path.join(_TOOLS, "correct.py"),
    "gateway": os.path.join(_TOOLS, "gateway.py"),
    "doctor": os.path.join(_HERE, "check.py"),
}

USAGE = __doc__.split("为什么是 subprocess", 1)[0].strip()


def cmd_lint(rest):
    """规则文件契约检查。

    单独在这里实现（而不是又一个脚本），因为它只依赖 rules_model，
    逻辑就十来行；多一个文件反而增加"哪个才是入口"的困惑。
    """
    import argparse
    sys.path.insert(0, _TOOLS)
    import rules_model

    ap = argparse.ArgumentParser(prog="privacy-gate lint", description="规则文件契约检查")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="规则文件路径")
    ap.add_argument("--strict", action="store_true", help="把 warn 也当成失败")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(rest)

    if not os.path.isfile(args.rules):
        msg = "规则文件不存在: %s" % args.rules
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False)
              if args.json else msg)
        return 1
    try:
        with open(args.rules, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        msg = "规则文件不是合法 JSON: %s" % e
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False)
              if args.json else msg)
        return 1

    problems = rules_model.lint(raw)
    errors = [m for s, m in problems if s == "error"]
    warns = [m for s, m in problems if s == "warn"]
    try:
        rules = rules_model.normalize(raw)
        summary = {
            "schema": rules.get("schema"),
            "rules": len(rules["rules"]),
            "exceptions": len(rules["exceptions"]),
            "remote_tool_patterns": len(rules["remote_tool_patterns"]),
        }
    except Exception as e:
        summary = {"error": str(e)}

    if args.json:
        print(json.dumps({"ok": not errors, "summary": summary,
                          "errors": errors, "warnings": warns},
                         ensure_ascii=False, indent=2))
    else:
        print("规则文件：%s" % args.rules)
        print("  概要：schema=%s  规则 %s 条 / 豁免 %s 条 / 远程工具模式 %s 个"
              % (summary.get("schema"), summary.get("rules"),
                 summary.get("exceptions"), summary.get("remote_tool_patterns")))
        for m in errors:
            print("  [错误] %s" % m)
        for m in warns:
            print("  [提示] %s" % m)
        if not problems:
            print("  契约检查通过。")

    if errors:
        return 1
    if warns and args.strict:
        return 1
    return 0


def run_script(name, rest):
    script = SCRIPTS[name]
    if not os.path.isfile(script):
        print("找不到脚本: %s" % script, file=sys.stderr)
        return 1
    # 不捕获 stdio：管道、heredoc、交互都要能原样透传
    # （受限沙箱下"捕获子进程输出"本身也会被拦，见本项目的环境笔记）
    try:
        return subprocess.call([sys.executable, script] + list(rest))
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print("privacy-gate %s" % __version__)
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd == "lint":
        return cmd_lint(rest)
    if cmd in SCRIPTS:
        return run_script(cmd, rest)

    print("未知子命令：%s" % cmd, file=sys.stderr)
    print("可用：" + "、".join(sorted(list(SCRIPTS) + ["lint", "version"])),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
