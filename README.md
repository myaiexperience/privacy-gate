# privacy-gate

端云协同里的数据出境门禁：**哪些数据能出内网，由代码决定，不由大模型自觉决定。**

> **在线试玩**（魔搭创空间，纯规则、不联网、零依赖）：
> <https://www.modelscope.cn/studios/xysrai/privacy-gate>
> 贴一段文本进去，看到的就是你把它部署到本地时会得到的同一个判定——页面跑的是本仓库未经简化的规则引擎。

> **English TL;DR** — A privacy gate for local AI agents (opencode + Ollama). The core claim: *"which data must never leave your intranet" should not depend on the LLM's self-discipline.* No fine-tuning, no extra compute: a deterministic keyword rule engine classifies every message (`none` / `medium` / `high`), an opencode plugin hard-blocks remote tools for sensitive levels, and a correction loop lets the user teach the engine new keywords with auto-generated regression cases. v5.1 adds hybrid routing: public tasks are delegated to a free cloud model (DeepSeek V4 Flash via opencode), sensitive tasks stay on your local model — **local brain routes, cloud hands do public work.** See [Quick start](#快速开始) and [Known limitations](#已知局限诚实清单) (the honest list — PRs welcome).

---

> 一个跑在本地 **opencode + Ollama** 上的 AI 智能体隐私门禁。
> 核心主张：**"哪些数据不能出内网"这件事，不该依赖大模型的自觉。**

不训练模型、不加算力、没有 GPU 预算——只用一段确定性规则引擎 + 一个用户纠正回流闭环，
让隐私路由在每次使用时**自己变准**。

v5.1 起支持**本地 + 云端混合**：公开任务委派云端免费模型（opencode 自带 DeepSeek V4 Flash），
敏感任务留在本地——"本地脑路由 + 云手执行公开活"。
这就是本项目在**端云协同**里的位置：分流由门禁决定，不由模型自己选。

---

## 为什么要做这个

本地部署的 AI 智能体（agent）经常需要联网：搜文档、抓网页、查资料。
但用户的需求里混着大量不该离开内网的数据——合同、薪酬、客户名单、源码、密钥……

常规做法是写进 prompt 让模型"自觉遵守"。实测结论是：**模型会在长对话里漂移，prompt 是承诺，不是门禁。**

这个项目把隐私分级从"模型承诺"升级为"框架强制"，并让它具备学习能力：

| 需求 | 常见方案 | 本项目的方案 |
|---|---|---|
| 判断数据敏感度 | 大模型自评 / 训练路由模型 | 代码层确定性关键词规则（零延迟、零显存） |
| 拦截外发 | prompt 约束 | opencode 插件在工具执行前硬拦截（throw） |
| 规则越用越准 | 训练/微调 | 用户纠正 → 关键词入库 + 回归用例自动生成 |
| 多轮追问防降级 | 无 / 模型记忆 | 会话级级别继承 + 话题切换检测 |
| 公开任务提速省钱 | 本地硬扛 / 全上云 | 混合路由：none 委派云端免费档，medium/high 留本地 |
| 可观测 | 无 | 每次路由决策落 jsonl 日志 |

## 架构（v5.1 混合）

```
用户输入
  │
  ▼
┌─ opencode 插件 .opencode/plugins/privacy-gate.js ──────────┐
│  ① 消息到达 → 运行 tools/rules_engine.py（带会话继承状态）    │
│  ② 注入标注：[系统隐私检测] effective_level=...              │
│  ③ 远程工具调用前裁决：high=硬拦 / medium=禁抓取 / none=放行  │
│  ④ 云端护栏：云模型收到非 none 内容 → 抛错拦截（best-effort） │
└──────────────────────────┬───────────────────────────────┘
                           │ （插件缺失时：worker 按 prompt 自行运行引擎兜底）
                           ▼
              @worker（ollama 本地模型，primary）
         none → 委派 @cloud ｜ medium/high → 本地一站式执行
                           │
                           ▼
              @cloud（opencode DeepSeek V4 Flash 免费档）
               只接公开任务；收到敏感内容立即拒绝并引导 @worker
                           │
                           ▼
                用户纠正 → tools/correct.py
                漏标关键词入库 + 回归用例生成 + 纠正记录
```

### 隐私级别

| 级别 | 判定 | 执行位置 | 约束 |
|---|---|---|---|
| `none` | 未命中关键词 | 可委派 @cloud | 所有工具可用（远程自动放行） |
| `medium` | 命中 medium 关键词 | 仅本地 @worker | 禁 webfetch；websearch 交用户看搜索词裁决 |
| `high` | 命中 high 关键词 | 仅本地 @worker | 禁所有远程访问，数据不出域 |

