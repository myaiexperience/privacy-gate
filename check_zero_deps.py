#!/usr/bin/env python3
"""
零第三方依赖断言

本项目的卖点之一是"零第三方运行时依赖"。**卖点必须由机器守。**
否则某天有人顺手 `import requests`，README 上那句话就变成谎话了——
而且是在别人 clone 下来跑不通的时候才发现。

做法：AST 解析所有 .py，收集顶层 import 的模块名，然后**动态**判断每个名字是不是
标准库（看它装在哪儿，而不是查一张会过期的名单）。这个办法在 3.9–3.13 上都成立。

**JavaScript 也要扫**：这个包还发一个 opencode 插件（`.js`）。只扫 .py 的话，
有人往里写一句 `require("axios")`，断言照样全绿——**守卫漏了一种语言，
而"没报错"看起来和"没问题"一模一样**。JS 那一半的判定标准是：
`node:` 前缀或 Node 内建模块（名单**问 Node 要**，不手抄）+ 相对/绝对路径（本仓库自己的文件）。
拿不到 Node 时明确打印"[略过]（这不等于没问题）"，并在总结里再点一次——
**"没查"不能读起来像"查过且没问题"。**

用法：
  python check_zero_deps.py                 # 扫全仓库
  python check_zero_deps.py tools adapters  # 只扫指定子目录
退出码：0 全绿 / 1 发现第三方依赖或无法导入的模块
"""

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env",
             "node_modules", ".pytest_cache", ".idea", ".vscode"}

# 本仓库自带的模块名：它们不是"第三方"，但也确实不在标准库里。
#
# 新增模块时必须在这里加一笔——这是**故意的**：否则任何人都能靠"起个本地名字"
# 把第三方依赖混进来，而断言照绿。让"我引了本地模块"成为一次显式声明。
LOCAL_MODULES = {
    # tools/ 下的真实模块（安装后是 privacy_gate 包）
    "rules_model", "rules_engine", "session", "gateway", "correct", "explain",
    "stats", "paths", "cli", "gateway_smoke",
    # 仓库根目录的入口与检查脚本
    "privacy_gate", "check_zero_deps",
}

THIRD_PARTY_HINTS = ("site-packages", "dist-packages", "site-python")


def iter_py_files(paths):
    for base in paths:
        full = os.path.join(ROOT, base) if not os.path.isabs(base) else base
        if os.path.isfile(full) and full.endswith(".py"):
            yield full
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


# ── JavaScript 那一半 ──────────────────────────────────────
#
# 这个包**也发 JavaScript**（opencode 插件）。只扫 .py 的话，
# 有人往插件里写一句 `require("axios")`，零依赖断言照样全绿——
# **守卫漏了一种语言，而"没报错"看起来和"没问题"一模一样。**
JS_EXT = (".js", ".mjs", ".cjs")
# require("x") / from "x" / import "x" / import("x")
JS_SPEC = re.compile(r"""(?:require\s*\(\s*|from\s+|import\s*\(\s*|import\s+)['"]([^'"]+)['"]""")


def iter_js_files(paths):
    for base in paths:
        full = os.path.join(ROOT, base) if not os.path.isabs(base) else base
        if os.path.isfile(full) and full.endswith(JS_EXT):
            yield full
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(JS_EXT):
                    yield os.path.join(dirpath, name)


def node_builtins():
    """问 Node 要它的内建模块名单。

    拿不到就返回 None —— **"查不了"不等于"没问题"**，调用方必须分开说，
    而且绝不能退回到"手写一份 Node 内建模块清单"（手抄的清单会漂移，见 D21）。
    """
    if shutil.which("node") is None:
        return None
    try:
        r = subprocess.run(
            ["node", "-e", "console.log(require('module').builtinModules.join(' '))"],
            capture_output=True, timeout=30)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return set(r.stdout.decode("utf-8", "replace").split())


def js_offenders(path, builtins):
    """返回 [(行号, 模块名)] —— 非本地的、又不在 Node 内建名单里的。"""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if line.lstrip().startswith("//"):
                continue
            for m in JS_SPEC.finditer(line):
                spec = m.group(1)
                if spec.startswith(("./", "../", "/")):
                    continue                      # 相对/绝对路径 = 本仓库自己的文件
                name = spec.split("/")[0]
                if spec.startswith("node:") or name in builtins:
                    continue
                out.append((lineno, spec))
    return out


