# 项目规则（供协作者与 AI 助手阅读）

> 这份文件是**契约**，不是介绍。介绍看 [README.md](README.md)。
> 每条为什么是这样，看 [DECISIONS.md](DECISIONS.md)。

## 一句话

**门禁是确定的、可测的、可回归的；边界是使用者的。**
机制归我们，策略归你。前者必须由代码保证，后者由 `keywords/rules.json` 表达。

## 强制层在哪（v6 起，这一点变了）

| 层 | 强度 | 依赖对方配合 |
|---|---|---|
| `tools/gateway.py`（本地 OpenAI 兼容网关） | **强** | **否**——在 LLM API 必经路径上 |
| `.opencode/plugins/privacy-gate.js` | 中 | 是 |
| `adapters/claude-code/` | 中 | 是 |
| `adapters/mcp/` | **弱**（自愿调用） | 是 |

**不要把 hook 层的适配器当成唯一防线。** 每个平台的 hook 都有"没拦住"的历史
（opencode 的 `permission.ask`、Claude Code 的 #43407 / #39344 / #18312）。
适配器一律按"会失效"实现：内部出错时对远程工具返回 **deny**。

## 级别与行为

| 级别 | 触发 | 网关行为（reroute） | 工具 |
|---|---|---|---|
| none | 无命中 | 转发云端上游 | 全可用 |
| medium | 命中 medium 规则 | 转发本地上游 + 改写 model | 远程抓取类工具被**摘掉** |
| high | 命中 high 规则 | 同上 | 远程工具被**摘掉** |

- **默认保守**：规则没命中但可能涉密 → 按 medium
- **失败方向**：本地上游不可达 → 502，**绝不回落云端**。
  这是全项目最重要的一条不变式，改网关时不许破坏它（`test_gateway.py` 有三条 ★ 断言守着）
- **多轮继承**：上一轮 medium/high，本轮无命中且无话题切换 → 继承；
  命中但更低 → 取高；话题切换信号 → 重置
- **授权只能由人发起**：升降级走用户显式声明，输出带 `[审计]` 行；模型禁止自行升级

## 规则维护（v4 契约）

- 单一来源：`keywords/rules.json`，`schema: v4`；v3 文件读时自动转换
- **匹配原语可配**：`substring`（ASCII 自动词边界）/ `word` / `regex`
- **`action` 会被真正读取**，不再是装饰性字段
- **豁免是模式级的**：`applies_to: [{"rule": "r", "patterns": ["词"]}]`。
  ⚠️ 不要用规则级豁免去表达"某个词在某个语境下不拦"——那会把同规则里其他模式
  一起豁免掉，是安全漏洞（回归用例里有 ★ 守着）
- 改规则用 CLI，不要手改 JSON：

```bash
python privacy_gate.py correct <<'EOF'
{"action": "exempt", "keyword": "收购", "when": ["新闻", "公告"], "note": "公开新闻里的收购是误伤"}
EOF
```

四种动作：`add` / `remove` / `demote` / `exempt`。
**误伤比漏检更常见**，所以收窄路径（`remove`/`demote`/`exempt`）和扩充路径一样重要。

## 开发纪律

**改完必须跑，全绿才算完成：**

```bash
python check.py            # 13 项体检（含规则契约 lint、收窄路径、可观测哨兵、零依赖、泄露审计）
python privacy_gate.py lint --strict
```

涉及以下文件时尤其不能跳过：`keywords/rules.json`、`tools/*.py`、
`.opencode/plugins/privacy-gate.js`、`adapters/**`。

**三套回归各自守什么：**

| 文件 | 守什么 |
|---|---|
| `test_routes.py` | 分级、多轮继承、模式级豁免不溢出、日志写盘容错 |
| `test_gateway.py` | 重路由、工具剥夺、**fail-closed 方向性**、SSE 流式 |
| `test_adapters.py` | MCP 协议、hook 决策、CLI 分发、规则不可用时 fail-closed |

## 两条自我约束（D17）

1. **能被机器守的承诺，不要留在人的记忆里。**
   - "零第三方依赖" → `check_zero_deps.py`（AST 扫 import，动态判断是否标准库）
   - "发布前不暴露内网 IP / 主机名 / 业务关键词" → `check_no_leaks.py`
   - 示例里要写内网地址，请用 RFC 5737 文档段（`192.0.2.x` / `198.51.100.x` / `203.0.113.x`）
2. **审计工具不含秘密。** `check_no_leaks.py` 里只有通用类别；
   项目专属敏感词走 `leaks.local.txt`（已 gitignore）或 `PRIVACY_GATE_DENY_WORDS`。
   ——审计工具不该是秘密的副本。

## 写设计文档的方式

本会话里被实现推翻的设计细节已经有四处（豁免作用域、`annotate` 语义、
会话键推导、规则不可用时的 fail-closed）。所以：

**纸上的设计要靠使用来验证。** 写完一段设计，尽量先拿它写一条真规则、
跑一次真请求，再回来改文档。文档里保留"这里被实现纠正过"的痕迹，比抹平它更有用。

## 文件结构

```
├── tools/gateway.py                   # ★ 传输层门禁（v6 的强制层）
├── tools/rules_model.py               # 规则模型 v4：匹配原语 + 豁免 + lint
├── tools/rules_engine.py              # 分级编排 + 多轮继承 + 决策日志
├── tools/session.py                   # 会话状态（网关与适配器共用）
├── tools/correct.py                   # 纠正回流（add/remove/demote/exempt）
├── tools/explain.py                   # 解释为什么是这个级别
├── tools/stats.py                     # 决策日志统计（只输出聚合量）
├── adapters/                          # 适配器矩阵（每个都标了强度与验证状态）
├── .opencode/plugins/privacy-gate.js  # opencode 适配器
├── keywords/rules.json                # 规则与边界（v4 契约，单一来源）
├── keywords/test_cases.json           # 纠正回流生成的回归用例
├── prompts/worker.md, cloud.md        # opencode 侧的 agent 提示词
├── privacy_gate.py                    # 裸 CLI 入口
├── check.py                           # 一键体检（13 项）
├── check_zero_deps.py                 # 零第三方依赖断言
├── check_no_leaks.py                  # 泄露面审计
├── test_routes.py / test_gateway.py / test_adapters.py
├── .github/workflows/ci.yml           # 2 OS × 3 Python
├── docs/                              # 设计文档（v1 方案 / v3 计划存档 / v6 网关提案）
├── data/                              # 运行时日志（勿提交）
└── DECISIONS.md                       # 决策日志：为什么砍、为什么留
```
