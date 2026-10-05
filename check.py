#!/usr/bin/env python3
"""
opencode 隐私门禁一键体检（check.py）

覆盖本项目历史上踩过的所有坑：
  1. 配置解析        opencode.jsonc 可解析（JSONC）+ worker/cloud 定义完整
                     + {file:...} prompt 引用存在 + 模型名与 provider 匹配（项目+全局合并视图）
  1.5 Provider 单一来源  provider.ollama 收敛在全局配置，项目内不重复定义
  2. 引擎中文端到端  通过 tools/rules_engine.py shim 用 UTF-8 管道喂中文
                     （含 --log 写盘路径，即当年 UnicodeEncodeError 的复现路径；
                      日志落在临时目录，体检本身不依赖仓库可写）
  3. correct.py 回流  shim 可用（循环导入检查）+ UTF-8 stdin 解析 + 无副作用
  3.5 纠正回流写盘   在临时目录真实跑一次纠正：行尾锁定 LF + 末尾换行 +
                     只新增一行（防 Windows 下整文件被重写）
  3.6 规则契约       rules.json 的 v4 schema 与 lint（未知字段、无效 regex、
                     重复 id、悬空豁免作用域、层级冲突）
  3.7 收窄路径       remove / demote / exempt 三种纠正在临时规则库上真实跑一遍，
                     含"豁免不得溢出到同规则其他模式"这条安全回归
  3.8 可观测工具     explain / stats 在临时数据上真实跑一遍；含隐私哨兵：
                     统计报告的输出里绝不能出现日志中的 user_input
  4. 插件检查         node --check 语法 + "导出必须是函数"契约（桌面端加载要求）
  5. 状态文件         privacy-gate-state.json 合法、无引擎失败残留的假 medium
  6. Ollama 连通性    推理服务器可达 + 配置里的模型 tag 存在（不可达只告警，
                     不阻塞——云端模型不受影响）
  7. 回归测试         test_routes.py 全绿
  7.5 网关回归        test_gateway.py 全绿：重路由 / 工具剥夺 / 会话继承 /
                     fail-closed 方向性（本地上游挂了必须 502，绝不改走云端）

用法:
  python 01-OpenCode配置/check.py      # 活体项目布局
  python check.py                     # 发布包布局（copy 到发布包根目录后）

退出码: 0=全绿（允许有 WARN）  1=存在 FAIL
改 rules.json / rules_engine.py / correct.py / privacy-gate.js 后必须跑一遍。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
# 布局自适应：
#   活体项目：check.py 位于 <项目根>/01-OpenCode配置/check.py
#   发布包  ：check.py 位于 <项目根>/check.py（本目录即项目根）
if os.path.basename(_HERE) == "01-OpenCode配置":
    PROJECT_ROOT = os.path.dirname(_HERE)
    CANON_DIR = _HERE
    LAYOUT = "live"
else:
    PROJECT_ROOT = _HERE
    CANON_DIR = _HERE
    LAYOUT = "package"
CONFIG_PATH = os.path.join(PROJECT_ROOT, "opencode.jsonc")
CONFIG_EXAMPLE_PATH = os.path.join(PROJECT_ROOT, "opencode.jsonc.example")


def _first_existing(*paths):
    """返回第一个存在的路径；全不存在时返回最后候选（由检查项报 FAIL）。"""
    for p in paths:
        if os.path.isfile(p):
            return p
    return paths[-1]


PLUGIN_PATH = os.path.join(PROJECT_ROOT, ".opencode", "plugins", "privacy-gate.js")
STATE_PATH = os.path.join(PROJECT_ROOT, ".opencode", "privacy-gate-state.json")
RULES_PATH = os.path.join(CANON_DIR, "keywords", "rules.json")
CASES_PATH = os.path.join(CANON_DIR, "keywords", "test_cases.json")
SHIM_ENGINE = _first_existing(
    os.path.join(PROJECT_ROOT, "tools", "rules_engine.py"),
    os.path.join(CANON_DIR, "tools", "rules_engine.py"),
)
SHIM_CORRECT = _first_existing(
    os.path.join(PROJECT_ROOT, "tools", "correct.py"),
    os.path.join(CANON_DIR, "tools", "correct.py"),
)
TEST_ROUTES = _first_existing(
    os.path.join(CANON_DIR, "test_routes.py"),
    os.path.join(PROJECT_ROOT, "test_routes.py"),
)

# 规则模型（v4 契约）。发布包与活体布局下 tools/ 都在 CANON_DIR 旁。
sys.path.insert(0, os.path.join(CANON_DIR, "tools"))
try:
    import rules_model  # noqa: E402
except Exception:  # 真的缺了，由「规则契约」那一项体检报出来
    rules_model = None

KNOWN_PERMISSIONS = {"read", "glob", "grep", "webfetch", "websearch",
                     "write", "edit", "bash", "todo", "patch"}

results = []  # (检查名, 状态, 说明)


def record(name, status, message):
    results.append((name, status, message))
    print(f"[{status:4}] {name}: {message}")


# ── 工具函数 ──────────────────────────────────────────────

def load_jsonc(path):
    """解析 JSONC（支持 // 与 /* */ 注释、尾逗号；字符串内不误删）。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out, i, n = [], 0, len(text)
    in_str, esc = False, False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == ",":  # 尾逗号：后面紧跟 } 或 ] 时跳过
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return json.loads("".join(out))


