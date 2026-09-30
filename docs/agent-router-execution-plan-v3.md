# Agent 路由调度 — 执行方案

> 通用 Agent 调度模板
> 基于 opencode 框架，使用函数调用链实现四阶段流水线（检索 → 分析 → 输出 → 校验）

---

## 目录

1. [项目结构](#1-项目结构)
2. [Agent 定义](#2-agent-定义)
3. [函数调用链流程](#3-函数调用链流程)
4. [各阶段 Prompt 设计](#4-各阶段-prompt-设计)
5. [关键词规则](#5-关键词规则)
6. [纠正反馈机制](#6-纠正反馈机制)
7. [边界情况处理](#7-边界情况处理)
8. [实现步骤](#8-实现步骤)

---

## 1. 项目结构

```
.agent-router/                          # 项目根目录
├── opencode.jsonc                      # opencode 配置文件
├── AGENTS.md                           # 项目规则说明文件
│
├── prompts/                            # 各 Agent 的 system prompt
│   ├── orchestrator.md                 # 路由调度中心
│   ├── retrieve.md                     # 检索 Agent
│   ├── analyze.md                      # 分析 Agent
│   ├── output.md                       # 输出 Agent
│   └── verify.md                       # 校验 Agent
│
├── data/                               # 数据存储
│   └── router_corrections.jsonl        # 用户纠正数据集
│
├── keywords/                           # 路由关键词配置
│   └── rules.json                      # 关键词规则表
│
└── tools/                              # 自定义工具（可选扩展）
    ├── router.js                       # 路由判定硬规则匹配
    └── correct.js                      # 纠正记录写入工具
```

---

## 2. Agent 定义

### 2.1 模型规划

| 角色 | 模型 | 用途 |
|---|---|---|
| **Orchestrator（路由层）** | `qwen2.5:1.5b` | 轻量模型做路由判定 + Q1 直出回答 |
| **执行层（Q2/Q3/Q4）** | `qwen3.6:35b` | 主模型处理检索、分析、输出、校验 |

### 2.2 配置文件（opencode.jsonc）

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama",
      "options": {
        "baseURL": "http://<你的Ollama服务器IP>:11434/v1"
      },
      "models": {
        "qwen3.6:35b": { "name": "qwen3.6:35b" },
        "qwen2.5:1.5b": { "name": "qwen2.5:1.5b" }
      }
    }
  },
  "agent": {
    "orchestrator": {
      "description": "路由调度中心，分析用户请求并按四象限分发任务",
      "mode": "primary",
      "model": "ollama/qwen2.5:1.5b",
      // 路由层用小模型，省资源
      "prompt": "{file:./prompts/orchestrator.md}",
      "permission": {
        "task": "allow",    // 允许调用子 Agent
        "read": "allow",
        "edit": "deny",     // 路由层不做文件修改
        "bash": "deny"
      }
    },
    "retrieve": {
      "description": "检索信息：搜索网页、查文件、读代码库",
      "mode": "subagent",
      "model": "ollama/qwen3.6:35b",
      "prompt": "{file:./prompts/retrieve.md}",
      "permission": {
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "webfetch": "allow",
        "websearch": "allow",
        "edit": "deny",
        "bash": "deny"
      }
    },
    "analyze": {
      "description": "分析检索结果并进行推理，制定方案",
      "mode": "subagent",
      "model": "ollama/qwen3.6:35b",
      "prompt": "{file:./prompts/analyze.md}",
      "permission": {
        "read": "allow",
        "edit": "deny",
        "bash": "deny"
      }
    },
    "output": {
      "description": "将分析结果转化为最终交付物",
      "mode": "subagent",
      "model": "ollama/qwen3.6:35b",
      "prompt": "{file:./prompts/output.md}",
      "permission": {
        "read": "allow",
        "write": "allow",  // 输出阶段可写文件
        "edit": "allow",
        "bash": "allow"    // 可执行命令（如运行代码）
      }
    },
    "verify": {
      "description": "校验输出质量：完整性、准确性、格式正确性",
      "mode": "subagent",
      "model": "ollama/qwen3.6:35b",
      "prompt": "{file:./prompts/verify.md}",
      "permission": {
        "read": "allow",
        "edit": "deny",    // 校验 Agent 只读
        "bash": "deny"
      }
    }
  }
}
```

**配置说明：**
- `orchestrator` 使用 1.5B 小模型做路由，速度快成本低
- 四个子 Agent 共享 35B 模型，但通过不同 prompt 控制行为
- 每个 Agent 的 permission 严格按职责划分，最小权限原则
- 用户可通过 `@retrieve`、`@analyze` 等手动调用子 Agent

---

## 3. 函数调用链流程

### 3.1 完整流程

```
用户输入
  │
  ▼
┌─────────────────────────────────────────────────┐
│ Step 0: Orchestrator 路由判定                     │
│                                                  │
│  ① 读取 keywords/rules.json 做硬规则匹配          │
│     ├── force_private 命中 → privacy = 5          │
│     ├── force_high_diff 命中 → difficulty = 5     │
│     ├── mark_low 命中 → 直接 Q1，跳过调用链        │
│     └── mark_divergent 命中 → divergent = true     │
│                                                  │
│  ② 硬规则未完全确定时，用自己的模型做软评估         │
│     输出：{ privacy: 1-5, difficulty: 1-5 }      │
│                                                  │
│  ③ 映射到象限                                    │
│     ├── Q1（低隐私+低难度）→ 直接回答              │
│     ├── Q2（低隐私+高难度）→ 走完整流水线          │
│     ├── Q3（高隐私+低难度）→ 走完整流水线          │
│     └── Q4（高隐私+高难度）→ 走完整流水线          │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Step 1: @retrieve 检索                            │
│                                                  │
│  ① 分析需要什么信息                               │
│  ② 按需顺序检索（先搜最快的，不够再补）             │
│     顺序示例：                                     │
│       a) 搜本地文件（glob/grep）                   │
│       b) 搜本地代码库                              │
│       c) 搜网页（websearch）                       │
│       d) 读指定URL（webfetch）                     │
│                                                  │
│  ③ 输出中间产物：                                 │
│     RetrievalResult {                             │
│       summary: "检索摘要",                        │
│       sources: [{ type, content, metadata }]      │
│     }                                             │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Step 2: @analyze 分析                             │
│                                                  │
│  ① 接收 Step 1 的 RetrievalResult                │
│  ② 理解检索结果，进行结构化推理                     │
│  ③ 如果 divergent == true：                       │
│     ├── 阶段一：发散（列出所有可能性）              │
│     └── 阶段二：收敛（筛选整合，形成方案）           │
│                                                  │
│  ④ 输出中间产物：                                 │
│     AnalysisResult {                              │
│       conclusion: "最终结论",                     │
│       options: [{ name, pros, cons }],           │
│       reasoning: "推理过程"                       │
│     }                                             │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Step 3: @output 输出                              │
│                                                  │
│  ① 接收 Step 2 的 AnalysisResult                 │
│  ② 生成最终交付物（代码 / 文档 / 报告 / 邮件）    │
│  ③ 输出中间产物：                                 │
│     FinalOutput {                                │
│       content: "最终内容",                       │
│       format: "code|doc|email|report",           │
│       files: [{ path, content }]  // 可选        │
│     }                                             │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ Step 4: @verify 校验                              │
│                                                  │
│  ① 接收 Step 3 的 FinalOutput + 原始用户请求      │
│  ② 校验维度：                                     │
│     ├── 完整性：是否覆盖所有需求点                 │
│     ├── 准确性：事实/逻辑是否正确                  │
│     ├── 格式：是否符合预期格式                     │
│     └── 安全性：是否包含敏感信息泄漏               │
│                                                  │
│  ③ 输出校验结果：                                 │
│     VerificationResult {                          │
│       status: "pass"|"minor_fix"|"major_rework", │
│       issues: [{ severity, description, fix }],  │
│       retry: N                                    │
│     }                                             │
└──────────────────────┬──────────────────────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
    ┌──────────┐           ┌──────────────┐
    │  pass    │           │ minor_fix     │
    │          │           │ or            │
    │ 返回用户  │           │ major_rework  │
    └──────────┘           └──────┬───────┘
                                  │
                       retry < 3 ─┤
                                  │
                          ┌───────┴───────┐
                          │ retry >= 3    │
                          │               │
                          │ 人工干预 ⚠    │
                          │ 返回当前结果   │
                          │ + 风险提示    │
                          └───────────────┘
```

### 3.2 Q1 简化流程

```
用户输入 → Orchestrator 判定为 Q1 → 直接回答
                                        │
                 (跳过 retrieve / analyze / output / verify)
```

### 3.3 重试逻辑

```
校验不通过时：
  minor_fix   → 打回 @output 重做（携带 issues 作为修正约束）
  major_rework → 打回 @analyze 重新分析

  每次重试 retry +1
  retry >= 3 时 → 不再重试，返回当前结果 + 提示人工复核
```

**函数调用链的协作方式（伪代码）：**
```
用户输入
  → orchestrator.判定(input)
    → Q1: 直接回答
    → Q2/Q3/Q4:
        result = retrieve.执行(input)
        result = analyze.执行(result)
        result = output.执行(result)
        result = verify.执行({ output: result, request: input })
        
        while result.status != "pass" and result.retry < 3:
          if result.status == "minor_fix":
            result = output.执行({ ...result, issues: result.issues })
          elif result.status == "major_rework":
            result = analyze.执行({ ...result, issues: result.issues })
          result = verify.执行({ output: result, request: input })
          
        if result.retry >= 3:
          return { content: "...", warning: "未通过校验，请人工复核" }
        else:
          return result.content
```

---

## 4. 各阶段 Prompt 设计

### 4.1 orchestrator.md — 路由判定

```markdown
## 角色
你是 Agent 路由调度中心，负责分析用户请求的隐私程度和难度，将任务分发给合适的子 Agent。

## 核心职责
1. 分析用户输入的隐私程度（1-5）和任务难度（1-5）
2. 将结果映射到四象限：Q1（直出）/ Q2（公开高难）/ Q3（隐私低难）/ Q4（隐私高难）
3. 对 Q1 任务直接回答；对 Q2/Q3/Q4 任务调用子 Agent 流水线
4. 检测用户的纠正意图（如"分类错了"），修正判定并记录

## 输出格式
你必须始终输出以下 JSON 格式，不要包含其他内容：
{
  "privacy": 1-5,
  "difficulty": 1-5,
  "divergent": true/false,
  "quadrant": "Q1|Q2|Q3|Q4",
  "reason": "判断依据说明"
}

## 评分标准

### 隐私程度（privacy）
- 1：完全公开知识（天气、新闻、百科、常识）
- 2：公开但需上下文（代码实现、技术解释、教程）
- 3：含个人数据（日程、笔记、邮件、个人文件）
- 4：敏感商业信息（财务数据、内部方案、客户信息）
- 5：绝密/合规受限（合同、薪酬、NDA、股权、商业秘密）

### 任务难度（difficulty）
- 1：事实性查询，单轮回答
- 2：简单推理，轻度上下文处理
- 3：多步推理，中等复杂度
- 4：复杂推理，需领域专业知识
- 5：专家级，创造性/战略性工作

### 发散模式（divergent）
- true：需要先发散再收敛（脑暴、创意、探索类）
- false：不需要

## 关键规则
- Q1 任务：直接回答，不调子 Agent
- Q2/Q3/Q4 任务：调用 @retrieve → @analyze → @output → @verify
- 用户说"分类错了"等纠正语句：修正判定，写入纠正数据集
```

### 4.2 retrieve.md — 检索

```markdown
## 角色
你是一个检索专家，负责收集用户所需的所有信息。

## 核心职责
1. 分析当前任务需要哪些信息
2. 按需顺序检索，先快后慢，先本地后远程
3. 合并所有检索结果，输出结构化摘要

## 检索顺序（优先使用前面的）
1. 搜本地文件（glob + grep）
2. 搜本地代码库
3. 搜本地文档
4. 搜索网页（websearch）
5. 抓取指定 URL（webfetch）

## 输出格式
{
  "summary": "所有检索结果的摘要",
  "sources": [
    { "type": "file|web|code", "path": "来源路径", "content": "相关内容", "metadata": {} }
  ]
}

## 注意事项
- 不要编造信息，如果找不到就如实说
- 优先使用本地来源（隐私任务禁止远程搜索）
```

### 4.3 analyze.md — 分析

```markdown
## 角色
你是一个分析专家，负责理解检索结果并进行推理。

## 核心职责
1. 理解检索结果
2. 进行结构化推理
3. 制定方案或得出结论

## 发散模式（当 divergent=true 时）
分为两个阶段：
- 阶段一（发散）：列出尽可能多的思路和可能性，不筛选
- 阶段二（收敛）：筛选整合，去掉重复和不合理的，形成最终方案

## 输出格式
{
  "conclusion": "最终结论",
  "options": [
    { "name": "方案名称", "pros": "优点", "cons": "缺点", "confidence": 0-1 }
  ],
  "reasoning": "推理过程"
}
```

### 4.4 output.md — 输出

```markdown
## 角色
你是一个内容生成专家，负责将分析结果转化为最终交付物。

## 核心职责
1. 根据分析结果生成完整的最终输出
2. 确保内容质量高、格式规范
3. 如果需要生成文件，使用 write/edit 工具

## 输出格式
根据不同任务类型输出：
- 代码：完整可用，带必要注释
- 文档：结构清晰，层次分明
- 报告：数据准确，分析到位
- 邮件：得体规范

## 注意事项
- 输出必须完整，不要省略
- 如果接收到 issues（来自校验阶段的修改意见），请按意见修正
```

### 4.5 verify.md — 校验

```markdown
## 角色
你是质量校验专家，负责检查最终输出的质量。

## 校验维度
1. 完整性：是否覆盖用户的所有需求点
2. 准确性：事实是否正确、逻辑是否自洽
3. 格式：是否符合预期的输出格式
4. 安全性：是否包含不应泄露的敏感信息
5. 可执行性（代码）：是否能直接运行

## 输出格式
{
  "status": "pass|minor_fix|major_rework",
  "issues": [
    {
      "severity": "critical|major|minor",
      "description": "问题描述",
      "suggestion": "修改建议"
    }
  ],
  "retry": 当前重试次数
}

## 重试规则
- minor_fix：打回 output 重新生成
- major_rework：打回 analyze 重新分析
- retry >= 3：标记为人工干预，返回当前结果

## 注意事项
- 严格校验，不要放过明显的问题
- 校验失败时给出具体建议，帮助修正
```

---

## 5. 关键词规则

### 5.1 rules.json

```json
{
  "force_private": {
    "description": "命中后 privacy 直接设为 5，覆盖模型评分",
    "keywords": [
      "保密", "合同", "薪酬", "NDA", "内部", "股权",
      "机密", "商业秘密", "未公开", "内部资料",
      "严禁外传", "confidential", "不公开"
    ]
  },
  "force_high_diff": {
    "description": "命中后 difficulty 直接设为 5，覆盖模型评分",
    "keywords": [
      "形式化验证", "形式化证明", "架构设计",
      "编译器", "操作系统内核", "分布式共识",
      "formal verification", "proof assistant"
    ]
  },
  "boost_privacy": {
    "description": "命中后 privacy +2",
    "keywords": [
      "财务", "法务", "客户数据", "投融资",
      "审计", "尽调", "合规", "员工信息",
      "用户隐私", "财务报表", "诉讼", "收购"
    ]
  },
  "boost_diff": {
    "description": "命中后 difficulty +2",
    "keywords": [
      "分布式", "优化", "推理", "证明", "战略",
      "分析", "对比", "实现", "设计模式",
      "系统设计", "性能调优", "并发"
    ]
  },
  "mark_divergent": {
    "description": "命中后 divergent 设为 true，走发散→收敛通道",
    "keywords": [
      "脑暴", "创意", "探索", "还有什么",
      "可能性", "想象", "发散",
      "brainstorm", "ideate", "creative"
    ]
  },
  "mark_low": {
    "description": "命中后直接归为 Q1，跳过所有后续 Agent",
    "keywords": [
      "是什么", "怎么用", "查询", "翻译",
      "天气", "新闻", "今天", "科普",
      "百科", "定义", "列举", "有哪些"
    ]
  }
}
```

### 5.2 优先级规则

```
force_private > force_high_diff > mark_divergent > boost_* > mark_low
```

- 高优先级规则覆盖低优先级规则的结果
- 同优先级多条规则命中时，取最高值
- force_* 类覆盖模型评分，boost_* 类在模型评分基础上加减

### 5.3 冲突处理示例

| 输入 | 命中 | 结果 |
|---|---|---|
| "帮我写一份股权分配方案" | force_private + boost_diff | privacy=5, difficulty+2 |
| "脑暴一个形式化验证方案" | force_high_diff + mark_divergent | force_high_diff 优先，privacy 不变，difficulty=5，divergent=true |
| "翻译一篇技术文档" | mark_low + boost_diff | mark_low 优先，直接 Q1 |

---

## 6. 纠正反馈机制

### 6.1 触发方式

用户主动触发的自然语言纠正，如：
- "这次分类错了"
- "这个问题涉及机密"
- "股权这个关键词应该标记为隐私"

### 6.2 纠正流程

```
用户输入纠正语句
  │
  ▼
Orchestrator 识别纠正意图
  ├── 提取纠正类型（privacy/difficulty/divergent 偏低或偏高）
  ├── 提取漏标的关键词
  └── 写入纠正数据集
        │
        ▼
  下次路由判定时，检索相似历史纠正
  作为 few-shot 注入 model prompt
```

### 6.3 数据集格式（data/router_corrections.jsonl）

```jsonl
{"user_input":"帮我写一份股权分配方案","privacy_original":2,"difficulty_original":3,"privacy_corrected":5,"difficulty_corrected":4,"missed_keywords":["股权"],"correction_type":"privacy_underestimate","timestamp":"2026-07-18T12:00:00Z"}
{"user_input":"设计一个分布式数据库","privacy_original":1,"difficulty_original":3,"privacy_corrected":1,"difficulty_corrected":5,"missed_keywords":["分布式"],"correction_type":"difficulty_underestimate","timestamp":"2026-07-18T12:05:00Z"}
```

### 6.4 数据集使用

- **Few-shot 注入**：每次路由判定前，检索 top-3 相似纠正记录，注入路由模型的 prompt
- **关键词自动扩充**：从 missed_keywords 中提取关键词，自动追加到对应规则表中

---

## 7. 边界情况处理

| 场景 | 策略 |
|---|---|
| **子任务拆分** | 不拆分，按最高隐私分 + 最高难度分整体路由，由执行层大模型自行处理多任务 |
| **多轮对话** | 每轮独立重新评估，但上轮 privacy>=4 且无话题切换信号时，本轮保持 privacy>=3 |
| **话题切换** | 用户明确切换话题（"换个话题""不谈这个了"）时，重置隐私继承状态 |
| **隐私泄漏风险** | force_private 规则硬拦截，不经模型评估，确保隐私任务不会误判为公开 |
| **三振出局** | retry>=3 后返回当前结果 + "未通过自动校验，请人工复核"提示 |
| **用户中断** | 各阶段执行完整，不丢弃中间产物，可断点续传 |
| **关键词冲突** | 按优先级 force_private > force_high_diff > mark_divergent > boost_* > mark_low |

---

## 8. 实现步骤

> 按顺序执行，每步完成后验证再进入下一步。

### Step 1：创建项目目录

```bash
mkdir -p .agent-router/prompts
mkdir -p .agent-router/data
mkdir -p .agent-router/keywords
mkdir -p .agent-router/tools
```

### Step 2：写 keywords/rules.json

创建关键词规则文件，定义六类关键词及对应行为。

```bash
# 将第 5 节的 rules.json 写入 keywords/rules.json
```

### Step 3：写 prompts/orchestrator.md

路由判定的 system prompt，定义评分标准、四象限映射、纠正检测逻辑。

```bash
# 将第 4.1 节的 prompt 写入 prompts/orchestrator.md
```

### Step 4：写 prompts/retrieve.md

检索 Agent 的 system prompt，定义按需顺序检索规则。

```bash
# 将第 4.2 节的 prompt 写入 prompts/retrieve.md
```

### Step 5：写 prompts/analyze.md

分析 Agent 的 system prompt，定义推理结构和发散→收敛两阶段逻辑。

```bash
# 将第 4.3 节的 prompt 写入 prompts/analyze.md
```

### Step 6：写 prompts/output.md

输出 Agent 的 system prompt，定义交付物生成规范。

```bash
# 将第 4.4 节的 prompt 写入 prompts/output.md
```

### Step 7：写 prompts/verify.md

校验 Agent 的 system prompt，定义校验维度和三振出局逻辑。

```bash
# 将第 4.5 节的 prompt 写入 prompts/verify.md
```

### Step 8：创建 data/router_corrections.jsonl

```bash
# 创建空文件，写入 jsonl 格式说明注释
```

### Step 9：写 opencode.jsonc

整合所有 Agent 定义、模型配置、权限设置。

```bash
# 将第 2.2 节的配置写入 opencode.jsonc
```

### Step 10：写 AGENTS.md

项目规则说明文件，让 opencode 理解项目结构和协作方式。

### Step 11：初始化并测试

```bash
cd .agent-router
opencode /init     # 初始化项目
opencode           # 启动测试
```

### 验证清单

| 验证项 | 方法 |
|---|---|
| 路由能否正确分类 Q1/Q2/Q3/Q4 | 输入不同难度/隐私度的请求，检查路由结果 |
| 关键词规则是否生效 | 输入含"股权""保密"等词，检查是否强制高隐私 |
| 函数调用链是否完整 | Q2+ 任务是否走完 retrieve→analyze→output→verify |
| 三振出局是否生效 | 让校验 Agent 持续不通过，检查 retry>=3 时是否提示人工干预 |
| 纠正反馈是否记录 | 输入"分类错了"，检查纠正数据是否写入 jsonl |
| 多轮对话隐私继承 | 先问隐私问题，再问普通问题，检查 privacy 是否保持 |

---

## 附录：快速参考

### 常用命令

```bash
# 初始化项目
opencode /init

# 启动 TUI
opencode

# 手动调用子 Agent
@retrieve 帮我查一下这个文件
@analyze 分析一下上面的结果
```

### 关键设计决策日志

| 决策 | 选择 | 原因 |
|---|---|---|
| 框架 | opencode 原生 | 不需要额外依赖，Agent 能力已满足 |
| 通信方式 | 函数调用链 | 简单直接，好理解好维护 |
| 路由模型 | 1.5B 小模型 | 速度快，省资源，路由不需要强推理 |
| 执行模型 | 35B 模型 | 兼顾质量与本地部署可行性 |
| 子任务拆分 | 不拆分，整体路由 | 避免复杂度过高 |
| 校验重试 | 三振出局 | 防止无限循环，暴露给用户做最终判断 |
| 数据集使用 | few-shot 注入 | 不需要训练，零成本起步 |
