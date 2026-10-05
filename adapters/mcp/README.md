# MCP 适配器

把分级能力暴露成 MCP 工具，任何 MCP 客户端都能问"这句话是什么级别"。

> **定位：弱强制层，不是门禁。**
> MCP 工具是**客户端自愿调用**的——模型可以根本不调它，也可以调了之后无视结果。
> 它提供的是**可观测性**，不是拦截。
> 真正的强制层是[网关](../../tools/gateway.py)：它在 LLM API 的必经路径上，框架绕不过。
> **把 MCP 当成门禁会给人虚假的安全感**，那是本项目最不想做的事。

因为这个原因，本 server **刻意只读**——没有任何"改规则"的工具。
让一个可以被模型调用的接口去修改门禁规则，等于把门禁的钥匙挂在门上。
（改规则请用 `python privacy_gate.py correct ...`，那是人执行的。）

## 接入

```json
{
  "mcpServers": {
    "privacy-gate": {
      "command": "python",
      "args": ["/绝对路径/privacy-gate/adapters/mcp/privacy_gate_mcp.py"]
    }
  }
}
```

## 提供的工具

| 工具 | 作用 |
|---|---|
| `classify_text` | 文本 → 级别 / 命中词 / 生效的豁免；可选传入 `previous_level` 模拟多轮继承 |
| `list_rules` | 列出规则 id、级别、动作、模式数量（**不返回全部词**，避免污染上下文） |
| `explain_text` | 逐条解释为什么是这个级别，含"哪条豁免触发/空转" |

环境变量 `PRIVACY_GATE_RULES` 可指定别的规则文件。

## 手工测试（不需要 MCP 客户端）

```bash
# 初始化
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | python privacy_gate_mcp.py

# 列工具
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python privacy_gate_mcp.py

# 调工具
echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"classify_text","arguments":{"text":"帮我写一份保密协议"}}}' \
  | python privacy_gate_mcp.py
```

## 实现说明

- 传输：stdio，行分隔的 JSON-RPC 2.0
- 实现 `initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`
- `initialize` **回显客户端请求的 protocolVersion**：我们只用最基础的 tools 能力，
  回显比硬编码一个版本号更兼容（客户端会拒绝它不认识的版本）
- 工具内部异常回 `isError: true` 而不是让连接崩掉；协议层异常回 JSON-RPC 标准错误码
  （`-32700` 解析失败 / `-32600` 非法请求 / `-32601` 未实现 / `-32602` 参数错 / `-32603` 内部错误）
- stdout 只走协议，日志走 stderr

## 已知局限

- **它拦不住任何东西**（重复三遍也不多）。想要拦截，用网关。
- 协议交互有自动化断言，但**没有在真实 MCP 客户端里接起来验证过**——
  这台机器上没有装 MCP 客户端。上面的手工测试可以替代大半验证。
