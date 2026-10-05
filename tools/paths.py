#!/usr/bin/env python3
"""
路径解析：规则文件与数据目录到底在哪

同一份代码要在三种环境里都找得到规则文件：

  1. 仓库检出 / 可编辑安装   → `<包目录的上一级>/keywords/rules.json`
  2. 从别处运行             → `<$PWD>/keywords/rules.json`
  3. pip 安装的 wheel       → `<sysconfig data>/share/privacy-gate/rules.json`

优先级：显式参数 > `PRIVACY_GATE_RULES` > 上面三条按顺序。

**为什么值得单独一个模块**：这套"往上找一层"的推断如果散在五个文件里，
总有一天会漏改其中一个，然后出现"引擎找得到规则、网关找不到"这种最难查的不一致。
一次写好、五处共用，比五次各写一遍可靠。

`PRIVACY_GATE_RULES` 的意义不只是"配置灵活"——它是**"边界由使用者定"的落地**：
出厂词表是要发布、要被人抄走的默认值；你自己加的词属于你自己的策略，
应该待在你自己的文件里，否则每次 `git pull` 都要跟公开词表打架。
"""

import os
import sys
import sysconfig

_HERE = os.path.dirname(os.path.abspath(__file__))

# 数据目录名（日志、会话状态、纠正记录）
_DATA_ENV = "PRIVACY_GATE_DATA"


def package_root():
    """包目录的上一级。仓库布局下就是仓库根。"""
    return os.path.dirname(_HERE)


def rules_candidates():
    """按优先级列出候选路径（含环境变量给出的那个）。"""
    out = []
    env = os.environ.get("PRIVACY_GATE_RULES")
    if env:
        out.append(env)
    out.append(os.path.join(package_root(), "keywords", "rules.json"))
    out.append(os.path.join(os.getcwd(), "keywords", "rules.json"))
    try:
        data = sysconfig.get_path("data")
    except Exception:
        data = ""
    if data:
        out.append(os.path.join(data, "share", "privacy-gate", "rules.json"))
    return out


def rules_path(explicit=None):
    """第一个存在的候选；都不存在时返回第一个候选，好让报错指回最可能的位置。"""
    if explicit:
        return os.path.abspath(explicit)
    cands = rules_candidates()
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return os.path.abspath(cands[0]) if cands else ""


def data_dir(explicit=None):
    """日志与会话状态放哪。

    仓库里 → `<仓库>/data`（跟代码放一起，方便看）；
    装好之后 → `~/.privacy-gate`。

    刻意不往当前目录写：一个 CLI 工具跑到哪个项目目录就往那儿扔 `data/`，
    是让人恼火的行为。
    """
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get(_DATA_ENV)
    if env:
        return os.path.abspath(env)
    root = package_root()
    if os.path.isdir(os.path.join(root, "keywords")):
        return os.path.join(root, "data")
    return os.path.join(os.path.expanduser("~"), ".privacy-gate")


def sibling(name):
    """按当前上下文拿到同目录的兄弟模块。

    两种上下文都要支持：
      - 被当作**包**导入（pip 安装后、`python -m privacy_gate.<mod>`）→ 相对导入
      - 被当作**脚本**直接跑（`python tools/<mod>.py`）/ 被测试按顶层模块导入 → 绝对导入

    为什么不干脆把文件搬进包目录：`tools/` 是开发者看得见的位置，
    而且 opencode 插件、prompts、文档都按 `tools/<mod>.py` 这个路径调用它们。
    两边都要能用，就只能在**这里**做一次判断——而不是在每个模块里各写一遍。
    """
    import importlib
    pkg = __package__ or ""
    if pkg:
        return importlib.import_module("." + name, pkg)
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    return importlib.import_module(name)
