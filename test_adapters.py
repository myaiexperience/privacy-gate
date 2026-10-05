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
    check("initialize 回显客户端协议版本",
          r1.get("protocolVersion") == "2025-03-26", str(r1.get("protocolVersion")))
    check("initialize 声明 tools 能力", "tools" in (r1.get("capabilities") or {}))
    check("initialize 返回 serverInfo",
          (r1.get("serverInfo") or {}).get("name") == "privacy-gate",
          str(r1.get("serverInfo")))
    check("通知不产生响应（只有 2 条响应）", len(resp) == 2, "收到 %d 条" % len(resp))

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

    # 输入读不懂时不该崩，也不该乱输出决策
    code, out, err = run([sys.executable, HOOK], "这不是 json", env=env)
    check("hook 收到非法输入不崩、不输出决策",
          code == 0 and not out.strip(), "exit=%s out=%r" % (code, out[:60]))


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
