#!/usr/bin/env python3
"""
privacy-gate MCP 适配器（stdio 传输）

把分级能力暴露成 MCP 工具，让任何 MCP 客户端都能问"这句话是什么级别"。

★ 定位声明：**这是弱强制层，不是门禁。**
--------------------------------------------------
MCP 工具是**客户端自愿调用**的——模型可以根本不调它，也可以调了之后无视结果。
所以它只提供**可观测性**：让 Agent 能在动手之前先问一句，
或者让人在对话里查询分级结果。

真正的强制层是 `tools/gateway.py`（在 LLM API 的必经路径上，框架绕不过）。
把 MCP 当成门禁会给人虚假的安全感——这是本项目最不想做的事。

因此这个 server 刻意**只读**：不提供任何"改规则"的工具。
让一个可以被模型调用的接口去修改门禁规则，等于把门禁的钥匙挂在门上。

协议：MCP over stdio（JSON-RPC 2.0，行分隔）
  实现 initialize / notifications/initialized / ping / tools/list / tools/call
零第三方依赖。

用法（在 MCP 客户端的配置里）：
  {
    "mcpServers": {
      "privacy-gate": {
        "command": "python",
        "args": ["/绝对路径/adapters/mcp/privacy_gate_mcp.py"]
      }
    }
  }
"""

import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.abspath(os.path.join(_HERE, "..", "..", "tools"))
sys.path.insert(0, _TOOLS)

import rules_model  # noqa: E402

# 本适配器支持（= 真的读过其规范、并只用其中最基础的 tools 能力）的协议版本。
#
# 规范原文（lifecycle）::
#   "If the server supports the requested protocol version, it MUST respond with the same
#    version. Otherwise, the server MUST respond with another protocol version it supports."
#
# ⚠️ 以前这里**回显客户端请求的任何版本**，注释还写着"回显是最兼容的做法"——那是错的：
# 它等于宣称自己懂一个没读过的规范版本。规范里客户端"若不支持服务端回的版本 SHOULD
# 断开"这条保护，正好被回显废掉了。
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]
PROTOCOL_FALLBACK = LATEST_PROTOCOL  # 旧名字，保留以免外部引用断掉
SERVER_INFO = {"name": "privacy-gate", "version": "0.1.0"}
RULES_PATH = os.environ.get(
    "PRIVACY_GATE_RULES",
    os.path.abspath(os.path.join(_HERE, "..", "..", "keywords", "rules.json")))

_RULES = None


def rules():
    """懒加载规则（加载失败不致命：返回空规则会让所有判定为 none，
    所以这里刻意让异常冒出去，由调用方转成 isError——见 tools_call）。"""
    global _RULES
    if _RULES is None:
        _RULES = rules_model.load_rules(RULES_PATH)
    return _RULES