**fail-closed**：规则没命中但可能涉密 → 默认按 medium；引擎调用失败 → 也按 medium（只拦远程工具，不锁死聊天）。

### 四层防线

1. **框架强制层**：`tool.execute.before` 硬拦截（throw）——最可靠
2. **权限裁决层**：`permission.ask` 按级别自动 deny/allow（opencode 该 hook 历史上有 bug，只作增强，不作唯一防线）
3. **云端护栏层**：cloud agent / 云模型收到非 none 内容 → 消息层抛错（best-effort；真正保险的是"敏感任务只用 @worker"）
4. **模型兜底层**：worker prompt 要求手动运行引擎 + 默认保守策略（插件完全失效时仍生效）

## 快速开始

### 1. 跑通规则引擎（独立于 opencode，任何环境都能用）

```bash
# 依赖：Python 3.9+，无第三方包
python tools/rules_engine.py --json --log --stdin <<'EOF'
帮我写一份保密协议
EOF
# → {"level": "high", "matched_keywords": ["保密"], "effective_level": "high", ...}
```

### 2. 接入 opencode

1. 复制 `opencode.jsonc.example` → `opencode.jsonc`，填你的 Ollama 地址与模型名
2. `.opencode/plugins/privacy-gate.js` 会被自动加载（v1 插件 API；CJS 格式：CLI 的 Bun 与桌面版的 Node sidecar 都能加载），无需在 config 里配置
3. 启动 opencode 后，发一条含"保密"的消息，模型应收到 `[系统隐私检测] effective_level=high` 标注
4. 让 agent 尝试 websearch/webfetch → 应被插件拒绝

**桌面版（OpenCode Desktop）提示：**

- 模型选择器读**全局配置**：建议把 provider 块放进 `~/.config/opencode/opencode.jsonc`（项目内就不用重复定义）。本仓库示例保留 provider 块是为了 CLI 开箱即用，两处并存时用 `python check.py` 的"Provider 单一来源"检查盯漂移
- 某些桌面版本有已知 GUI bug：配置里的自定义模型不显示在模型列表（上游 issue 见文末）。**不影响使用**——直接把 agent 切成 `@worker`，它的模型固定绑定本地 Ollama
- 云端护栏在桌面端生效：主模型是云模型（如 DeepSeek）时，medium/high 内容会被插件拒绝并引导回 @worker

### 3. 让规则自我进化（能加，也能收）

用户说"这个也算机密"时，worker 会调用：

```bash
python tools/correct.py <<'EOF'
{"user_input": "帮我看看股权对赌条款", "level": "high", "keyword": "股权对赌", "correction_type": "privacy_underestimate"}
EOF
```

效果：`股权对赌` 进规则表 → 回归用例自动生成 → 以后同类请求直接命中。

**收窄同样重要**——误伤比漏检更常见。v5.1 只能追加，删不掉误伤的词；现在有四种动作：

```bash
# 整个删掉（公开行情讨论被误拦）
python tools/correct.py <<'EOF'
{"action": "remove", "keyword": "底价", "note": "公开行情讨论被误拦"}
EOF

# 降级：从 high 挪到 medium
{"action": "demote", "keyword": "收购", "to": "medium"}

# 豁免：只在特定语境下不拦，其他语境照常保护
{"action": "exempt", "keyword": "收购", "when": ["新闻", "公告", "公开报道"]}
```

豁免是**模式级**的：它只掐掉点名的那个词，同一条规则里的其他词不受影响。
所以"帮我写保密协议，顺便看看收购的新闻"里，「保密」照常拦——这不是细节，
是实现里踩过的一个安全漏洞（见 [DECISIONS.md](DECISIONS.md) D14）。

### 4. 让规则可解释、让日志会说话

写了 200 条规则之后，你迟早要问"这段话**为什么**被判 high""我配的豁免为什么没生效"：

```bash
python tools/explain.py "帮我写保密协议，顺便看看收购的新闻"
```

会逐条列出：哪条规则命中、命中了哪个模式、哪条豁免生效并掐掉了什么、
以及**哪条豁免"触发了但没有可豁免的命中"**（配错了却不会报错的典型情况）。

```bash
python tools/stats.py            # 决策日志统计：级别分布、继承收益、最吵的规则与关键词
```

