#!/usr/bin/env python3
"""
privacy-gate 命令行入口（裸 CLI 适配器）

它有两种被调用的方式，各有各的场合：

    **仓库检出里**（还没装）：
        python privacy_gate.py classify --stdin     ← 根目录那个转发脚本
        python tools/cli.py classify --stdin

    **装好之后**：
        privacy-gate classify --stdin               ← console script
        python -m privacy_gate.cli classify --stdin

注意 `python -m privacy_gate.cli` **只在装好之后可用**：在检出里，根目录那个
`privacy_gate.py` 文件会把同名包遮蔽掉（包是安装时才由 `package-dir` 映射出来的）。
这不是缺陷，是"仓库里叫 tools/、装出来叫 privacy_gate"这个双身份的必然结果。

子命令
------
    classify   给一段文本分级
    explain    解释"为什么是这个级别"
    stats      决策日志统计（最吵的规则与关键词）
    correct    纠正回流（扩充 / 收窄 / 降级 / 豁免）
    lint       规则文件契约检查
    gateway    启动本地网关（传输层门禁）
    smoke      拿真实上游验一次端到端（自带 --self-test）
    doctor     一键体检（**仅仓库检出可用**，见下）
    version    版本

为什么是 subprocess 而不是 import
--------------------------------
这些工具各自带 argparse 和 `main()`，直接 import 会因为 argparse 的 sys.argv
解析互相打架；而且它们本来就被设计成"可以单独执行"的形式（插件、worker 提示词
都是这么调它们的）。包一层 subprocess 也顺便保证了 stdio 原样透传，
管道和 heredoc 都能正常工作。

零第三方依赖。
"""

import argparse
import json
import os
import subprocess
import sys

try:
    from . import paths
    _PKG = __package__ or ""
except ImportError:  # 脚本模式
    import paths
    _PKG = ""

__version__ = "6.0.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = paths.rules_path()

# 子命令 → 模块名（仓库里是 tools/<name>.py，装好后是 privacy_gate.<name>）
MODULES = {
    "classify": "rules_engine",
    "explain": "explain",
    "stats": "stats",
    "correct": "correct",
    "gateway": "gateway",
    "smoke": "gateway_smoke",
}

USAGE = __doc__.split("为什么是 subprocess", 1)[0].strip()


def _target(name):
    """按当前上下文决定怎么起子进程。

    仓库里（被当作顶层模块导入）→ 直接跑 `tools/<name>.py`，
      于是插件、prompts、文档里那些既有路径全部照旧；
    装好之后（被当作包导入）→ `python -m privacy_gate.<name>`。
    """
    if _PKG:
        return [sys.executable, "-m", _PKG + "." + name]
    return [sys.executable, os.path.join(_HERE, name + ".py")]


def _rules_model():
    return paths.sibling("rules_model")


def cmd_lint_impl(rest):
    rules_model = _rules_model()
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
        summary = {"schema": rules.get("schema"),
                   "rules": len(rules["rules"]),
                   "exceptions": len(rules["exceptions"]),
                   "remote_tool_patterns": len(rules["remote_tool_patterns"])}
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


def cmd_doctor(rest):
    """一键体检。

    **只有仓库检出里才有意义**：它检查插件语法、prompts、配置模板、Ollama 连通性
    这些只有检出里才有的东西。装好的包里没有它们，所以这里如实说明并给出替代命令，
    而不是假装成功。
    """
    check = os.path.join(paths.package_root(), "check.py")
    if not os.path.isfile(check):
        print("doctor 需要仓库检出——它检查插件、prompts、配置模板等只有检出里才有的东西。",
              file=sys.stderr)
        print("装好的包里请用：privacy-gate lint / classify / explain / stats / gateway / smoke",
              file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, check] + list(rest))


def run_module(name, rest):
    try:
        return subprocess.call(_target(name) + list(rest))
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
        return cmd_lint_impl(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    if cmd in MODULES:
        return run_module(MODULES[cmd], rest)

    print("未知子命令：%s" % cmd, file=sys.stderr)
    print("可用：" + "、".join(sorted(list(MODULES) + ["lint", "doctor", "version"])),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
