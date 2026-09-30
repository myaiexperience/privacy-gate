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
  4. 插件检查         node --check 语法 + "导出必须是函数"契约（桌面端加载要求）
  5. 状态文件         privacy-gate-state.json 合法、无引擎失败残留的假 medium
  6. Ollama 连通性    推理服务器可达 + 配置里的模型 tag 存在（不可达只告警，
                     不阻塞——云端模型不受影响）
  7. 回归测试         test_routes.py 全绿

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
        shutil.copy2(SHIM_CORRECT, os.path.join(tmp, "tools", "correct.py"))
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
    check_plugin()
    check_state()
    if isinstance(cfg, dict):
        check_ollama(cfg, gcfg)
    check_tests()

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
