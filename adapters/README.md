# 适配器

同一个隐私门禁，接到不同的 Agent 基座上。

**这里没有"主基座"。** v5.1 把强制层绑在 opencode 的插件 hook 上，代价是三次真实教训
（[D9](../DECISIONS.md) 插件格式 / D11 durable event schema / D13 只读环境）**全都来自
"依赖别人的 hook"**。v6 把强制层下沉到传输层，opencode 于是降级成"适配器之一"。

## 矩阵（按强制强度排序，不按流行度）

| 适配器 | 强制强度 | 依赖对方配合 | 在本机验证过吗 | 说明 |
|---|---|---|---|---|
| **[网关](../tools/gateway.py)** | **强** | **否** | ✅ 38 条断言 + 真上游实测 | 在 LLM API 的必经路径上。远程工具剥夺 + 重路由 + fail-closed 都在这里。**这是唯一一个框架绕不过的强制点。** |
| [opencode 插件](../.opencode/plugins/privacy-gate.js) | 中 | 是（v1 插件 hook） | ✅ 语法/导出契约 + 真机使用 | 打标注 + 工具执行前硬拦截。网关缺失时它是主防线。 |
| [Claude Code hook](claude-code/) | 中 | 是（PreToolUse / UserPromptSubmit） | ⚠️ 协议逻辑✅（含 fail-closed 与退出码），**真机未测** | 覆盖不经过网关的路径。上游有一串"hook 没拦住"的 issue，所以定位为增强层。 |
| [MCP server](mcp/) | **弱**（自愿调用） | 是（模型得愿意调） | ✅ 协议层断言 | **只做可观测性，不要当门禁。** 刻意只读。 |
| 裸 CLI | — | — | ✅ | `python privacy_gate.py classify --stdin`，给脚本和管道用。 |

> "本机验证过吗"这一列区分**协议逻辑**与**真机接线**：前者能在没有对方的机器上跑，
> 后者不能。网关那一行之所以敢写"真上游实测"，是因为本地 llama.cpp 与魔搭推理 API
> 都真的接过（见 DECISIONS D24）。Claude Code 与 MCP 客户端这台机器上没有，
> 所以只能到协议层——**"没测过"要写出来，不能含糊**。

## 一句话选型

- **只想要一道真正拦得住的线** → 网关。其他都可以不配。
- **已经在用某个 Agent，想立刻有覆盖** → 加它对应的适配器，但知道它是增强层。
- **想让人在对话里查分级结果** → MCP。别指望它拦得住任何东西。

## 关于"hook 拦不住"这件事

这不是某一个平台的毛病，是这一类设计的通病。已知的上游问题（按报告的标题引用，
未在本机复现）：

- [anthropics/claude-code#43407](https://github.com/anthropics/claude-code/issues/43407)
  — PreToolUse 返回 exit 2 + deny JSON 都没能阻止工具执行
- [#39344](https://github.com/anthropics/claude-code/issues/39344)
  — permissionDecision=ask 静默覆盖 permissions.deny 规则
- [#18312](https://github.com/anthropics/claude-code/issues/18312)
  — 工具在 allow 白名单里时 permissionDecision 被忽略

opencode 的 `permission.ask` 也有历史 bug 和回归记录（见 [D4](../DECISIONS.md)）。

**所以本项目的立场是：hook 层永远按"会失效"来设计。**
具体做法有两条，都已实现：

1. **fail-closed**：hook 脚本内部出错时，对远程工具一律 **deny**。
   （因为 hook 抛异常在 Claude Code 里只是非零退出，**不会拦截**——崩溃 = 放行 = fail-open。
   所以本适配器把整个流程包在 try/except 里。）
2. **纵深**：真正要紧的路径由网关兜住，hook 只是不经过网关时的补位。

## 验证状态（诚实清单）

| 部分 | 验证到什么程度 |
|---|---|
| 网关 | 真机跑通，25 条断言含"本地上游挂了绝不回落云端" |
| MCP server | 真机跑通协议交互（initialize / tools/list / tools/call / 错误分支） |
| Claude Code hook | **协议逻辑**在真机跑通（喂 JSON 看输出的决策）；`settings.json` 的接法**未实测**——这台机器上没有 Claude Code |
| opencode 插件 | 语法与导出契约有自动化检查；真机使用过 |

第三行那句话是故意的：**没在自己机器上验证过的功能，不写进文档当成功案例。**
hook 的 JSON 输入输出我按官方文档与社区参考实现，并且把"怎么判断它有没有生效"
写在 [claude-code/README.md](claude-code/README.md) 里，你接上之后可以五分钟内验完。