`stats` **只输出聚合量，绝不打印 user_input**——日志里含用户原文，那是给本机排查用的，
不是给报告用的。这条已经写成带哨兵的自动化测试。

### 5. 一键体检（改完必跑）

```bash
python check.py
```

覆盖本项目历史上踩过的所有坑：配置解析（JSONC + prompt 引用 + 模型映射）、引擎中文端到端
（含 Windows 管道编码崩溃路径）、correct.py（循环导入 + 零副作用）、规则契约 lint、
收窄路径（含"豁免不得溢出"安全回归）、可观测工具（含 user_input 泄漏哨兵）、
插件语法与导出契约、状态文件（假 medium 残留）、Ollama 连通性、三套回归测试，
以及**零第三方依赖断言**——一共 12 项。全绿再启动 opencode。

CI（`.github/workflows/ci.yml`）在 GitHub 镜像上跑 2 个操作系统 × 3 个 Python 版本。
要两个 OS 是因为这个项目有相当多**只在 Windows 上才暴露**的坑（管道编码、CRLF 与字节
一致性、Store 占位 python 的 stub），而这些坑历史上都是发布前才发现的——
矩阵把"发布前才发现"变成"提交时就发现"。

## v6：把门禁下沉到传输层（网关）

v5.1 的强制层是 opencode 插件——它依赖 hook 存在、语义不变、并且可靠。
代价是三次真实教训（D9 插件格式 / D11 durable event schema / D13 只读环境）
**全都来自"依赖别人的 hook"**。

v6 把它下沉到**所有 Agent 的公共必经之路**：LLM API 调用本身。

```
Agent ──► 127.0.0.1:8787/v1 ──┬─ none        ──► 云端上游
（任何 OpenAI 兼容客户端）      ├─ medium/high ──► 本地上游（改写 model + 摘掉远程工具）
                              └─ 或 403 拒绝  （block 策略）
```

```bash
python tools/gateway.py \
    --cloud-upstream https://api.example.com/v1 \
    --cloud-key-env MY_CLOUD_KEY \
    --local-upstream http://127.0.0.1:11434/v1 \
    --local-model "qwen3:35b" \
    --policy reroute
```

然后把客户端的 `base_url` 指向 `http://127.0.0.1:8787/v1`。
opencode 照旧能用，但它**不再是基座，只是适配器之一**。

| 机制 | 说明 |
|---|---|
| **无声重路由** | 敏感会话改写到本地上游并改写 `model`——模型自己都不知道被降级了 |
| **远程工具剥夺** | 敏感会话时把 `tools[]` 里的搜索/抓取工具**摘掉**。模型不是"被劝住"，是**没有能力调**。这条不依赖任何框架 hook，是整个 v6 里唯一一个框架绕不过的强制点 |
| **fail-closed 的方向性** | 本地上游不可达 → 502，**绝不回落到云端**。网络故障必须朝"更私密"的方向倒 |

响应头会带上判定，排查时不用猜：

```
x-privacy-gate: level=high policy=reroute inherited=true tools-stripped=web_search,web_fetch route=local session=e78d39a6
```

三种策略：`reroute`（默认，真管事）／`block`（403 拒绝）／
`annotate`（**纯演练**：不改路由、不剥夺工具，只注入标注——先用它看策略想怎么判，再决定要不要真让它管事）。

`GET /healthz` 会报告**会话键来源分布**：`fallback` 比例高说明客户端没带
`x-session-id`。单人用无所谓；多人共用一个网关实例时必须配上，否则会话会互相污染。

> **上游地址刻意不放进 `rules.json`。** 那是要提交、要分享、要被人抄走的**策略**文件；
> 把本机 Ollama 的内网 IP 写进去，正好违反本项目自己的发布纪律。
> 策略归策略，部署归部署。

## 目录结构