def _child_env():
    """子进程统一用 UTF-8 标准流，避免管道捕获时中文按 GBK 编码变乱码。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_py(args, cwd, stdin_bytes=None, timeout=180):
    return subprocess.run(
        [sys.executable] + args,
        cwd=cwd,
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
        env=_child_env(),
    )


# ── 1. 配置解析（项目 + 全局合并视图）──────────────────────

GLOBAL_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "opencode", "opencode.jsonc"
)


def load_global_config():
    if not os.path.isfile(GLOBAL_CONFIG_PATH):
        return None
    try:
        return load_jsonc(GLOBAL_CONFIG_PATH)
    except Exception as e:
        record("全局配置", "FAIL", f"全局 opencode.jsonc 解析失败: {e}")
        return None


def provider_view(cfg, gcfg):
    proj = ((cfg or {}).get("provider") or {}).get("ollama") or {}
    glob = ((gcfg or {}).get("provider") or {}).get("ollama") or {}
    return proj, glob


def merged_models(cfg, gcfg):
    proj, glob = provider_view(cfg, gcfg)
    return {**(glob.get("models") or {}), **(proj.get("models") or {})}


def check_config(gcfg):
    if not os.path.isfile(CONFIG_PATH):
        if os.path.isfile(CONFIG_EXAMPLE_PATH):
            record("配置解析", "WARN",
                   "未找到 opencode.jsonc（发布包布局）——复制 opencode.jsonc.example 为 opencode.jsonc 并填好 Ollama 地址后重跑")
        else:
            record("配置解析", "WARN", "未找到 opencode.jsonc")
        return None
    try:
        cfg = load_jsonc(CONFIG_PATH)
    except Exception as e:
        record("配置解析", "FAIL", f"opencode.jsonc 解析失败: {e}")
        return None

    problems = []
    agents = cfg.get("agent") or {}
    for name in ("worker", "cloud"):
        if name not in agents:
            problems.append(f"缺 {name} agent 定义")

    worker = agents.get("worker") or {}
    model = worker.get("model") or ""
    if not model.startswith("ollama/"):
        problems.append(f"worker.model 应为 ollama/<tag> 形式，实为 {model!r}")
    else:
        tag = model.split("/", 1)[1]
        models = merged_models(cfg, gcfg)
        if tag not in models:
            problems.append(f"provider.ollama.models（项目+全局合并）里没有 {tag!r}")

    base = os.path.dirname(CONFIG_PATH)
    for name in ("worker", "cloud"):
        prompt = (agents.get(name) or {}).get("prompt", "")
        m = re.search(r"\{file:([^}]+)\}", prompt or "")
        if not m:
            problems.append(f"{name}.prompt 不是 {{file:...}} 引用")
            continue
        rel = m.group(1)
        if rel.startswith("./"):
            rel = rel[2:]
        p = os.path.normpath(os.path.join(base, rel))
        if not os.path.isfile(p):
            problems.append(f"{name} prompt 文件不存在: {p}")
        elif os.path.getsize(p) == 0:
            problems.append(f"{name} prompt 文件为空: {p}")

    for name in ("worker", "cloud"):
        perms = (agents.get(name) or {}).get("permission") or {}
        unknown = set(perms) - KNOWN_PERMISSIONS
        if unknown:
            problems.append(f"{name} 含未知 permission 键: {sorted(unknown)}")

    if problems:
        record("配置解析", "FAIL", "; ".join(problems))
    else:
        record("配置解析", "PASS", "JSONC 结构完整，worker/cloud + prompt 引用 + 模型映射（合并视图）全部有效")
    return cfg


# ── 1.5 Provider 单一来源 ───────────────────────────────────

def check_provider_source(cfg, gcfg):
    """provider.ollama 的单一来源约定（活体布局：全局配置；发布包：项目配置也可）。"""
    proj, glob = provider_view(cfg, gcfg)
    if not glob and not proj:
        record("Provider 单一来源", "FAIL", "项目+全局都没有 provider.ollama（worker 无法解析模型）")
        return
    if not glob:
        if LAYOUT == "package":
            record("Provider 单一来源", "PASS",
                   "provider 定义在项目配置（发布包默认方式；桌面端如需模型列表可见，建议移入全局配置）")
        else:
            record("Provider 单一来源", "WARN",
                   "provider 只定义在项目配置（桌面端模型列表可能看不到本地模型，建议移入全局配置）")
        return
    if not proj:
        record("Provider 单一来源", "PASS", "provider.ollama 仅在全局配置定义（单一来源）")
        return
    pbase = (proj.get("options") or {}).get("baseURL")
    gbase = (glob.get("options") or {}).get("baseURL")
    if pbase != gbase:
        record("Provider 单一来源", "FAIL",
               f"项目与全局 baseURL 不一致：项目={pbase!r} 全局={gbase!r}")
        return
    if set(proj.get("models") or {}) != set(glob.get("models") or {}):
        record("Provider 单一来源", "WARN",
               "项目配置里也定义了 provider.ollama 且与全局不一致（建议删除项目内定义，保持单一来源）")
        return
    record("Provider 单一来源", "WARN",
           "项目配置里也定义了 provider.ollama（与全局一致；建议删除项目内定义，保持单一来源）")


# ── 2. 引擎中文端到端 ──────────────────────────────────────

def check_engine():
    if not os.path.isfile(SHIM_ENGINE):
        record("引擎中文端到端", "FAIL", f"shim 不存在: {SHIM_ENGINE}")
        return
    sess = f"check-{int(time.time())}"
    detail = []
    ok = True
    cases = [
        ("公开输入", "帮我看看今天的天气怎么样，顺便写一段Python代码", "none"),
        ("敏感输入", "帮我写一份保密协议，涉及股权分配", "high"),
    ]
    # 决策日志写到临时目录，而不是仓库内的 data/：
    # 体检必须能在只读检出（CI、容器只读挂载、陌生贡献者的环境）下通过。
    # 否则日志写失败会让引擎以退出码 1 结束，体检报"引擎坏了"——那是假警报，
    # 而真实原因只是这个目录不可写（见 rules_engine.log_decision 的说明）。
    tmpdir = tempfile.mkdtemp(prefix="privacy-gate-check-")
    log_path = os.path.join(tmpdir, "routing_log.jsonl")
    try:
        for label, text, want in cases:
            r = run_py(
                [SHIM_ENGINE, "--json", "--log", "--log-path", log_path,
                 "--session-id", sess, "--prev-level", "none",
                 "--source", "check", "--stdin"],
                PROJECT_ROOT,
                stdin_bytes=text.encode("utf-8"),
                timeout=60,
            )
            got = None
            try:
                got = json.loads(r.stdout.decode("utf-8", "replace").strip()).get("effective_level")
            except Exception:
                pass
            if r.returncode != 0 or got != want:
                ok = False
                detail.append(f"{label}: 期望 {want} 实得 {got} (退出码 {r.returncode})")
            else:
                detail.append(f"{label} → {got} ✓")
        # 写盘路径仍然被真实走了一遍（当年 UnicodeEncodeError 的复现点），
        # 只是落在临时目录。确认它确实写出来了。
        if not (os.path.isfile(log_path) and os.path.getsize(log_path) > 0):
            ok = False
            detail.append("决策日志未落盘（--log 写盘路径回归失效）")
    except Exception as e:
        ok = False
        detail.append(f"调用异常: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    record("引擎中文端到端", "PASS" if ok else "FAIL", " | ".join(detail))


# ── 3. correct.py 回流 ─────────────────────────────────────

def check_correct():
    if not os.path.isfile(SHIM_CORRECT):
        record("correct.py 回流", "FAIL", f"shim 不存在: {SHIM_CORRECT}")
        return
    try:
        before_rules = open(RULES_PATH, "rb").read()
        before_cases = open(CASES_PATH, "rb").read()
        payload = json.dumps(
            {"user_input": "体检脚本中文测试", "note": "check"}, ensure_ascii=False
        ).encode("utf-8")
        r = run_py([SHIM_CORRECT], PROJECT_ROOT, stdin_bytes=payload, timeout=60)
        after_rules = open(RULES_PATH, "rb").read()
        after_cases = open(CASES_PATH, "rb").read()
    except Exception as e:
        record("correct.py 回流", "FAIL", f"调用异常: {e}")
        return

    problems = []
    if r.returncode != 2:
        problems.append(f"缺 keyword 应退出码 2（先走到校验说明 shim 可导入），实得 {r.returncode}")
    try:
        data = json.loads(r.stdout.decode("utf-8", "replace").strip())
        if data.get("ok") is not False:
            problems.append("缺 keyword 应返回 ok=false")
    except Exception:
        problems.append(f"stdout 不是 JSON: {r.stdout[:120]!r}")
    if before_rules != after_rules or before_cases != after_cases:
        problems.append("体检不应改动 rules.json / test_cases.json")

    if problems:
        record("correct.py 回流", "FAIL", "; ".join(problems))
    else:
        record("correct.py 回流", "PASS", "shim 可导入（无循环引用）、UTF-8 stdin 解析正常、零副作用")


# ── 3.5 纠正回流写盘卫生 ───────────────────────────────────

def check_correct_write():
    """一次纠正只应新增一个关键词，不该把整个规则库重写一遍。

    真实坑：json.dump 不写末尾换行，而 Windows 文本模式会把 \\n 转成 \\r\\n。
    两者叠加后，用户在 Windows 上做一次纠正，rules.json 整个文件都变成
    "已修改"，diff 里根本看不出真正加了哪个词——而规则库正是要靠 diff 审阅的。

    做法：在临时目录里复制一份 tools/ + keywords/，真实跑一次纠正（带 keyword
    的完整路径），断言行尾仍是 LF、末尾有换行、行数只 +1、新词确实入库。
    全程不碰仓库内的真实规则库。
    """
    if not os.path.isfile(SHIM_CORRECT) or not os.path.isfile(RULES_PATH):
        record("纠正回流写盘", "FAIL", "correct.py 或 keywords/rules.json 不存在")
        return
    tmp = tempfile.mkdtemp(prefix="privacy-gate-correct-")
    try:
        os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "keywords"), exist_ok=True)
        # 复制整个 tools/（不只是 correct.py）：correct.py 依赖 rules_model，
        # 将来还可能加模块。只挑单个文件复制，加依赖时这条体检就会莫名其妙地失败。
        tools_src = os.path.dirname(SHIM_CORRECT)
        copied = 0
        for name in sorted(os.listdir(tools_src)):
            if name.endswith(".py"):
                shutil.copy2(os.path.join(tools_src, name),
                             os.path.join(tmp, "tools", name))
                copied += 1
        if not copied:
            record("纠正回流写盘", "FAIL", f"{tools_src} 下没有 .py 可复制")
            return
        shutil.copy2(RULES_PATH, os.path.join(tmp, "keywords", "rules.json"))
        if os.path.isfile(CASES_PATH):
            shutil.copy2(CASES_PATH, os.path.join(tmp, "keywords", "test_cases.json"))

        probe = "体检探针词-不必入库"
        before = open(os.path.join(tmp, "keywords", "rules.json"), "rb").read()
        payload = json.dumps({
            "user_input": "体检用纠正用例-不必入库",
            "level": "medium",
            "keyword": probe,
            "correction_type": "general",
            "note": "check",
        }, ensure_ascii=False).encode("utf-8")
        r = run_py([os.path.join(tmp, "tools", "correct.py")], tmp,
                   stdin_bytes=payload, timeout=60)
        after = open(os.path.join(tmp, "keywords", "rules.json"), "rb").read()

        problems = []
        if r.returncode != 0:
            problems.append(f"完整纠正应退出码 0，实得 {r.returncode}")
        if b"\r\n" in after:
            problems.append("写回后出现 CRLF（Windows 文本模式未锁定 LF）")
        if not after.endswith(b"\n"):
            problems.append("写回后缺末尾换行（json.dump 不会自己补）")
        if probe.encode("utf-8") not in after:
            problems.append("新关键词未写入规则库")
        if after.count(b"\n") != before.count(b"\n") + 1:
            problems.append(
                f"规则库行数应只 +1，实得 {before.count(b'\\n')} → {after.count(b'\\n')}"
                "（疑似整文件被重写）"
            )
        if problems:
            record("纠正回流写盘", "FAIL", "; ".join(problems))
        else:
            record("纠正回流写盘", "PASS", "行尾锁定 LF、末尾换行、新增词只影响一行")
    except Exception as e:
        record("纠正回流写盘", "FAIL", f"调用异常: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 3.6 规则文件契约（v4 schema + lint）────────────────────

def check_rules_schema():
    """规则文件的契约检查。

    这是"开放性"的地基：使用者要写自己的边界，得先有契约可依。
    v5.1 的 rules.json 看着像契约，其实只有 keywords 被引擎读——
    对全仓库 .py 检索 action|description|_schema|_note 是零匹配。
    v4 起契约显式化，这一项就负责让契约不漂移。
    """
    if rules_model is None:
        record("规则契约", "FAIL", "rules_model 导入失败（tools/rules_model.py 缺失？）")
        return
    if not os.path.isfile(RULES_PATH):
        record("规则契约", "FAIL", f"规则文件不存在: {RULES_PATH}")
        return
    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        record("规则契约", "FAIL", f"规则文件不是合法 JSON: {e}")
        return
    try:
        problems = rules_model.lint(raw)
        rules = rules_model.normalize(raw)
    except Exception as e:
        record("规则契约", "FAIL", f"lint/normalize 异常: {e}")
        return

    summary = ("schema=%s 规则 %d 条 / 豁免 %d 条 / 远程工具模式 %d 个"
               % (rules.get("schema"), len(rules["rules"]), len(rules["exceptions"]),
                  len(rules["remote_tool_patterns"])))
    errs = [m for s, m in problems if s == "error"]
    warns = [m for s, m in problems if s == "warn"]
    if errs:
        record("规则契约", "FAIL", "%s；%d 个错误: %s" % (summary, len(errs), "; ".join(errs[:3])))
    elif warns:
        record("规则契约", "WARN", "%s；%d 个提示: %s" % (summary, len(warns), "; ".join(warns[:2])))
    else:
        record("规则契约", "PASS", summary + "；lint 无问题")


# ── 3.7 纠正回流的收窄路径 ─────────────────────────────────

def _correct_payload(tmp, payload):
    """在临时规则库上跑一次 correct.py，返回 (退出码, 解析后的 JSON)。"""
    args = [os.path.join(tmp, "tools", "correct.py")]
    r = run_py(args, tmp, stdin_bytes=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
               timeout=60)
    try:
        return r.returncode, json.loads(r.stdout.decode("utf-8", "replace").strip())
    except Exception:
        return r.returncode, None


def check_correct_actions():
    """收窄路径（remove / demote / exempt）在临时目录里真实跑一遍。

    v5.1 的 correct.py 只能追加，删不掉误伤的词。但**误伤比漏检更常见**——
    "搜某公司收购的公开新闻被拦成 high"是本项目诚实清单的第一条局限。
    一个"边界由你定"的工具链只支持放大、不支持收窄，是开放性的直接缺口。
    这条体检就是防止收窄路径悄悄坏掉。
    """
    if rules_model is None or not os.path.isfile(SHIM_CORRECT):
        record("收窄路径", "FAIL", "rules_model 或 correct.py 不可用")
        return
    tmp = tempfile.mkdtemp(prefix="privacy-gate-narrow-")
    problems = []
    detail = []
    try:
        os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "keywords"), exist_ok=True)
        tools_src = os.path.dirname(SHIM_CORRECT)
        for name in sorted(os.listdir(tools_src)):
            if name.endswith(".py"):
                shutil.copy2(os.path.join(tools_src, name), os.path.join(tmp, "tools", name))
        rules_tmp = os.path.join(tmp, "keywords", "rules.json")
        # 用**合成**规则库，而不是仓库自带的那份：自带的策略会随版本演进
        # （比如它现在已经自带一条"公开新闻"豁免），拿它当测试基线会让这条体检
        # 随策略变化而失效。测试要测的是工具，不是当前策略。
        synthetic = {
            "schema": "v4",
            "rules": [
                {"id": "high-default", "level": "high", "action": "block_remote",
                 "match": {"type": "substring", "patterns": ["保密", "收购", "合同"]}},
                {"id": "medium-default", "level": "medium", "action": "prefer_local",
                 "match": {"type": "substring", "patterns": ["预算"]}},
            ],
            "exceptions": [],
            "topic_shift_keywords": ["换个话题"],
            "remote_tool_patterns": ["*web*", "*fetch*"],
        }
        with open(rules_tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(synthetic, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if os.path.isfile(CASES_PATH):
            shutil.copy2(CASES_PATH, os.path.join(tmp, "keywords", "test_cases.json"))

        def load_tmp():
            with open(rules_tmp, encoding="utf-8") as f:
                return rules_model.normalize(json.load(f))

        def level_of(text):
            return rules_model.evaluate(text, load_tmp())["level"]

        # 前提：这个词确实会被拦
        if level_of("看看收购的公开新闻") != "high":
            problems.append("前提不成立：合成规则里「收购」应当先判 high")

        # 1) exempt —— 只在公开新闻语境下豁免
        code, out = _correct_payload(tmp, {
            "action": "exempt", "keyword": "收购",
            "when": ["新闻", "公告", "公开报道"], "demote_to": "none",
            "user_input": "看看收购的公开新闻", "note": "check",
        })
        if code != 0 or not (out or {}).get("ok"):
            problems.append("exempt 失败：exit=%s out=%s" % (code, out))
        else:
            got = level_of("看看收购的公开新闻")
            if got != "none":
                problems.append("exempt 生效后应判 none，实得 %s" % got)
            # 关键：豁免不得溢出到同规则的其他模式。
            # 这条输入同时含「保密」（不该被豁免）和「收购」（已豁免）+ 触发词「新闻」，
            # 如果豁免按规则级实现，「保密」会一起失效 → 判 none，那是安全漏洞。
            other = level_of("帮我写保密协议，顺便看看收购的新闻")
            if other != "high":
                problems.append("豁免溢出了！「保密+收购+新闻」应仍为 high，实得 %s" % other)
            detail.append("exempt 生效且未溢出")

        # 2) remove —— 把词整个删掉，并清掉悬空的豁免作用域
        code, out = _correct_payload(tmp, {
            "action": "remove", "keyword": "收购", "note": "check",
        })
        if code != 0 or not (out or {}).get("ok"):
            problems.append("remove 失败：exit=%s out=%s" % (code, out))
        else:
            if level_of("评估一下收购XX公司的可行性") != "none":
                problems.append("remove 之后「收购」不该再命中")
            dropped = (out or {}).get("dropped_scopes") or []
            if not any("收购" in str(d.get("pattern", "")) or "作用域已全部失效" in str(d.get("reason", ""))
                       for d in dropped):
                problems.append("remove 后应报告被清理的悬空豁免，实得 %s" % dropped)
            detail.append("remove 生效并清理悬空作用域")

        # 3) demote —— 从 high 挪到 medium
        code, out = _correct_payload(tmp, {
            "action": "demote", "keyword": "合同", "to": "medium", "note": "check",
        })
        if code != 0 or not (out or {}).get("ok"):
            problems.append("demote 失败：exit=%s out=%s" % (code, out))
        else:
            got = level_of("帮我写一份合同")
            if got != "medium":
                problems.append("demote 到 medium 后应判 medium，实得 %s" % got)
            detail.append("demote 生效")

        # 4) 改完必须仍是合法契约（行尾 LF + lint 无 error）
        raw = open(rules_tmp, "rb").read()
        if b"\r\n" in raw:
            problems.append("收窄路径写盘引入了 CRLF")
        if not raw.endswith(b"\n"):
            problems.append("收窄路径写盘缺末尾换行")
        errs = [m for s, m in rules_model.lint(json.loads(raw.decode("utf-8"))) if s == "error"]
        if errs:
            problems.append("改完 lint 报错: " + "; ".join(errs[:2]))

        if problems:
            record("收窄路径", "FAIL", "; ".join(problems))
        else:
            record("收窄路径", "PASS", "；".join(detail) + "；写盘 LF/lint 均正常")
    except Exception as e:
        record("收窄路径", "FAIL", "调用异常: %s" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 3.8 可观测性工具（explain / stats）──────────────────────

def check_observability():
    """explain / stats 在临时数据上真实跑一遍。

    为什么必须守：v6 默认策略会把敏感会话**无声重路由**到本地弱模型，
    误命中从"拦一下你看得见"变成"悄悄降级你不知道"。可观测性是那个取舍的唯一补偿——
    这两个工具坏了，代价就从"看得到"变成"看不到"。

    另外钉一条隐私哨兵：日志里含用户原文，但统计报告**绝不能**把它打出来。
    """
    tmp = tempfile.mkdtemp(prefix="privacy-gate-observe-")
    problems, detail = [], []
    try:
        tools_tmp = os.path.join(tmp, "tools")
        os.makedirs(tools_tmp, exist_ok=True)
        tools_src = os.path.dirname(SHIM_CORRECT)
        for name in sorted(os.listdir(tools_src)):
            if name.endswith(".py"):
                shutil.copy2(os.path.join(tools_src, name), os.path.join(tools_tmp, name))

        rules_path = os.path.join(tmp, "rules.json")
        synthetic = {
            "schema": "v4",
            "rules": [
                {"id": "high-default", "level": "high", "action": "block_remote",
                 "match": {"type": "substring", "patterns": ["保密", "收购"]}},
                {"id": "medium-default", "level": "medium", "action": "prefer_local",
                 "match": {"type": "substring", "patterns": ["预算"]}},
            ],
            "exceptions": [
                {"id": "news", "demote_to": "none",
                 "applies_to": [{"rule": "high-default", "patterns": ["收购"]}],
                 "when": {"type": "substring", "patterns": ["新闻"]}},
            ],
            "topic_shift_keywords": ["换个话题"],
            "remote_tool_patterns": ["*web*"],
        }
        with open(rules_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(synthetic, f, ensure_ascii=False, indent=2)
            f.write("\n")

        def explain_of(text):
            r = run_py([os.path.join(tools_tmp, "explain.py"), "--json",
                        "--rules", rules_path, text], tmp, timeout=60)
            try:
                return json.loads(r.stdout.decode("utf-8", "replace").strip()), r.returncode
            except Exception:
                return None, r.returncode

        # 1) 豁免生效
        exp, code = explain_of("看看收购的公开新闻")
        if code != 0 or not exp:
            problems.append("explain 调用失败：exit=%s" % code)
        else:
            if exp.get("level") != "none":
                problems.append("explain 判级错：期望 none，实得 %s" % exp.get("level"))
            eff = [e for e in exp.get("exceptions", []) if e.get("effective")]
            if not eff:
                problems.append("explain 没标出生效的豁免")
            elif not any("收购" in s.get("terms", []) for e in eff for s in e.get("suppressed", [])):
                problems.append("explain 没说明豁免掐掉了哪个模式")
            detail.append("explain 能说明豁免生效")

        # 2) 豁免不得溢出（同一条规则里的其他模式必须保住）
        exp2, code2 = explain_of("帮我写保密协议，顺便看看收购的新闻")
        if code2 != 0 or not exp2:
            problems.append("explain 溢出用例调用失败：exit=%s" % code2)
        else:
            if exp2.get("level") != "high":
                problems.append("豁免溢出：期望 high，实得 %s" % exp2.get("level"))
            elif "保密" not in (exp2.get("matched_keywords") or []):
                problems.append("豁免溢出：保密的贡献被连带掐掉了")
            detail.append("explain 反映「豁免不溢出」")

        # 3) stats：聚合正确，且**绝不泄漏 user_input**
        canary = "SENTINEL-LEAK-CANARY"
        log_path = os.path.join(tmp, "routing_log.jsonl")
        rows = [
            {"timestamp": "2026-09-01T00:00:00Z", "session_id": "s1", "source": "plugin",
             "user_input": canary, "level": "high", "matched_keywords": ["保密"],
             "matched_rules": [{"id": "high-default", "level": "high"}],
             "inherited": False, "topic_shift": False, "effective_level": "high"},
            {"timestamp": "2026-09-01T00:01:00Z", "session_id": "s1", "source": "plugin",
             "user_input": canary, "level": "none", "matched_keywords": [],
             "inherited": True, "topic_shift": False, "effective_level": "high"},
            {"timestamp": "2026-09-01T00:02:00Z", "session_id": "s2", "source": "cli",
             "user_input": canary, "level": "medium", "matched_keywords": ["预算"],
             "inherited": False, "topic_shift": True, "effective_level": "medium"},
        ]
        with open(log_path, "w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        rj = run_py([os.path.join(tools_tmp, "stats.py"), "--json", "--log", log_path],
                    tmp, timeout=60)
        try:
            st = json.loads(rj.stdout.decode("utf-8", "replace").strip())
        except Exception:
            st = None
        if rj.returncode != 0 or not st:
            problems.append("stats --json 调用失败：exit=%s" % rj.returncode)
        else:
            if st.get("total") != 3:
                problems.append("stats 总数错：期望 3，实得 %s" % st.get("total"))
            if st.get("levels", {}).get("high") != 1:
                problems.append("stats 级别分布错：high 期望 1，实得 %s" % st.get("levels"))
            if st.get("inherited") != 1 or st.get("topic_shift") != 1:
                problems.append("stats 继承/话题切换计数错：%s/%s"
                                % (st.get("inherited"), st.get("topic_shift")))
            if st.get("with_rule_trace") != 1:
                problems.append("stats 规则级溯源覆盖数错：期望 1，实得 %s"
                                % st.get("with_rule_trace"))
            detail.append("stats 聚合正确")

        rt = run_py([os.path.join(tools_tmp, "stats.py"), "--log", log_path], tmp, timeout=60)
        text_out = rt.stdout.decode("utf-8", "replace")
        if canary in text_out:
            problems.append("隐私哨兵被触发：stats 的文本输出里出现了 user_input！")
        else:
            detail.append("stats 未泄漏 user_input")

        if problems:
            record("可观测工具", "FAIL", "; ".join(problems))
        else:
            record("可观测工具", "PASS", "；".join(detail))
    except Exception as e:
        record("可观测工具", "FAIL", "调用异常: %s" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. 插件检查 ────────────────────────────────────────────

def check_plugin():
    if not os.path.isfile(PLUGIN_PATH):
        record("插件检查", "FAIL", f"插件不存在: {PLUGIN_PATH}")
        return
    node = shutil.which("node")
    if not node:
        record("插件检查", "WARN", "本机 PATH 没有 node，跳过语法/导出契约检查（opencode 内置运行时仍会加载插件）")
        return

    problems = []
    try:
        r = subprocess.run([node, "--check", PLUGIN_PATH], capture_output=True, timeout=30)
        if r.returncode != 0:
            problems.append("node --check 语法失败")
    except Exception as e:
        problems.append(f"node --check 异常: {e}")

    # 导出契约：桌面端只认"导出本身是函数"（module.exports = fn）
    script = ("const m = require(process.argv[1]); "
              "if (typeof m !== 'function') { "
              "console.error('Plugin export is not a function'); process.exit(3) }")
    try:
        r = subprocess.run([node, "-e", script, PLUGIN_PATH], capture_output=True, timeout=30)
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip() or "未知"
            problems.append(f"导出契约失败: {err}")
    except Exception as e:
        problems.append(f"导出契约检查异常: {e}")

    if problems:
        record("插件检查", "FAIL", "; ".join(problems))
    else:
        record("插件检查", "PASS", "语法 OK，导出为函数（满足桌面端加载契约）")


# ── 5. 状态文件 ────────────────────────────────────────────

def check_state():
    if not os.path.isfile(STATE_PATH):
        record("状态文件", "PASS", "无状态文件（多轮继承从 none 开始）")
        return
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
        active = []
        for sid, v in data.items():
            lv = v.get("level") if isinstance(v, dict) else None
            if lv not in ("none", "medium", "high"):
                record("状态文件", "FAIL", f"会话 {sid} 的 level 非法: {lv!r}")
                return
            if lv != "none":
                active.append(f"{sid}={lv}")
        if active:
            record("状态文件", "WARN",
                   f"存在非 none 继承状态: {', '.join(active)}"
                   "（同会话后续消息会继承；若疑似引擎失败残留，清空该文件即可）")
        else:
            record("状态文件", "PASS", "格式合法，无遗留继承")
    except Exception as e:
        record("状态文件", "FAIL", f"解析失败: {e}")


# ── 6. Ollama 连通性 ───────────────────────────────────────

def check_ollama(cfg, gcfg):
    proj, glob = provider_view(cfg, gcfg)
    provider = proj or glob
    try:
        base = (provider.get("options") or {})["baseURL"]
    except Exception:
        record("Ollama 连通性", "FAIL", "项目+全局配置里都没有 provider.ollama.options.baseURL")
        return
    try:
        model = cfg["agent"]["worker"]["model"]
    except Exception:
        record("Ollama 连通性", "WARN", "跳过：worker agent 配置缺失")
        return
    if not model.startswith("ollama/"):
        record("Ollama 连通性", "WARN", f"跳过：worker.model 非 ollama: {model!r}")
        return
    tag = model.split("/", 1)[1]
    api = base.rsplit("/v1", 1)[0] + "/api/tags"
    try:
        with urllib.request.urlopen(api, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name") for m in data.get("models", []) if isinstance(m, dict)]
    except Exception as e:
        record("Ollama 连通性", "WARN",
               f"不可达（{e}）—— @worker 将无法工作；检查 NAS 电源/网络/ollama 服务。云端模型不受影响")
        return
    if tag in names:
        record("Ollama 连通性", "PASS", f"{api} 在线，模型 {tag} 存在")
    else:
        record("Ollama 连通性", "FAIL", f"服务器在线但模型 {tag} 不存在，现有: {names}")


# ── 7. 回归测试 ────────────────────────────────────────────

def check_tests():
    if not os.path.isfile(TEST_ROUTES):
        record("回归测试", "FAIL", f"test_routes.py 不存在: {TEST_ROUTES}")
        return
    try:
        r = run_py([TEST_ROUTES], CANON_DIR, timeout=180)
    except Exception as e:
        record("回归测试", "FAIL", f"调用异常: {e}")
        return
    lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
    last = lines[-1] if lines else ""
    if r.returncode == 0:
        record("回归测试", "PASS", last)
    else:
        record("回归测试", "FAIL", f"退出码 {r.returncode}，末尾输出: {last}")


# ── 7.5 网关回归（传输层门禁）──────────────────────────────

def check_gateway():
    """网关回归测试。

    这是 v6 的核心机制：**不依赖任何 Agent 框架**的强制层。它一旦坏了，
    "哪些数据能出内网"就重新变回口头承诺——所以每次体检都要跑。
    重点守的是 fail-closed 的方向性：本地上游不可达必须 502，绝不能改走云端。
    """
    path = _first_existing(os.path.join(CANON_DIR, "test_gateway.py"),
                           os.path.join(PROJECT_ROOT, "test_gateway.py"))
    if not os.path.isfile(path):
        record("网关回归", "FAIL", f"test_gateway.py 不存在: {path}")
        return
    try:
        r = run_py([path], CANON_DIR, timeout=300)
    except Exception as e:
        record("网关回归", "FAIL", f"调用异常: {e}")
        return
    lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
    last = lines[-1] if lines else ""
    if r.returncode == 0:
        record("网关回归", "PASS", last)
    else:
        record("网关回归", "FAIL", f"退出码 {r.returncode}，末尾输出: {last}")


# ── 主流程 ─────────────────────────────────────────────────

def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("=" * 60)
    print("opencode 隐私门禁一键体检")
    print(f"项目根: {PROJECT_ROOT}")
    print("=" * 60)

    gcfg = load_global_config()
    cfg = check_config(gcfg)
    if isinstance(cfg, dict):
        check_provider_source(cfg, gcfg)
    check_engine()
    check_correct()
    check_correct_write()
    check_correct_actions()
    check_rules_schema()
    check_observability()
    check_plugin()
    check_state()
    if isinstance(cfg, dict):
        check_ollama(cfg, gcfg)
    check_tests()
    check_gateway()

    print("=" * 60)
    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "WARN"]
    passes = len(results) - len(fails) - len(warns)
    print(f"结果: {passes} 通过 / {len(fails)} 失败 / {len(warns)} 警告")
    if fails:
        print("以下检查失败，修复后再启动 opencode:")
        for name, _, msg in fails:
            print(f"  - {name}: {msg}")
        return 1
    print("全绿，可以放心启动 opencode。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