TOOLS = [
    {
        "name": "classify_text",
        "description": ("判断一段文本的隐私级别（none / medium / high），"
                        "并说明命中了哪些词、哪些语境豁免生效。"
                        "注意：本工具是自愿调用的观测手段，不能当作强制门禁——"
                        "强制拦截在本地网关里做。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要判定的文本"},
                "previous_level": {
                    "type": "string", "enum": ["none", "medium", "high"],
                    "description": "上一轮的生效级别（模拟多轮对话继承）",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_rules",
        "description": "列出当前规则库的规则 id、级别、动作与模式数量（不返回全部词，避免污染上下文）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "explain_text",
        "description": "逐条解释为什么这段文本被判成这个级别：命中了哪条规则、哪个模式、"
                       "哪条豁免生效、哪条豁免空转。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


# ── 工具实现 ───────────────────────────────────────────────

def tool_classify(args):
    text = str(args.get("text") or "")
    prev = args.get("previous_level")
    result = rules_model.evaluate(text, rules())
    out = {
        "level": result["level"],
        "matched_keywords": result["matched_keywords"],
        "exemptions": [e.get("id") for e in result["exemptions"]],
    }
    if prev in rules_model.LEVELS:
        rank = rules_model.RANK
        effective = prev if rank[prev] > rank[result["level"]] else result["level"]
        shift = any(kw in text.lower()
                    for kw in (rules().get("topic_shift_keywords") or []))
        if shift:
            effective = result["level"]
        out["previous_level"] = prev
        out["effective_level"] = effective
        out["inherited"] = (effective != result["level"])
    lines = ["级别：%s" % out["level"]]
    lines.append("命中：%s" % ("、".join(out["matched_keywords"]) or "无"))
    if out["exemptions"]:
        lines.append("生效的豁免：%s" % "、".join(out["exemptions"]))
    if "effective_level" in out:
        lines.append("（上一轮 %s → 生效 %s）" % (prev, out["effective_level"]))
    lines.append("原始结果：" + json.dumps(out, ensure_ascii=False))
    return "\n".join(lines)


def tool_list_rules(_args):
    r = rules()
    rows = ["schema=%s  规则 %d 条 / 豁免 %d 条"
            % (r.get("schema"), len(r.get("rules") or []), len(r.get("exceptions") or []))]
    for rule in r.get("rules") or []:
        rows.append("- %s  level=%s action=%s type=%s 模式数=%d"
                    % (rule.get("id"), rule.get("level"), rule.get("action") or "-",
                       (rule.get("match") or {}).get("type"),
                       len((rule.get("match") or {}).get("patterns") or [])))
    for exc in r.get("exceptions") or []:
        scopes = exc.get("applies_to")
        rows.append("- [豁免] %s  demote_to=%s  作用域=%s"
                    % (exc.get("id"), exc.get("demote_to"),
                       "全部规则" if scopes is None else
                       "、".join(str(s.get("rule")) for s in scopes)))
    rows.append("远程工具模式：" + "、".join(r.get("remote_tool_patterns") or []))
    return "\n".join(rows)


def tool_explain(args):
    text = str(args.get("text") or "")
    r = rules()
    lowered = text.lower()
    rows = []
    for rule in r.get("rules") or []:
        hits = rules_model.match_hits(rule.get("match") or {}, text, lowered)
        rows.append("%s %s（%s）%s"
                    % ("[命中]" if hits else "[未命中]", rule.get("id"),
                       rule.get("level"), "、".join(hits) if hits else ""))
    for exc in r.get("exceptions") or []:
        when = rules_model.match_hits(exc.get("when") or {}, text, lowered)
        rows.append("%s 豁免 %s  触发条件命中：%s"
                    % ("[触发]" if when else "[未触发]", exc.get("id"),
                       "、".join(when) or "—"))
    result = rules_model.evaluate(text, r)
    rows.append("→ 最终级别：%s" % result["level"])
    return "\n".join(rows)


HANDLERS = {
    "classify_text": tool_classify,
    "list_rules": tool_list_rules,
    "explain_text": tool_explain,
}


# ── JSON-RPC ───────────────────────────────────────────────

def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def handle(msg):
    """返回要写回的响应 dict，或 None（通知不该有响应）。"""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        params = msg.get("params") or {}
        requested = params.get("protocolVersion")
        # 规范：支持客户端请求的版本就回同一个；不支持就回一个**自己支持的**版本
        # （应当是最新的那个）。**不能回显**——见 SUPPORTED_PROTOCOLS 上的说明。
        version = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        return reply(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # 通知不需要响应

    if method == "ping":
        return reply(msg_id, {})

    if method == "tools/list":
        return reply(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return reply(msg_id, error={"code": -32602,
                                        "message": "未知工具: %s" % name})
        try:
            text = fn(args if isinstance(args, dict) else {})
            return reply(msg_id, {"content": [{"type": "text", "text": text}],
                                  "isError": False})
        except Exception as e:
            # 工具内部错误：按 MCP 约定回 isError，而不是让整个连接崩掉
            return reply(msg_id, {
                "content": [{"type": "text",
                             "text": "privacy-gate 工具执行失败：%s" % e}],
                "isError": True,
            })

    if msg_id is None:
        return None  # 其他通知
    return reply(msg_id, error={"code": -32601, "message": "未实现的方法: %s" % method})


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            out.write(json.dumps(reply(None, error={"code": -32700,
                                                    "message": "解析失败: %s" % e}),
                                 ensure_ascii=False) + "\n")
            out.flush()
            continue
        if not isinstance(msg, dict):
            out.write(json.dumps(reply(None, error={"code": -32600,
                                                    "message": "必须是 JSON 对象"}),
                                 ensure_ascii=False) + "\n")
            out.flush()
            continue
        try:
            resp = handle(msg)
        except Exception:
            traceback.print_exc(file=sys.stderr)  # stderr 是给日志的，不污染协议流
            resp = reply(msg.get("id"), error={"code": -32603, "message": "内部错误"})
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
