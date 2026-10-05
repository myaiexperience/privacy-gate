#!/usr/bin/env python3
"""
文档里的命令必须真的能跑

为什么需要它
------------
README 上的命令是**用户唯一会照抄的东西**。而这个项目刚刚改过 CLI 的形态
（解包之后多了一条 `privacy-gate` 命令，仓库里那个转发脚本也换了实现），
这类变化最容易漏改文档——**而文档飘了不会有任何测试失败**，
只会让第一个照抄的人浪费时间。

这与 D17 是同一条原则：能被机器守的承诺，不要留在人的记忆里。
"文档里的命令是对的"也是一个可以机器守的承诺。

验什么
------
  1. 代码块里以 `python` / `py` 开头、第一个参数是 `.py` 的命令
     → 那个文件必须存在
  2. `python privacy_gate.py <sub>` / `privacy-gate <sub>` / `python -m privacy_gate... <sub>`
     → `<sub>` 必须是 `tools/cli.py` 里真正注册过的子命令（**从源码取，不手抄清单**）
  3. 反引号里以仓库顶层目录开头的路径（`tools/…`、`docs/…`、`adapters/…` …）
     → 必须存在
  4. Markdown 链接指向的仓库内文件与目录必须存在（相对链接按该文档所在目录解析）

不验什么（以及为什么）
--------------------
  - **不验外部 URL**：那要联网，而且会让 CI 因为别人的站点挂掉而变红。
  - **不验每个 `--flag`**：文档里的命令行常带示例值，逐 flag 比对会产生大量误报。
  - **不验裸文件名**（如 `rules.json`）：文档里常省略目录，逐条要求会变成噪音。

**一个有噪音的检查等于没有检查**——它会先被忽略，然后被删掉。

退出码：0 全部对得上 / 1 有对不上的
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "data",
             ".pytest_cache", "build", "dist"}

# 仓库顶层目录：只有以它们开头的反引号路径才当作"仓库内路径"来验
TOP_DIRS = ("tools/", "adapters/", "docs/", "keywords/", "prompts/",
            ".opencode/", ".github/")

# 代码块里 "python xxx.py" 形式
PY_CMD = re.compile(r"(?m)^\s*(?:python3?|py)\s+(\S+\.py)\b")
# 子命令形式（三种入口）
SUB_CMD = re.compile(
    r"(?m)^\s*(?:python3?\s+privacy_gate\.py"
    r"|python3?\s+-m\s+privacy_gate(?:\.cli)?"
    r"|privacy-gate(?:\.exe)?)\s+([A-Za-z][A-Za-z0-9_-]*)")
FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.S)
BACKTICK = re.compile(r"`([^`\s]+)`")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")

# 关于"体检项数"：这里**刻意不检查**文档里的数字。
#
# 试过一版：数 check.py 里 record() 的不同标签，再和文档里的"N 项体检"比对。
# 它当场抓出了两处过期的 14。但接着就发现这条规则本身是错的：
# record() 注册 19 个标签，而**实际执行几项取决于布局**——
# 活体项目有「全局配置」「Provider 单一来源」「Ollama 连通性」三项，
# 发布包里没有它们，于是跑出来是 16 项。
#
# 也就是说：任何写在文档里的固定数字，必然在两种布局里错一个。
# 这不是"把 14 改成 15"能解决的，是"数字本身不该出现在文档里"。
# 所以两份 README 与 AGENTS.md 都改成不报数、只列覆盖范围——
# 而列出的那些项，由 check.py 自己保证存在。


def cli_subcommands():
    """从 tools/cli.py 里读真正注册过的子命令（不手抄一份，免得两边漂移）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import cli
    except Exception as e:  # pragma: no cover
        return None, "无法导入 tools/cli.py：%s" % e
    subs = set(cli.MODULES) | {"lint", "doctor", "version"}
    return subs, ""


def iter_docs():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)
            elif name.endswith((".yml", ".yaml")) and ".github" in dirpath:
                yield os.path.join(dirpath, name)


def resolve(base_dir, target):
    return os.path.normpath(os.path.join(base_dir, target))


def check_file(docpath, problems):
    rel = os.path.relpath(docpath, ROOT)
    text = open(docpath, encoding="utf-8").read()
    docdir = os.path.dirname(docpath)

    # ── 1. 代码块里的 python xxx.py ──
    for block in FENCE.findall(text):
        for m in PY_CMD.finditer(block):
            raw = m.group(1)
            if raw.startswith("-") or "<" in raw or raw.startswith("$"):
                continue
            cand = resolve(ROOT, raw)
            if not os.path.isfile(cand):
                problems.append((rel, "代码块里的脚本不存在", raw))

    # ── 2. 子命令 ──
    subs, err = cli_subcommands()
    if subs is not None:
        for m in SUB_CMD.finditer(text):
            sub = m.group(1)
            if sub not in subs:
                problems.append((rel, "文档写了 CLI 里没有的子命令", sub))
    elif err:
        problems.append((rel, "无法核对子命令", err))

    # ── 3. 反引号里的仓库路径 ──
    #
    # 只验"看起来确实是仓库路径"的：以顶层目录开头，**且**最后一段带扩展名
    # 或整体以 `/` 结尾（目录）。
    #
    # 为什么加后面这个条件：MCP 的方法名恰好长这样——`tools/list`、`tools/call`、
    # `prompts/list`——它们的头一段正好是仓库顶层目录名。第一版检查器就把这两个
    # 报成了"路径不存在"。**有噪音的检查会先被忽略、然后被删掉**，
    # 所以这里宁可少验，也不制造误报。
    for m in BACKTICK.finditer(text):
        raw = m.group(1).rstrip(".,;:）)")
        if not raw.startswith(TOP_DIRS):
            continue
        if "<" in raw or "*" in raw:
            continue
        last = raw.rstrip("/").rsplit("/", 1)[-1]
        if not raw.endswith("/") and "." not in last:
            continue          # 没有扩展名也不是目录 → 更像协议方法名，跳过
        cand = resolve(ROOT, raw)
        if not (os.path.isfile(cand) or os.path.isdir(cand)):
            problems.append((rel, "反引号里的仓库路径不存在", raw))

    # ── 4. Markdown 链接 ──
    for m in MD_LINK.finditer(text):
        raw = m.group(1)
        if raw.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        target = raw.split("#", 1)[0]
        if not target:
            continue
        cand = resolve(docdir, target)
        if not (os.path.isfile(cand) or os.path.isdir(cand)):
            problems.append((rel, "Markdown 链接指向的文件不存在", raw))


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    docs = sorted(iter_docs())
    problems = []
    for d in docs:
        check_file(d, problems)

    print("文档命令核对")
    print("=" * 62)
    print("扫描文档：%d 个" % len(docs))
    subs, err = cli_subcommands()
    if subs:
        print("CLI 子命令（从 tools/cli.py 读出）：%s" % "、".join(sorted(subs)))
    if problems:
        print("")
        for rel, kind, what in problems:
            print("  [%s] %s  →  %s" % (kind, rel, what))
        print("")
        print("失败：%d 处对不上。" % len(problems))
        print("文档里的命令是用户唯一会照抄的东西——它飘了，第一个照抄的人就白费功夫。")
        return 1
    print("结果：文档里提到的脚本、子命令、仓库路径与链接全部对得上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
