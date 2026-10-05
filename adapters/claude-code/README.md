# Claude Code 适配器

一个 hook 脚本，覆盖"不经过网关"的路径。

> **定位：增强层，不是唯一防线。** 原因写在 [../README.md](../README.md)：
> Claude Code 的 hook 有一串"没拦住"的上游 issue，跟 opencode 的 `permission.ask`
> 是同一类问题。真正拦得住的是[网关](../../tools/gateway.py)。

## 接入

1. 把 [`settings.example.json`](settings.example.json) 里的 `hooks` 段合并进
   `.claude/settings.json`（项目级）或 `~/.claude/settings.json`（用户级）
2. 把 `command` 里的路径换成 `privacy_gate_hook.py` 的**绝对路径**
   （Windows 用正斜杠，或双反斜杠）
3. 重启 Claude Code 会话

它处理两个事件：

| 事件 | 做什么 |
|---|---|
| `UserPromptSubmit` | 给提示词分级 → 更新该会话的级别 → 把 `[系统隐私检测] effective_level=…` 注入上下文。**不拦提示词**——门禁管的是数据外发，不是拒绝跟你说话 |
| `PreToolUse` | 远程工具按会话级别裁决：`high` 一律 deny；`medium` 拒绝抓取类、搜索类交回 Claude Code 自己的权限流程；`none` 放行 |

## 五分钟验证它到底有没有生效

**别只看配置写对了没有——要看不该发生的事有没有发生。**

```bash
# 1) 起一个会话，先发一条敏感内容建立会话级别
你：帮我写一份保密协议，涉及股权分配
#    预期：回答里能看到 [系统隐私检测] effective_level=high

# 2) 让它联网
你：搜索一下这个条款的行业惯例
#    预期：WebSearch / WebFetch 被拒绝，理由是 privacy-gate 会话级别=high

# 3) 如果不该发生的事发生了（它照样联网了）
#    → 说明这个版本忽略 hook 的 permissionDecision。
#      本脚本**已经**同时给出 exit 2 与 deny JSON（见下），若仍然拦不住，
#      那就是上游的 bug（#43407 一类），不是配置问题。
```

第 3 步那句不是敷衍——**上游确实有"exit 2 + deny JSON 都没能拦住"的报告**。
所以本适配器**两条路都给**：JSON 负责给出理由，退出码负责给出强制力。
官方文档对这两条路的说法是：*"exit 0 + 打印 JSON"是结构化控制的正路，但
"If your hook is meant to enforce a policy, use `exit 2`"*——退出 2 在能拦的事件上
无论有没有 JSON 都会拦。混用是文档明确允许的，且退出 2 的拦截效果不会被 JSON 取消。

## 手工调试 hook 本身

不用起 Claude Code 也能看它怎么决策：

```bash
# 用户提交提示词
echo '{"hook_event_name":"UserPromptSubmit","session_id":"t1","prompt":"帮我写一份保密协议"}' \
  | python privacy_gate_hook.py

# 紧接着让它调远程工具（同一 session_id，应被拒）
echo '{"hook_event_name":"PreToolUse","session_id":"t1","tool_name":"WebFetch","tool_input":{}}' \
  | python privacy_gate_hook.py

# 非远程工具应放行（无输出）
echo '{"hook_event_name":"PreToolUse","session_id":"t1","tool_name":"Read","tool_input":{}}' \
  | python privacy_gate_hook.py
```

## 已知局限（诚实清单）

- **`settings.json` 的接法未在本机实测**——这台机器上没有 Claude Code。
  协议逻辑（喂 JSON 看决策）有自动化测试；配置接法按官方文档与社区参考实现，
  请你按上面的"五分钟验证"跑一遍。
- **工具名是平台相关的。** 本适配器刻意**不**直接复用 `rules.json` 里的
  `remote_tool_patterns`——那些模式（`*web*` / `*search*`…）是给 OpenAI 风格的
  `web_search` / `web_fetch` 用的，套到 Claude Code 上会把
  `mcp__memory__search_nodes` 这类**本地**工具也一并拦掉。
  默认只覆盖 `WebFetch` / `WebSearch` / `mcp__*__*fetch*` / `mcp__*__*search*`，
  可用 `PRIVACY_GATE_REMOTE_TOOLS` 覆盖。
- **会话状态与网关各自独立。** 两边按不同的键存（这里用 Claude 的 `session_id`，
  网关用请求头/`user` 字段派生），同时使用时可能出现级别不同步。
  想只维护一份状态，就只用网关。
- **hook 崩溃 = 放行——这是官方语义，所以必须显式拒绝。**
  官方文档写着"exit 0 且不输出任何 JSON = **没有决定**，工具调用照常走权限流程"，
  也就是**沉默等于放行**。所以本脚本在内部出错时**一定输出一个决策**：
  - 能认出是远程工具 → `permissionDecision: "deny"` + 退出码 2
  - 认不出工具（连输入都读不懂）→ `permissionDecision: "ask"`，把决定交回给你
    （一刀切 deny 会连 Read / Edit 这类本地工具一起挡掉，整个会话不可用——
    那不是保守，那是坏掉）
  - 事件是 `UserPromptSubmit` → 只注入标注，不拦提示词（门禁管的是数据外发）

  > 这三条以前**没做到**：except 分支里是静默 `sys.exit(0)`，而本文档却写着
  > "内部出错时一律 deny"；测试甚至有一条断言写着"收到非法输入**不输出决策**"，
  > **在守护 fail-open**。已修，并有断言守着（含退出码 2 本身）。