```
├── .opencode/plugins/privacy-gate.js  # 框架级门禁插件（四层防线的 1、2、3 层）
├── tools/rules_model.py               # 规则模型 v4：匹配原语 + 语境豁免 + lint（策略层契约）
├── tools/rules_engine.py              # 分级编排 + 多轮继承 + 决策日志
├── tools/session.py                   # 会话状态（网关与适配器共用的会话键推导 + 继承）
├── tools/gateway.py                   # ★ 传输层门禁：重路由 / 工具剥夺 / fail-closed（v6 核心）
├── tools/correct.py                   # 纠正回流（扩充 / 收窄 / 降级 / 豁免 + 回归用例）
├── tools/explain.py                   # 解释"这段话为什么被判成这个级别"
├── tools/stats.py                     # 决策日志统计（最吵的规则与关键词）
├── keywords/rules.json                # 规则与边界（单一来源；v4 契约，v3 自动转换）
├── keywords/test_cases.json           # 纠正回流生成的回归用例
├── prompts/worker.md                  # 本地执行 agent 的系统提示（含兜底路径 + 委派规则）
├── prompts/cloud.md                   # 云端 agent 的系统提示（非 none 即拒绝）
├── privacy_gate.py                    # 裸 CLI 入口：classify/explain/stats/correct/lint/gateway/doctor
├── adapters/                          # 适配器矩阵（网关之外的接法，含各自强度与验证状态）
│   ├── claude-code/                   #   PreToolUse / UserPromptSubmit hook
│   └── mcp/                           #   MCP stdio server（弱层，刻意只读）
├── check.py                           # 一键体检（活体项目/发布包双布局自适应）
├── test_routes.py                     # 分级回归测试
├── test_gateway.py                    # 网关回归测试（含"绝不回落云端"）
├── test_adapters.py                   # 适配器回归测试（MCP 协议 / hook 决策 / CLI 分发）
├── check_zero_deps.py                 # 零第三方依赖断言——卖点必须由机器守
├── README.en.md                       # 英文 README（GitHub 镜像用）
├── .github/workflows/ci.yml           # CI：2 个 OS × 3 个 Python 版本
├── data/                              # routing_log.jsonl / corrections.jsonl（运行时生成）
├── opencode.jsonc.example             # 配置模板（worker + cloud + provider 示例）
├── docs/                              # 设计文档（v1 方案 / v3 执行计划存档；v6 网关提案）
└── DECISIONS.md                       # 历次迭代决策日志（为什么砍、为什么留）
```

## 已知局限（诚实清单）

- **关键词法是子串匹配**：有误伤（如"收购"会拦掉公开新闻查询），靠 fail-closed 方向 + 用户显式升级缓解；升级频率可从决策日志统计
- **召回依赖人肉纠正**：没有关键词命中的涉密表达（"老王那份文件"）只能靠默认保守 + 用户纠正慢慢补
- **bash 的联网行为无法拦截**：只能约束 webfetch/websearch；用 bash 里的 curl 属于已知盲区
- **permission.ask 不可靠**：opencode 该 hook 有历史 bug 和回归记录，强制拦截只信 `tool.execute.before`
- **云端护栏是 best-effort**：消息层抛错拦截受 opencode 版本行为影响；真正保险的是"敏感任务只用 @worker"
- **插件字段名随版本漂移**：`chat.message` 的消息结构在不同 SDK 版本有差异；注入标注只改已有 part 的 text 字段、绝不新增 part（新增 part 缺 id/sessionID/messageID 会让桌面端消息保存失败）
- **单人 homelab 验证**：非生产级、非大规模测试，欢迎反馈和 PR

以上每一条，都是一块等人来补的"玉"。

## 设计演进

从 1.5B 小模型路由 → 纯规则引擎 → 门禁框架化 → 纠正回流 → 云端分层，六次迭代、
每次砍掉什么、留下什么、为什么——见 [DECISIONS.md](DECISIONS.md)。

## 相关上游 issue

桌面端自定义 provider 不显示等已知 GUI 问题，可关注：
[anomalyco/opencode#9581](https://github.com/anomalyco/opencode/issues/9581)、
[#25125](https://github.com/anomalyco/opencode/issues/25125)、
[#11032](https://github.com/anomalyco/opencode/issues/11032)。

## 仓库与反馈

- 主仓库（Gitee）：<https://gitee.com/playing-with-ai-x/privacy-gate>
- 英文 README：[README.en.md](README.en.md)（给 GitHub 镜像与国际读者）
- 在线演示（魔搭创空间）：<https://www.modelscope.cn/studios/xysrai/privacy-gate>
  —— 演示源码在魔搭侧单独维护（创空间的「文件」页可看），但其中的分级引擎与词表
  是按字节从本仓库同步的，`sync_engine.py --check` 做严格哈希校验，
  所以页面上的判定与本仓库跑出来的判定必然是同一个
- 早期设计存档在 [`docs/`](docs)，历次迭代的取舍见 [DECISIONS.md](DECISIONS.md)
- 问题与 PR：欢迎提 Issue。这是单人 homelab 项目，回复可能不快，但每条都会看——
  尤其是"诚实清单"里那几条该怎么补。

## License

[MIT](LICENSE) — 代码；文档同仓库，可自由引用（注明出处即可）。