def top_level_imports(path):
    """返回该文件顶层 import 的模块名集合（含 `from a.b import c` 里的 a）。"""
    with open(path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=path)
        except SyntaxError as e:
            raise SyntaxError("%s: %s" % (path, e))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # 相对 import（level>0）是包内的，跳过
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def classify(name):
    """返回 (kind, detail)，kind ∈ stdlib | third-party | missing | local。"""
    if name in LOCAL_MODULES:
        return "local", ""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError) as e:
        return "missing", str(e)
    if spec is None:
        return "missing", "找不到这个模块"
    origin = getattr(spec, "origin", None) or ""
    if origin in ("built-in", "frozen"):
        return "stdlib", origin
    if not origin and getattr(spec, "submodule_search_locations", None):
        # 命名空间包：看它在哪
        locs = list(spec.submodule_search_locations or [])
        origin = locs[0] if locs else ""
    low = origin.replace("\\", "/").lower()
    if any(h in low for h in THIRD_PARTY_HINTS):
        return "third-party", origin
    return "stdlib", origin


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = list(sys.argv[1:] if argv is None else argv)
    paths = argv or ["."]

    offenders, missing, scanned = [], [], []
    for path in iter_py_files(paths):
        rel = os.path.relpath(path, ROOT)
        scanned.append(rel)
        try:
            names = top_level_imports(path)
        except SyntaxError as e:
            print("[FAIL] 语法错误：%s" % e)
            return 1
        for name in sorted(names):
            kind, detail = classify(name)
            if kind == "third-party":
                offenders.append((rel, name, detail))
            elif kind == "missing":
                missing.append((rel, name, detail))

    print("零第三方依赖检查")
    print("=" * 60)
    print("扫描文件：%d 个" % len(scanned))

    # 构建元数据里也不能有依赖。
    # 运行时代码零依赖固然好，但如果 pyproject 里写了 dependencies = ["requests"]，
    # pip 装的时候照样会把它拉下来——承诺一样破了。
    pyproject = os.path.join(ROOT, "pyproject.toml")
    meta_bad = False
    if os.path.isfile(pyproject):
        with open(pyproject, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"(?ms)^\s*dependencies\s*=\s*\[(.*?)\]", text)
        if m is None:
            print("  [警告] pyproject.toml 里找不到 dependencies 声明，无法核对")
        elif m.group(1).strip():
            body = " ".join(m.group(1).split())[:70]
            print("  [依赖] pyproject.toml 声明了运行时依赖：%s" % body)
            meta_bad = True
        else:
            print("  [ OK ] pyproject.toml 的 dependencies 是空的（零依赖也写进了元数据）")

    # JavaScript 那一半（见 JS_EXT 上的说明）：只扫 .py 会漏掉插件里的依赖。
    js_files = list(iter_js_files(paths))
    js_skipped = False
    if js_files:
        builtins = node_builtins()
        if builtins is None:
            js_skipped = True
            print("  [略过] JS 依赖检查：拿不到 Node 内建模块名单（**这不等于没问题**）")
        else:
            js_bad = 0
            for path in js_files:
                rel = os.path.relpath(path, ROOT)
                scanned.append(rel)
                for lineno, spec in js_offenders(path, builtins):
                    js_bad += 1
                    offenders.append((rel, spec, "第 %d 行（非 node 内建、也非本地文件）" % lineno))
            if not js_bad:
                print("  [ OK ] %d 个 JS 文件：只引 Node 内建模块" % len(js_files))

    for rel, name, detail in offenders:
        print("  [第三方] %s 导入 %s  ← %s" % (rel, name, detail))
    for rel, name, detail in missing:
        print("  [找不到] %s 导入 %s  ← %s" % (rel, name, detail))

    if offenders or missing or meta_bad:
        print("")
        msg = "失败："
        if offenders:
            msg += "%d 个第三方依赖、" % len(offenders)
        if missing:
            msg += "%d 个无法导入的模块、" % len(missing)
        if meta_bad:
            msg += "构建元数据里声明了依赖、"
        print(msg.rstrip("、") + "。")
        print("这个项目承诺零第三方运行时依赖——要加依赖，请先改 README 的承诺，")
        print("而不是让这句话在别人 clone 下来时变成谎话。")
        return 1

    print("结果：全部 import 都来自标准库或本仓库自身，构建元数据也没有声明依赖。")
    if js_skipped:
        # 别让"没查"读起来像"查过且没问题"——上一行的"全部"只覆盖了 Python 那一半。
        print("      注：**JS 依赖检查这次被略过**（见上面的 [略过]）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
