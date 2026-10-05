#!/usr/bin/env python3
"""
适配器回归测试

测什么、不测什么，先说清楚：

  ✅ 能测的：协议逻辑本身
     - MCP：喂 JSON-RPC 行，看响应对不对（含错误分支）
     - Claude Code hook：喂 hook 事件 JSON，看它输出什么决策
     - 裸 CLI：子命令分发、lint、classify 管道
  ❌ 测不了的：与真实客户端的接线
     这台机器上没有 Claude Code、也没有 MCP 客户端，所以
     `adapters/claude-code/settings.example.json` 的接法**未实测**——
     适配器 README 里写明了这一点，并给了五分钟自查步骤。

这正是本项目一贯的界线：**协议逻辑必须自动化守住，没验证过的接线必须标出来。**

用法：python test_adapters.py
零第三方依赖。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(_HERE, "adapters", "mcp", "privacy_gate_mcp.py")
HOOK = os.path.join(_HERE, "adapters", "claude-code", "privacy_gate_hook.py")
CLI = os.path.join(_HERE, "privacy_gate.py")
RULES = os.path.join(_HERE, "keywords", "rules.json")

PASS = 0
FAIL = 0


def check(note, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", note, ("  " + extra) if extra else ""))


def run(cmd, stdin_text="", env=None, timeout=60):
    e = dict(os.environ)
    if env:
        e.update(env)
    # 文本模式：子进程是 UTF-8 协议流，字节/字符串别在这里混着用
    p = subprocess.run(cmd, input=stdin_text, capture_output=True, timeout=timeout,
                       env=e, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def rpc(lines):
    """把多行 JSON-RPC 喂给 MCP server，返回解析后的响应列表。"""
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)
    code, out, err = run([sys.executable, MCP], text)
    resp = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            resp.append(json.loads(line))
    return code, resp, err


# ── MCP ────────────────────────────────────────────────────

def test_mcp():
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    code, resp, err = rpc([init,
                           {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    check("MCP 进程正常退出", code == 0, "exit=%s stderr=%s" % (code, err[:120]))

    by_id = {r.get("id"): r for r in resp if "id" in r}
    r1 = by_id.get(1, {}).get("result") or {}
    check("initialize 对支持的版本回同一个（2025-03-26）",
          r1.get("protocolVersion") == "2025-03-26", str(r1.get("protocolVersion")))
    check("initialize 声明 tools 能力", "tools" in (r1.get("capabilities") or {}))
    check("initialize 返回 serverInfo",
          (r1.get("serverInfo") or {}).get("name") == "privacy-gate",
          str(r1.get("serverInfo")))
    check("通知不产生响应（只有 2 条响应）", len(resp) == 2, "收到 %d 条" % len(resp))

    # ★ 版本协商：规范要求"不支持就回一个自己支持的版本"，**不是回显**。
    #   （以前实现回显任何版本，等于宣称自己懂一个没读过的规范版本；
    #     而规范里"客户端若不支持服务端回的版本应当断开"这条保护正好被它废掉。）
    _, rv, _ = rpc([{"jsonrpc": "2.0", "id": 9, "method": "initialize",
                     "params": {"protocolVersion": "2099-01-01",
                                "capabilities": {}, "clientInfo": {"name": "future", "version": "1"}}}])
    got = ((rv[0] if rv else {}) or {}).get("result") or {}
    check("★initialize 不回显不支持的版本（规范 MUST）",
          got.get("protocolVersion") != "2099-01-01",
          "回的是 %s" % got.get("protocolVersion"))
    check("★initialize 回一个自己支持的版本",
          got.get("protocolVersion") in ("2025-06-18", "2025-03-26", "2024-11-05"),
          str(got.get("protocolVersion")))
    _, rn, _ = rpc([{"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}}])
    gotn = ((rn[0] if rn else {}) or {}).get("result") or {}
    check("initialize 没给版本时回最新支持的版本",
          gotn.get("protocolVersion") == "2025-06-18", str(gotn.get("protocolVersion")))

    r2 = by_id.get(2, {}).get("result") or {}
    names = sorted(t["name"] for t in (r2.get("tools") or []))
    check("tools/list 返回三个工具",
          names == ["classify_text", "explain_text", "list_rules"], str(names))
    check("每个工具都带 inputSchema",
          all("inputSchema" in t for t in (r2.get("tools") or [])))
    check("★ MCP 是只读的：没有任何改规则的入口",
          all(k in ("classify_text", "explain_text", "list_rules") for k in names))

    # 调工具
    code, resp, _ = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "classify_text",
                    "arguments": {"text": "帮我写一份保密协议"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "classify_text",
                    "arguments": {"text": "那第三条怎么改", "previous_level": "high"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "explain_text",
                    "arguments": {"text": "看看某公司收购的公开新闻"}}},
    ])
    by_id = {r.get("id"): r for r in resp}
    t1 = (by_id.get(1, {}).get("result") or {})
    check("classify_text 判出 high",
          "high" in json.dumps(t1, ensure_ascii=False), json.dumps(t1, ensure_ascii=False)[:80])
    t2 = (by_id.get(2, {}).get("result") or {})
    t2_text = json.dumps(t2, ensure_ascii=False)
    check("classify_text 支持多轮继承（prev=high 时生效级别仍为 high）",
          "上一轮 high" in t2_text and "生效 high" in t2_text,
          t2_text[:140])
    t3 = (by_id.get(3, {}).get("result") or {})
    check("explain_text 说明豁免生效",
          "豁免" in json.dumps(t3, ensure_ascii=False),
          json.dumps(t3, ensure_ascii=False)[:80])

    # 错误分支
    code, resp, _ = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "not_a_method"},
    ])
    by_id = {r.get("id"): r for r in resp}
    check("未知工具 → -32602",
          (by_id.get(1, {}).get("error") or {}).get("code") == -32602,
          str(by_id.get(1, {}).get("error")))
    check("未实现方法 → -32601",
          (by_id.get(2, {}).get("error") or {}).get("code") == -32601,
          str(by_id.get(2, {}).get("error")))

    code, out, _ = run([sys.executable, MCP], "{不是 json}\n")
    check("非法 JSON → -32700 且进程不崩",
          code == 0 and "-32700" in out, "exit=%s out=%s" % (code, out[:80]))


# ── Claude Code hook ───────────────────────────────────────

def hook(event, env):
    code, out, err = run([sys.executable, HOOK],
                         json.dumps(event, ensure_ascii=False), env=env)
    decision = None
    if out.strip():
        try:
            decision = json.loads(out)
        except Exception:
            decision = {"_unparsed": out}
    return code, decision, err


def test_hook(tmp):
    env = {"PRIVACY_GATE_RULES": RULES,
           "PRIVACY_GATE_LOG": os.path.join(tmp, "hook_log.jsonl"),
           "PRIVACY_GATE_STATE": os.path.join(tmp, "hook_state.json")}

    code, dec, err = hook({"hook_event_name": "UserPromptSubmit",
                           "session_id": "s1",
                           "prompt": "帮我写一份保密协议，涉及股权分配"}, env)
    ctx = ((dec or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
    check("UserPromptSubmit：注入标注且判为 high",
          code == 0 and "effective_level=high" in ctx, ctx[:100])

    code, dec, _ = hook({"hook_event_name": "PreToolUse", "session_id": "s1",
                         "tool_name": "WebFetch", "tool_input": {}}, env)
    pd = ((dec or {}).get("hookSpecificOutput") or {})
    check("PreToolUse：high 会话拒绝 WebFetch",
          pd.get("permissionDecision") == "deny", str(pd)[:120])
    check("拒绝理由说明了级别", "high" in str(pd.get("permissionDecisionReason", "")),
          str(pd.get("permissionDecisionReason"))[:80])

    code, dec, _ = hook({"hook_event_name": "PreToolUse", "session_id": "s1",
                         "tool_name": "Read", "tool_input": {}}, env)
    check("PreToolUse：非远程工具不管（无输出 = 不干预）", code == 0 and dec is None,
          str(dec)[:80])

    # 新会话 + 无害输入 → 不应该拦
    code, dec, _ = hook({"hook_event_name": "UserPromptSubmit", "session_id": "s2",
                         "prompt": "你好，帮我看看今天的天气"}, env)
    code, dec, _ = hook({"hook_event_name": "PreToolUse", "session_id": "s2",
                         "tool_name": "WebFetch", "tool_input": {}}, env)
    check("none 会话不拦远程工具（门禁不该无差别打扰）", dec is None, str(dec)[:80])

    # medium：禁抓取，搜索不自动放行（交回平台自己的权限流程）
    hook({"hook_event_name": "UserPromptSubmit", "session_id": "s3",
          "prompt": "整理一下这个月的销售数据"}, env)
    code, dec, _ = hook({"hook_event_name": "PreToolUse", "session_id": "s3",
                         "tool_name": "WebFetch", "tool_input": {}}, env)
    pd = ((dec or {}).get("hookSpecificOutput") or {})
    check("medium 会话拒绝抓取（WebFetch）", pd.get("permissionDecision") == "deny",
          str(pd)[:100])
    code, dec, _ = hook({"hook_event_name": "PreToolUse", "session_id": "s3",
                         "tool_name": "WebSearch", "tool_input": {}}, env)
    check("medium 会话不自动放行搜索（交回平台裁决）", dec is None, str(dec)[:80])

    # ★ fail-closed：规则文件坏掉时，远程工具必须被拒
    bad_env = dict(env)
    bad_env["PRIVACY_GATE_RULES"] = os.path.join(tmp, "not-there.json")
    code, dec, _ = hook({"hook_event_name": "PreToolUse", "session_id": "s1",
                         "tool_name": "WebFetch", "tool_input": {}}, bad_env)
    pd = ((dec or {}).get("hookSpecificOutput") or {})
    check("★规则文件不可用时 fail-closed：远程工具被拒",
          pd.get("permissionDecision") == "deny",
          str(pd)[:120])
    check("★fail-closed 的理由说清了原因",
          "fail-closed" in str(pd.get("permissionDecisionReason", "")),
          str(pd.get("permissionDecisionReason"))[:80])
    # ★ 而且 fail-closed 要落在**退出码**上，不能只靠 JSON。
    # 官方文档："exit 2" 在能拦的事件上无论有没有 JSON 都会拦，并且
    # "If your hook is meant to enforce a policy, use exit 2"；
    # 同时社区 issue（#43407 等）里有"exit 2 + deny JSON 都没能阻止执行"的记录。
    # 两者都给才是这道门禁该有的样子。
    check("★fail-closed 同时给出退出码 2（不只靠 JSON）",
          code == 2, "exit=%s" % code)

    # 输入读不懂时：**必须给出一个决策**，不能沉默。
    # 官方文档写着"exit 0 且无输出 = 没有决定 → 工具调用照常走权限流程"，
    # 也就是说**沉默等于放行**。
    # ⚠️ 这条断言以前写的是"不输出决策"——**它在守护 fail-open**。
    #    测试断言错方向，比没有测试更糟：它会让错误的行为看起来很稳。
    code_bad, out_bad, err_bad = run([sys.executable, HOOK], "这不是 json", env=env)
    try:
        dec_bad = json.loads(out_bad.strip()) if out_bad.strip() else None
    except Exception:
        dec_bad = None
    pd_bad = ((dec_bad or {}).get("hookSpecificOutput") or {})
    check("输入读不懂时必须给出决策，不能沉默（沉默 = 放行）",
          pd_bad.get("permissionDecision") in ("ask", "deny"),
          "exit=%s out=%r" % (code_bad, out_bad[:80]))
    check("输入读不懂时交给用户决定（不是一刀切拒绝，那会连本地工具一起挡掉）",
          pd_bad.get("permissionDecision") == "ask",
          str(pd_bad.get("permissionDecision")))
    check("输入读不懂时把原因写到了 stderr（人要看得到）",
          "读不懂输入" in (err_bad or "") or "读不懂输入" in (out_bad or ""),
          (err_bad or "")[:80])


# ── opencode 插件 ↔ 引擎 字段契约 ─────────────────────────

def test_plugin_engine_contract():
    """插件（JS）从引擎（Python）的 stdout 里读哪些字段？引擎必须都给到。

    这是本项目里最容易**静默失效**的一处接口：两边各有一半测试，
    而合起来的那条缝没人测——字段一改名，插件不报错，只是悄悄降级。

    所以这里**从插件源码里提取它实际读的字段名**，而不是在测试里抄一份清单：
    抄一份的话，插件加了新字段而引擎没提供，测试照样是绿的。

    另外守两条更基本的：
    - 引擎 stdout 必须是**纯 JSON**（插件直接 JSON.parse；往 stdout 多打一行就炸）
    - 拿不到可识别的级别时必须 fail-closed，不能当成 none
    """
    plugin = os.path.join(_HERE, ".opencode", "plugins", "privacy-gate.js")
    engine = os.path.join(_HERE, "tools", "rules_engine.py")
    if not (os.path.isfile(plugin) and os.path.isfile(engine)):
        check("插件↔引擎字段契约", False, "插件或引擎文件不存在")
        return

    src = open(plugin, encoding="utf-8").read()
    fields = sorted(set(re.findall(r"\bdata\.([A-Za-z_][A-Za-z0-9_]*)", src)))
    check("能从插件源码里提取出它读取的字段", bool(fields),
          "字段：" + ("、".join(fields) if fields else "（一个都没提取到）"))

    for text, want in (("帮我写一份保密协议", "high"), ("今天天气怎么样", "none")):
        r = subprocess.run(
            [sys.executable, engine, "--json", "--stdin"],
            input=text.encode("utf-8"), capture_output=True, timeout=60,
            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
        out = r.stdout.decode("utf-8", "replace")
        try:
            data = json.loads(out.strip())
        except Exception as e:
            check("引擎 stdout 是纯 JSON（%s）" % text[:6], False, str(e)[:90])
            continue
        missing = [f for f in fields if f not in data]
        check("引擎提供了插件读取的全部字段（%s）" % text[:6], not missing,
              ("缺：" + "、".join(missing)) if missing else "字段齐全")
        check("effective_level 可识别（%s）" % text[:6],
              data.get("effective_level") == want,
              "实得 %r，期望 %r" % (data.get("effective_level"), want))

    fb = re.search(r"function fallback\(\)\s*\{[^}]*level:\s*\"(\w+)\"", src)
    check("插件的兜底级别是 medium（fail-closed）",
          bool(fb) and fb.group(1) == "medium",
          ("兜底级别 = %s" % fb.group(1)) if fb else "没找到 fallback()")
    # 这条是 fail-open 的回归：曾经写成「不是 high/medium 就当作 none」，
    # 于是引擎字段改名 / 引擎打印 {"error": ...} 都会把远程工具全打开。
    has_guard = "LEVELS.has(data.effective_level)" in src
    check("插件不再把「看不懂的级别」当成 none（fail-open 回归）", has_guard,
          "识别到 LEVELS.has(...) 判定" if has_guard
          else "插件里找不到 LEVELS.has(data.effective_level)——fail-open 路径又回来了")


# ── 裸 CLI ─────────────────────────────────────────────────

def test_cli(tmp):
    code, out, _ = run([sys.executable, CLI, "version"])
    check("CLI version 可用", code == 0 and "privacy-gate" in out, out.strip())

    code, out, _ = run([sys.executable, CLI])
    check("CLI 无参数时打印用法", code == 0 and "子命令" in out, out[:60].replace("\n", " "))

    code, out, err = run([sys.executable, CLI, "nope"])
    check("CLI 未知子命令 → 退出码 2", code == 2, "exit=%s" % code)

    code, out, _ = run([sys.executable, CLI, "lint", "--json"])
    try:
        payload = json.loads(out)
    except Exception:
        payload = None
    check("CLI lint --json 可用且规则干净",
          code == 0 and payload and payload.get("ok") is True,
          (out[:120].replace("\n", " ") if out else ""))

    code, out, _ = run([sys.executable, CLI, "classify", "--json", "--stdin"],
                       "帮我写一份保密协议\n")
    try:
        level = json.loads(out).get("effective_level")
    except Exception:
        level = None
    check("CLI classify 走 stdin 管道判出 high", level == "high",
          "level=%s" % level)


def main():
    tmp = tempfile.mkdtemp(prefix="privacy-gate-adapters-")
    try:
        print("-" * 60)
        print("MCP 适配器")
        print("-" * 60)
        test_mcp()
        print("-" * 60)
        print("Claude Code hook 适配器")
        print("-" * 60)
        test_hook(tmp)
        print("-" * 60)
        print("opencode 插件 ↔ 引擎 字段契约")
        print("-" * 60)
        test_plugin_engine_contract()
        print("-" * 60)
        print("裸 CLI 适配器")
        print("-" * 60)
        test_cli(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 60)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
