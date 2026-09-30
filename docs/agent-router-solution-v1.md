# Agent 路由调度方案 v1

> 通用 Agent 调度模板：根据任务难度与隐私程度，将请求路由到不同模型处理。
> 支持用户纠错反馈与数据集积累，持续优化路由准确率。

---

## 目录

1. [整体架构](#1-整体架构)
2. [路由判定引擎](#2-路由判定引擎)
3. [关键词规则系统](#3-关键词规则系统)
4. [四象限与模型映射](#4-四象限与模型映射)
5. [发散任务处理](#5-发散任务处理)
6. [多轮对话](#6-多轮对话)
7. [子任务拆分策略](#7-子任务拆分策略)
8. [用户纠正反馈](#8-用户纠正反馈)
9. [数据集与持续优化](#9-数据集与持续优化)
10. [配置文件模板](#10-配置文件模板)

---

## 1. 整体架构

```
                    ┌──────────────────────────────────────┐
                    │            用户输入                   │
                    └────────────────┬─────────────────────┘
                                     │
                                     ▼
                    ┌──────────────────────────────────────┐
                    │          路由判定引擎                  │
                    │                                      │
                    │  ┌──────────┐   ┌──────────┐        │
                    │  │ 硬规则层  │   │ 软评估层  │        │
                    │  │ (关键词)  │   │ (路由模型) │        │
                    │  └────┬─────┘   └────┬─────┘        │
                    │       └──────┬───────┘              │
                    │              ▼                       │
                    │     ┌────────────────┐              │
                    │     │ 象限判定 & 分发  │              │
                    │     └────────────────┘              │
                    └────────────────┬─────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         │                           │                           │
         ▼                           ▼                           ▼
   ┌───────────┐             ┌─────────────┐            ┌───────────┐
   │  路由直出   │             │  模型执行层   │            │  发散通道   │
   │  (Q1)     │             │  (Q2/Q3/Q4)  │            │  (标记任务) │
   └───────────┘             └──────┬──────┘            └─────┬─────┘
                                    │                         │
                                    ▼                         ▼
                          ┌──────────────────┐      ┌──────────────────┐
                          │   结果返回用户     │      │ 小模型发散思路    │
                          └────────┬─────────┘      │ → 大模型收敛整合  │
                                   │                └────────┬─────────┘
                                   ▼                         │
                          ┌──────────────────┐              │
                          │ 用户是否纠正？    │◄─────────────┘
                          │ (主动触发)        │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │ 存入纠正数据集     │
                          │ (jsonl 追加写入)   │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │ 下次路由时注入     │
                          │ few-shot 样例      │
                          └──────────────────┘
```

---

## 2. 路由判定引擎

### 2.1 双通道判定流程

```
用户输入字符串
    │
    ├── 通道一：硬规则层
    │   │
    │   ├── 遍历关键词规则表
    │   ├── 匹配规则 → 立即修正隐私分/难度分/标记
    │   ├── force_private   → privacy = 5（覆盖软评估）
    │   ├── force_high_diff → difficulty = 5（覆盖软评估）
    │   ├── mark_divergent  → divergent = true
    │   └── mark_low        → 直接返回 Q1（跳过后续流程）
    │
    ├── 通道二：软评估层（仅当未被硬规则直接截断时执行）
    │   │
    │   ├── 路由模型（本地 1-3B 小模型）
    │   ├── 注入系统 prompt + few-shot 样例
    │   ├── 输出 JSON：
    │   │   {
    │   │     "privacy": 2,       // 1-5 隐私评分
    │   │     "difficulty": 3,    // 1-5 难度评分
    │   │     "divergent": false, // 是否需要发散模式
    │   │     "reason": "..."     // 判断依据
    │   │   }
    │   └── 路由模型也可直接回答 Q1 问题（直出模式）
    │
    └── 合成最终决策
        │
        ├── 硬规则 > 软评估（force_* 类覆盖模型输出）
        ├── boost_* 类在软评估基础上加减分
        └── 最终 (privacy, difficulty) → 映射到象限
```

### 2.2 判定优先级规则

```
force_private > force_high_diff > mark_divergent > boost_* > mark_low
```

- **force_private** 命中 → privacy 直接为 5，不信任模型输出
- **force_high_diff** 命中 → difficulty 直接为 5
- **mark_divergent** 命中 → divergent = true，走发散通道
- **boost_privacy** 命中 → privacy += 2
- **boost_diff** 命中 → difficulty += 2
- **mark_low** 命中 → 直接走 Q1，不再调用其他模型
- 同优先级规则都命中时，取最高值

### 2.3 隐私分 + 难度分 → 象限映射

| privacy \ difficulty | 1-2（低） | 3-5（高） |
|---|---|---|
| **1-2（公开）** | **Q1** — 路由直出 | **Q2** — 云端强模型 |
| **3-5（隐私）** | **Q3** — 本地中等模型 | **Q4** — 本地强模型 |

> 如果 `divergent == true`，无论落在哪个象限，先走发散通道再走对应模型。

---

## 3. 关键词规则系统

### 3.1 规则类型与内置词库

| 规则类型 | 效果 | 内置关键词（可扩展） |
|---|---|---|
| `force_private` | `privacy = 5`，强制隐私 | `保密`、`合同`、`薪酬`、`NDA`、`内部`、`股权`、`机密`、`商业秘密`、`未公开`、`内部资料`、`严禁外传`、`confidential`、`不公开` |
| `force_high_diff` | `difficulty = 5`，强制高难 | `形式化验证`、`形式化证明`、`架构设计`、`编译器`、`操作系统内核`、`分布式共识`、`formal verification`、`proof assistant` |
| `boost_privacy` | `privacy += 2`，提升隐私分 | `财务`、`法务`、`客户数据`、`投融资`、`审计`、`尽调`、`合规`、`员工信息`、`用户隐私`、`财务报表`、`诉讼`、`收购` |
| `boost_diff` | `difficulty += 2`，提升难度分 | `分布式`、`优化`、`推理`、`证明`、`战略`、`分析`、`对比`、`实现`、`设计模式`、`系统设计`、`性能调优`、`并发` |
| `mark_divergent` | `divergent = true`，走发散通道 | `脑暴`、`创意`、`探索`、`还有什么`、`可能性`、`想象`、`发散`、` brainstorm`、`ideate`、`creative`、`如果...会怎样` |
| `mark_low` | 直接 Q1，路由直出 | `是什么`、`怎么用`、`查询`、`翻译`、`天气`、`新闻`、`今天`、`科普`、`百科`、`定义`、`列举`、`有哪些` |

### 3.2 关键词匹配规则

```
匹配策略：
- 精确匹配：用户输入中包含完整关键词
- 否定排除：用户显式声明"不需要保密"→ 移除 privacy 标记
  （由路由模型识别，不做正则硬匹配，避免过于机械）
- 会话继承：上轮标记为隐私，本轮即使无关键词也不降级
```

### 3.3 冲突处理示例

| 输入 | 命中规则 | 冲突 | 结果 |
|---|---|---|---|
| "帮我写一份**股权**分配**方案**" | `force_private` + `boost_diff` | 无冲突，叠加 | privacy=5, difficulty+2=3 |
| "**脑暴**一个**形式化验证**方案" | `force_high_diff` + `mark_divergent` | 发散 vs 严谨 | `force_high_diff` 优先，走 Q2，但保留 divergent 模式 |
| "**翻译**一篇**技术文档**" | `mark_low` + `boost_diff` | 冲突 | `mark_low` 优先级高 |

---

## 4. 四象限与模型映射

| 象限 | 场景 | 调用模型 | 策略说明 |
|---|---|---|---|
| **Q1** | 日常问答、天气、查询、翻译 | 路由模型直出（1-3B） | 无需额外调用，低延迟 |
| **Q2** | 代码实现、架构设计、数学推理 | 云端强模型（如 Claude Sonnet、GPT-4o、DeepSeek-V3） | 注重推理能力与代码质量 |
| **Q3** | 个人日程、笔记、邮件、简单数据分析 | 本地中等模型（14B 级，如 Qwen2.5-14B、Llama-3-8B） | 兼顾隐私与效率 |
| **Q4** | 商业计划书、战略分析、法律合同 | 本地强模型（32B-70B 级，如 Qwen2.5-72B、Llama-3-70B） | 隐私优先，最强本地推理 |

### 模型调用配置示例

```yaml
models:
  Q1:
    provider: local
    model: qwen2.5-1.5b-instruct
    endpoint: http://localhost:11434/v1  # Ollama
    max_tokens: 512

  Q2:
    provider: cloud
    model: claude-sonnet-4-20260514
    endpoint: https://api.anthropic.com/v1
    max_tokens: 4096

  Q3:
    provider: local
    model: qwen2.5-14b-instruct
    endpoint: http://localhost:11434/v1
    max_tokens: 2048

  Q4:
    provider: local
    model: qwen2.5-72b-instruct
    endpoint: http://localhost:11434/v1
    max_tokens: 4096
```

---

## 5. 发散任务处理

### 5.1 触发条件

- 关键词规则匹配 `mark_divergent`
- 路由模型评分 `divergent == true`
- 用户输入含开放性问题（"还有什么方法？""有哪些可能性？"）

### 5.2 执行流程

```
用户输入（标记为发散）
    │
    ▼
┌───────────────────────────┐
│  阶段一：小模型发散          │
│  - 调用本地 1-3B 小模型     │
│  - Prompt：生成尽可能多的     │
│    思路/方案/可能性（重数量） │
│  - temperature ≥ 0.8       │
└───────────┬───────────────┘
            │
            ▼
┌───────────────────────────┐
│  阶段二：大模型收敛          │
│  - 将小模型产出作为上下文    │
│  - 调用象限对应的大模型      │
│  - Prompt：筛选、整合、优化   │
│    形成最终方案（重质量）    │
│  - temperature ≤ 0.3       │
└───────────┬───────────────┘
            │
            ▼
          返回结果
```

### 5.3 发散通道 Prompt 模板

**阶段一（发散）：**
```
用户的问题是：{user_input}

请尽可能多地列出不同的思路、方案或可能性。
不要做筛选和判断，重点是数量和多角度覆盖。
尽量从不同维度思考：技术、策略、时机、风险、替代方案等。
```

**阶段二（收敛）：**
```
用户的问题是：{user_input}

以下是初步发散得出的各种思路：
{divergent_output}

请基于以上思路，筛选出最合理/最可行的方案，整合成一份完整的回答。
你需要做的是：去重、归类、评估可行性、给出最终建议。
```

---

## 6. 多轮对话

### 6.1 每轮独立评估

- 每一轮用户输入都**重新经过路由判定引擎**
- 但路由模型的上下文包含：
  - 本轮用户输入
  - 前 1-3 轮的历史对话摘要（由路由模型自己维护）
  - 前一轮的路由判定结果

### 6.2 隐私标记继承规则

```
IF 前一轮 privacy >= 4
AND 本轮用户输入无明显话题切换信号（如"换个话题""不谈这个了"）
THEN privacy 保持 >= 3（不自动降级）

IF 前一轮 privacy <= 2
AND 本轮输入含隐私关键词
THEN 正常重新评估，隐私分按规则提升
```

### 6.3 话题切换检测

由路由模型判断用户是否切换话题。判断依据：

- 用户明确说 "换一个话题"、"不谈这个了"、"另外"、"还有"
- 当前轮输入与前几轮之间无实体/主题关联
- 路由模型输出中 `topic_shift` 字段为 true

### 6.4 对话历史注入格式

```
路由模型的系统 prompt 中包含：
---
对话历史摘要：
- 第1轮：用户问天气 → Q1，已回答
- 第2轮：用户问股权分配 → Q4，隐私分5，难度分4
- 当前用户输入的隐私继承状态：privacy >= 3（上轮隐私标记继承中）

当前输入："{user_input}"
---
```

---

## 7. 子任务拆分策略

### 7.1 原则

**不拆分子任务，以最高级别整体路由。**

一个请求包含多个子任务时，按最高隐私分 + 最高难度分整体路由。
拆分逻辑过于复杂且容易出错，交由执行层大模型自主处理。

### 7.2 示例

| 输入 | 子任务 | 路由策略 |
|---|---|---|
| "写一份商业计划书，顺便查一下明天天气" | Q4 + Q1 | 整体路由为 Q4 |
| "帮我翻译这段话，然后分析一下公司财报" | Q1 + Q4 | 整体路由为 Q4 |

### 7.3 执行层的自主拆分

大模型在回答时，如果发现请求包含多个独立任务，应自行：
1. 区分任务边界
2. 按顺序执行
3. 合并输出

> 如果未来需要支持子任务并行分发到不同模型，可扩展此模块，但当前版本不实现。

---

## 8. 用户纠正反馈

### 8.1 触发方式

**用户主动触发**，通过自然语言表达纠正意图。

触发句式示例：

| 触发句 | 含义 |
|---|---|
| "这次分类错了" | 通用纠正 |
| "这个问题涉及机密，不应该走云端" | 隐私分偏低 |
| "这个问题很简单，不需要大模型" | 难度分偏高 |
| "你应该先发散再收敛" | 漏标发散模式 |

### 8.2 路由模型解析纠正意图

```
用户纠正输入 → 路由模型解析
                │
                ├── 识别纠正类型：privacy / difficulty / divergent
                ├── 提取修正后的值（如 "隐私分应该是5"）
                ├── 提取漏标关键词（如 "股权这个关键词应该标记为隐私"）
                └── 输出结构化纠正记录
```

### 8.3 纠正记录数据结构

```json
{
  "id": "corr_20260718_001",
  "timestamp": "2026-07-18T12:00:00Z",
  "conversation_id": "conv_8a3f2b",
  "session_turn": 5,
  "user_input": "帮我写一份股权分配方案",

  "initial_decision": {
    "privacy": 2,
    "difficulty": 3,
    "quadrant": "Q2",
    "divergent": false,
    "matched_keywords": ["方案"]
  },

  "correction": {
    "type": "privacy_underestimate",
    "user_text": "这次分类错了，股权分配是商业机密",
    "corrected_privacy": 5,
    "corrected_difficulty": 4,
    "corrected_quadrant": "Q4",
    "missed_keywords": ["股权"],
    "false_positive_keywords": []
  }
}
```

### 8.4 纠正类型枚举

| correction_type | 含义 | 处理方式 |
|---|---|---|
| `privacy_overestimate` | 隐私分估高了 | 降低 privacy 值，检查误命中关键词 |
| `privacy_underestimate` | 隐私分估低了 | 提升 privacy 值，记录漏标关键词 |
| `difficulty_overestimate` | 难度分估高了 | 降低 difficulty 值 |
| `difficulty_underestimate` | 难度分估低了 | 提升 difficulty 值 |
| `divergent_missed` | 漏标发散模式 | divergent = true |
| `divergent_false` | 误标发散模式 | divergent = false |
| `general` | 通用纠正，无明确方向 | 记录为标准纠错样例，不做自动化修正 |

---

## 9. 数据集与持续优化

### 9.1 数据集格式

文件：`router_corrections.jsonl`，每行一个 JSON 对象。

```jsonl
{"user_input":"帮我写一份股权分配方案","raw":{"privacy":2,"difficulty":3},"corrected_privacy":5,"corrected_difficulty":4,"missed_keywords":["股权"],"correction_type":"privacy_underestimate","timestamp":"2026-07-18T12:00:00Z"}
{"user_input":"设计一个分布式数据库","raw":{"privacy":1,"difficulty":3},"corrected_privacy":1,"corrected_difficulty":5,"missed_keywords":["分布式"],"correction_type":"difficulty_underestimate","timestamp":"2026-07-18T12:05:00Z"}
```

### 9.2 数据集的两种使用方式

**方式一：Few-shot 注入（推荐，无需训练）**

```
每次路由判定前：
1. 计算当前输入与数据集中每条记录的文本相似度
2. 取 top-3 最相似的纠正记录
3. 格式化为 few-shot 样例，注入路由模型的系统 prompt
```

注入格式：

```
以下是之前用户纠正过的类似案例，请参考：

#1
用户输入：帮我写一份股权分配方案
路由错误：privacy=2 (偏低), difficulty=3
用户纠正：privacy=5, difficulty=4
原因：股权分配是商业机密

#2
用户输入：设计一个分布式数据库系统
路由错误：difficulty=3 (偏低)
用户纠正：difficulty=5
原因：分布式系统设计复杂度高
---
```

**方式二：关键词自动扩充**

每次纠正后，从 `missed_keywords` 字段中提取关键词，自动添加到对应的关键词规则中。

```
纠正记录中 missed_keywords: ["股权"]
→ 自动执行：keywords.force_private.append("股权")
```

### 9.3 数据集维护规则

| 操作 | 规则 |
|---|---|
| 写入 | 追加写入，不修改已有记录 |
| 去重 | 相同 `user_input` + `raw` 的记录只保留最新一条 |
| 过期 | 超过 90 天无使用的记录可归档（可选） |
| 导出 | 支持导出用于微调路由模型 |
| 清空 | 用户可自愿重置数据集 |

---

## 10. 配置文件模板

完整配置文件 `router-config.yaml`：

```yaml
# ============================================
# Agent 路由调度配置
# ============================================

router:
  # 路由模型配置
  model:
    provider: local
    endpoint: http://localhost:11434/v1
    model_name: qwen2.5-1.5b-instruct
    max_tokens: 256
    temperature: 0.1  # 路由判定需要确定性输出

  # few-shot 配置
  few_shot:
    enabled: true
    dataset_path: ./data/router_corrections.jsonl
    max_samples: 3
    similarity_threshold: 0.6

  # 硬规则配置
  hard_rules:
    force_private:
      enabled: true
      override_soft_score: true  # 命中后覆盖模型评分
      keywords:
        - 保密
        - 合同
        - 薪酬
        - NDA
        - 内部
        - 股权
        - 机密
        - 商业秘密
        - 未公开
        - 内部资料
        - 严禁外传
        - confidential
        - 不公开

    force_high_diff:
      enabled: true
      override_soft_score: true
      keywords:
        - 形式化验证
        - 形式化证明
        - 架构设计
        - 编译器
        - 操作系统内核
        - 分布式共识
        - formal verification
        - proof assistant

    boost_privacy:
      enabled: true
      boost_value: 2       # privacy += 2
      keywords:
        - 财务
        - 法务
        - 客户数据
        - 投融资
        - 审计
        - 尽调
        - 合规
        - 员工信息
        - 用户隐私
        - 财务报表
        - 诉讼
        - 收购

    boost_diff:
      enabled: true
      boost_value: 2       # difficulty += 2
      keywords:
        - 分布式
        - 优化
        - 推理
        - 证明
        - 战略
        - 分析
        - 对比
        - 实现
        - 设计模式
        - 系统设计
        - 性能调优
        - 并发

    mark_divergent:
      enabled: true
      keywords:
        - 脑暴
        - 创意
        - 探索
        - 还有什么
        - 可能性
        - 想象
        - 发散
        - brainstorm
        - ideate
        - creative

    mark_low:
      enabled: true
      keywords:
        - 是什么
        - 怎么用
        - 查询
        - 翻译
        - 天气
        - 新闻
        - 今天
        - 科普
        - 百科
        - 定义
        - 列举
        - 有哪些

  # 优先级（数字越小优先级越高）
  priority_order:
    - force_private        # 1
    - force_high_diff      # 2
    - mark_divergent       # 3
    - boost_privacy        # 4
    - boost_diff           # 5
    - mark_low             # 6

# 模型执行层配置
models:
  Q1:
    provider: local
    model: qwen2.5-1.5b-instruct
    endpoint: http://localhost:11434/v1
    max_tokens: 512
    temperature: 0.7

  Q2:
    provider: cloud
    model: claude-sonnet-4-20260514
    endpoint: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    max_tokens: 4096
    temperature: 0.3

  Q3:
    provider: local
    model: qwen2.5-14b-instruct
    endpoint: http://localhost:11434/v1
    max_tokens: 2048
    temperature: 0.5

  Q4:
    provider: local
    model: qwen2.5-72b-instruct
    endpoint: http://localhost:11434/v1
    max_tokens: 4096
    temperature: 0.3

# 发散通道配置
divergent_channel:
  enabled: true
  small_model:
    provider: local
    model: qwen2.5-1.5b-instruct
    endpoint: http://localhost:11434/v1
    max_tokens: 1024
    temperature: 0.8    # 发散任务需要高随机性

  large_model:
    provider: local
    model: qwen2.5-14b-instruct  # 或根据象限使用对应模型
    max_tokens: 2048
    temperature: 0.3    # 收敛任务需要低随机性

# 多轮对话配置
multi_turn:
  enabled: true
  max_history_turns: 5
  privacy_inheritance:
    enabled: true
    threshold: 4          # privacy >= 4 时继承
    decay_on_topic_shift: true  # 检测到话题切换时不再继承

# 纠正反馈配置
correction:
  enabled: true
  dataset_path: ./data/router_corrections.jsonl
  auto_keyword_extraction: true   # 自动从 missed_keywords 扩充关键词
  max_dataset_size: 10000         # 超过此数量时自动清理旧记录
```

---

## 附录：路由模型 System Prompt 模板

```
你是一个任务分类路由助手。你的职责是分析用户输入，评估其隐私程度和任务难度，并决定是否需要发散模式。

请严格按照以下 JSON 格式输出：
{
  "privacy": <int 1-5>,
  "difficulty": <int 1-5>,
  "divergent": <bool>,
  "reason": "<简短判断依据>"
}

评分标准：
- privacy（隐私程度）：
  1 = 完全公开知识（如天气、新闻、百科）
  2 = 公开但需上下文（如代码实现、技术解释）
  3 = 含个人数据（如日程、笔记、邮件）
  4 = 敏感商业信息（如财务数据、内部方案）
  5 = 绝密/合规受限（如合同、薪资、NDA）

- difficulty（任务难度）：
  1 = 事实性查询，单轮回答
  2 = 简单推理，轻度上下文
  3 = 多步推理，中等复杂度
  4 = 复杂推理，需专业知识
  5 = 专家级，创造性/战略性

- divergent（是否需要发散模式）：
  true = 需要先发散再收敛（脑暴、创意、探索类）
  false = 不需要

注意：输出必须是合法的 JSON，不要包含其他内容。
```
