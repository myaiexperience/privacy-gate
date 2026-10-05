#!/usr/bin/env python3
"""
privacy-gate Claude Code 适配器（hook 脚本）

接入方式见同目录 settings.example.json。它处理两个事件：

  UserPromptSubmit  用户提交提示词 → 分级、更新会话状态、把标注注入上下文
  PreToolUse        工具即将执行 → 按级别裁决远程工具（deny / allow）

★ 定位声明：**这是增强层，不是唯一防线。**
--------------------------------------------------
Claude Code 的 `PreToolUse` hook 社区里有一串"hook 没能拦住"的 issue
（例如 anthropics/claude-code#43407「exit 2 + deny JSON 都没能阻止工具执行」、
#39344「permissionDecision=ask 静默覆盖 permissions.deny」、
#18312「工具在白名单里时 permissionDecision 被忽略」）。这与 opencode 的
`permission.ask` 历史 bug 是同一类问题。

结论和 D4 一样：**hook 层的强制力取决于对方的实现细节，不能当成唯一防线。**
真正的强制层是 `tools/gateway.py`——它在 LLM API 的必经路径上，框架绕不过。
本 hook 的价值是：覆盖**不经过网关**的路径（比如你只用了 Claude Code 而没配网关），
以及在网关之外多一层纵深。

★ fail-closed 的实现细节
--------------------------------------------------
hook 脚本抛异常时，Claude Code 看到的只是非零退出（非 2），**不会拦截**——
也就是说崩溃 = 放行 = fail-open。所以这里把整个流程包在 try/except 里，
内部出错时对远程工具一律 **deny**。

环境变量：
  PRIVACY_GATE_RULES          规则文件（默认 ../../keywords/rules.json）
  PRIVACY_GATE_LOG            决策日志（默认 ../../data/routing_log.jsonl）
  PRIVACY_GATE_STATE          会话状态文件（默认 ../../data/claude_sessions.json）
  PRIVACY_GATE_REMOTE_TOOLS   远程工具名模式，逗号分隔（覆盖内置默认）

零第三方依赖。
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.abspath(os.path.join(_HERE, "..", "..", "tools"))
sys.path.insert(0, _TOOLS)

import rules_engine  # noqa: E402
import rules_model  # noqa: E402
import session as session_mod  # noqa: E402

ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
RULES_PATH = os.environ.get("PRIVACY_GATE_RULES", os.path.join(ROOT, "keywords", "rules.json"))
LOG_PATH = os.environ.get("PRIVACY_GATE_LOG", os.path.join(ROOT, "data", "routing_log.jsonl"))
STATE_PATH = os.environ.get(
    "PRIVACY_GATE_STATE", os.path.join(ROOT, "data", "claude_sessions.json"))

# 刻意**不**直接复用 rules.json 里的 remote_tool_patterns：
# 那些模式（*web* / *fetch* / *search*…）是给 OpenAI 风格的 web_search / web_fetch 用的，
# 套到 Claude Code 上会把 mcp__memory__search_nodes 这类本地工具也一并拦掉。
# 工具名是平台相关的——这正是"工具剥夺按名匹配"这条局限的具体体现。
DEFAULT_REMOTE_TOOLS = ["WebFetch", "WebSearch",
                        "mcp__*__*fetch*", "mcp__*__*search*"]


def remote_patterns(rules):
    env = os.environ.get("PRIVACY_GATE_REMOTE_TOOLS", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    return list(DEFAULT_REMOTE_TOOLS)


def is_remote(tool_name, patterns):
    import fnmatch
    low = str(tool_name or "").lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


# 兜底时要用到"这次到底是哪个事件、什么工具"。main 一读到输入就记下来。
_EVENT = {}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def deny(reason):
    """拒绝一个工具调用。

    除了打印 JSON，还**退出码 2**。官方文档把两条路都写了：

      * "exit 0 + 打印 JSON" 是结构化控制的正路；
      * 但 "If your hook is meant to enforce a policy, use `exit 2`" ——
        退出 2 在能拦的事件上**无论有没有 JSON 都会拦**。

    社区 issue（anthropics/claude-code#43407 等）里有"exit 2 + deny JSON 都没能阻止
    执行"的记录，所以这里两者都给：JSON 给理由，退出码给强制力。文档明确说混用是允许的，
    且退出 2 的拦截效果不会被 JSON 取消——**多给一个不会更差**。
    """
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})
    sys.exit(2)


def escalate(reason):
    """门禁自己判断不了时，把决定权**交回给用户**。

    为什么不是 deny：连输入都读不懂时，我们不知道这是不是远程工具；一刀切拒绝会把
    Read / Edit 这类本地工具也挡住，整个会话不可用——那不是保守，那是坏掉。

    为什么不是"什么都不输出"：官方文档写着"exit 0 且无输出 = **没有决定**，工具调用
    照常走权限流程"，也就是**放行**。`ask` 让用户当场决定，方向仍然是"不替你放行"。
    """
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }})


def allow(reason=""):
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": reason,
    }})


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        # 读不懂输入 = 不知道是什么工具、什么事件。
        # 这里以前是 `return 0`（不输出任何决策）——按官方文档那等于**放行**，
        # 而适配器 README 写着"内部出错时对远程工具返回 deny"。两者必须改一个：
        # 这里选改代码，因为"门禁坏了就放行"正是这个项目最不能有的方向。
        try:
            sys.stderr.write("[privacy-gate] hook 读不懂输入: %s\n" % e)
        except Exception:
            pass
        escalate("[privacy-gate] hook 读不懂输入，无法判断是否该放行，请你决定")
        return 0
    if not isinstance(event, dict):
        escalate("[privacy-gate] hook 收到非对象输入，无法判断是否该放行，请你决定")
        return 0
    _EVENT.clear()
    _EVENT.update(event)

    name = event.get("hook_event_name")
    session_id = str(event.get("session_id") or "unknown")

    # 规则不可用 = 门禁失效。**必须 fail-closed。**
    # 注意这里要同时覆盖两种情形：文件不存在、以及文件存在但解析失败。
    # 第一版只处理了解析失败，"文件不存在"会得到一个 rules=None 的空规则库，
    # 于是新会话被判成 none 并放行——那是 fail-open，正是最不能有的方向。
    rules, rules_error = None, None
    try:
        if not os.path.isfile(RULES_PATH):
            rules_error = "规则文件不存在：%s" % RULES_PATH
        else:
            rules = rules_model.load_rules(RULES_PATH)
    except Exception as e:
        rules_error = "规则文件读取失败：%s" % e

    if rules_error:
        if name == "PreToolUse" and is_remote(event.get("tool_name"), DEFAULT_REMOTE_TOOLS):
            deny("[privacy-gate] %s，按 fail-closed 拒绝远程工具" % rules_error)
        elif name == "UserPromptSubmit":
            # 不拦提示词（门禁管的是数据外发，不是拒绝跟你说话），但要让人知道门禁是坏的
            emit({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "[privacy-gate] 门禁规则不可用（%s）——"
                                      "本会话无法分级，请检查配置。" % rules_error}})
        return 0

    store = session_mod.SessionStore(
        STATE_PATH, session_mod.canonical_topic_shift(rules))
    patterns = remote_patterns(rules)

    # ── 用户提交提示词：分级 + 更新会话 + 注入标注 ──
    if name == "UserPromptSubmit":
        prompt = str(event.get("prompt") or "")
        try:
            raw_level = rules_model.evaluate(prompt, rules)["level"]
        except Exception:
            raw_level = "medium"
        effective, prev, inherited, shift = store.effective(session_id, raw_level, prompt)
        store.set_level(session_id, effective)
        store.save()
        rules_engine.log_decision({
            "session_id": "claude:" + session_mod.key_digest(session_id),
            "source": "claude-code-hook",
            "user_input": prompt,
            "level": raw_level,
            "matched_keywords": [],
            "inherited": inherited,
            "topic_shift": shift,
            "effective_level": effective,
        }, LOG_PATH)
        note = ("[系统隐私检测] effective_level=%s matched=%s inherited=%s"
                % (effective, "、".join(rules_model.evaluate(prompt, rules)["matched_keywords"])
                   or "无", inherited))
        if effective != "none":
            note += ("。本会话已限制远程访问：远程工具会被 hook 拒绝。"
                     "如需解除，请由用户明确授权。")
        emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": note}})
        return 0

    # ── 工具即将执行：按会话级别裁决远程工具 ──
    if name == "PreToolUse":
        tool = event.get("tool_name")
        if not is_remote(tool, patterns):
            return 0  # 非远程工具不管，直接放行
        level = store.level_of(session_id)
        if level == "high":
            deny("[privacy-gate] 会话级别=high，已拒绝远程工具 %s" % tool)
        elif level == "medium":
            # 与 v5.1 一致：medium 禁抓取，搜索交用户裁决
            if "fetch" in str(tool).lower():
                deny("[privacy-gate] 会话级别=medium，已拒绝抓取类远程工具 %s" % tool)
            else:
                return 0  # 搜索类不自动放行，交给 Claude Code 自己的权限流程
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 走到这里说明连分级流程都没跑起来。**必须显式说出一个决策**：
        # 官方文档写着"exit 0 且不输出任何 JSON = 没有决定 → 工具调用照常走权限流程"，
        # 所以"静默退出"不是 fail-closed，**是 fail-open**。
        # （第一版这里就是静默 sys.exit(0)，而注释写着 fail-closed——注释骗了自己。）
        try:
            sys.stderr.write("[privacy-gate] hook 内部错误: %s\n" % e)
        except Exception:
            pass
        try:
            name = _EVENT.get("hook_event_name")
            if name == "UserPromptSubmit":
                # 不拦提示词：门禁管的是数据外发，不是拒绝跟你说话
                emit({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "[privacy-gate] hook 内部错误：%s——"
                                          "本会话无法分级，请检查配置。" % e}})
            elif is_remote(_EVENT.get("tool_name"), DEFAULT_REMOTE_TOOLS):
                # 能认出是远程工具 → 这就是 README 承诺的"内部出错时返回 deny"
                deny("[privacy-gate] hook 内部错误，按 fail-closed 拒绝：%s" % e)
            else:
                # 认不出工具（或不是 PreToolUse）→ 交回给用户决定，不替他放行
                escalate("[privacy-gate] hook 内部错误，无法判断是否该放行，请你决定：%s" % e)
        except SystemExit:
            raise
        except Exception:
            pass
        sys.exit(0)
