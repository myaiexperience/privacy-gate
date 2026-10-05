# 从 v5.1 迁移到 v6

> v5.1 的安装方式是"把文件复制进你的 agent 项目"。v6 改变了强制层的位置，
> 所以迁移不是覆盖文件那么简单——**有些东西该留在你那边，不该跟着更新走。**
> 这份文档就是那条界线。

## 变了什么

| | v5.1 | v6 |
|---|---|---|
| 强制层 | opencode 插件 hook | **本地网关**（LLM API 必经路径） |
| opencode 的地位 | 基座 | 适配器之一 |
| 规则文件 | `_schema: v3`，只有 `keywords` 被读 | `schema: v4`，匹配原语 / `action` / 模式级豁免 |
| 纠正回流 | 只能追加 | `add` / `remove` / `demote` / `exempt` |
| 可观测 | 决策日志 | 决策日志 + `explain` + `stats` |
| 体检 | 6 项 | 规则契约 lint、收窄路径、零依赖断言、泄露面审计、文档命令核对…（不写死项数：项数随布局变化） |

**v3 规则文件会被自动识别并转换**，不需要你手动改格式。但第一次写回时会升级成 v4，
届时 diff 会比较大（结构变了），这是预期的。

## 迁移前：先分清"谁的边界"

这是整份文档里最重要的一段。

出厂词表（`keywords/rules.json`）是**默认值**，它会随项目更新而变化；
你自己加的词是**你的策略**，它不该跟着更新走——否则每次 `git pull` 都要打架，
而打架的结局通常是使用者干脆不维护了。

所以 v6 起，你自己的词表放在你自己的文件里，用环境变量指过去：

```bash
export PRIVACY_GATE_RULES=/path/to/my-rules.json     # Windows: $env:PRIVACY_GATE_RULES=...
export PRIVACY_GATE_DATA=/path/to/my-data            # 可选：日志与会话状态放哪
```

- 它会**替换**出厂词表（不是叠加）——"我的边界"就是我的边界
- 纠正回流（`correct.py`）会写进这个文件，回归用例 `test_cases.json` 也落在它旁边
- `explain` / `gateway` 也都认这个变量

**如果你一个字都没改过出厂词表**，那就不用设，直接用默认的。

## 迁移路径 A：最小改动（推荐先做这一步）

适用于"v5.1 是复制进项目里的"这种安装方式。

1. **先把你的词表分离出来**（如果你加过词）：
   把 v5.1 那份 `rules.json` 复制到你自己的目录，设好 `PRIVACY_GATE_RULES`。
   这样第 3 步替换引擎时，你加的边界不会丢。
2. **保留你的运行数据**：`data/routing_log.jsonl`（真实使用记录）
   和 `data/corrections.jsonl`。v5.1 里如果有一个只含注释的 `router_corrections.jsonl`，
   那是废弃文件，忽略它。
3. **切换引擎**。三种做法，按侵入程度从低到高：
   - **`pip install .`**（在 v6 仓库里执行）→ 得到 `privacy-gate` 命令，
     规则用随包安装的出厂词表；
   - 把 v6 仓库放在手边，让原来的入口脚本转发过去；
   - 或者删掉旧的引擎副本，把插件/提示词里的路径指向 v6 仓库的 `tools/`。

   > ⚠️ 如果你**同时**有两份引擎副本（比如"活体目录一份 + 发布包一份"），
   > 那正是本项目明确反对的状态（见 [DECISIONS.md](../DECISIONS.md) D12：
   > 单一来源 + 漂移检查）。迁移是一次消灭它的好机会——**只留一份**。
4. **装上网关**（这是 v6 的主要收益，也可以之后再做）：
   见 README 的「v6：把门禁下沉到传输层」一节。不装网关，v6 就只等于"更好的规则引擎"。

## 迁移路径 B：只用引擎，不换安装方式

如果你暂时不想动网关和插件，只想拿到 v4 规则与四个纠正动作：

- 保留 v5.1 的插件与 prompt 不变
- 用 v6 的 `tools/rules_engine.py` 与 `tools/correct.py` 替换旧的同名文件
- 设好 `PRIVACY_GATE_RULES`（如果加过词）
- **不要**指望插件能理解 v4 的新能力：插件只读引擎输出的 `effective_level`，
  这一点没变，所以它能正常工作

## 迁移后必须验的几件事

按顺序跑，任何一条不对就先别往下走：

```bash
# 1) 引擎还认你的词表吗
python tools/rules_engine.py --json --stdin <<'EOF'
<一句你确定该被判 high 的话>
EOF
# 期望 effective_level=high

# 2) 出厂词表和你自己的词表各归各位
python privacy_gate.py lint --strict

# 3) 规则为什么是这个级别，能解释吗
python privacy_gate.py explain "<同一句话>"

# 4) 全量体检
python privacy_gate.py doctor
```

**期望 `check.py` 全绿。** 有一项会 WARN（"未找到 opencode.jsonc"）——
如果你是把规则引擎挂在别的 agent 上，那条警告可以忽略。

## 回滚

v6 没有改写任何 v5.1 的文件格式之外的东西，所以回滚很简单：

- 恢复旧的 `rules_engine.py` / `correct.py`，清掉 `PRIVACY_GATE_RULES`
- 如果你的规则文件已经被升级成 v4，**它仍然能被 v5.1 读吗？不能**——
  v5.1 只认 `privacy_high.keywords` 结构。
  回滚前先把手上的 `rules.json` 备份一份，或者用 git 恢复。

一句话：**回滚前先备份 `rules.json`**，因为它是唯一会被原地改变格式的文件。

## 已知缺口（诚实清单）

- **`pip install` 现在有了，但它不会自动"消灭副本"。** 装好之后可以用 `privacy-gate`
  命令，规则来自随包安装的出厂词表；你自己的边界仍然用 `PRIVACY_GATE_RULES` 指过去。
  至于**你原来那份文件副本要不要退役**，那是你的决定——`pip install` 只是让
  "用同一份引擎"变成一个选项，不是强制。
- **`PRIVACY_GATE_RULES` 是替换，不是叠加。** 想"在出厂词表之上加几条"，
  现在得自己把两份合并成一个文件。这是刻意的：叠加会让"我的边界"变得无法预测。
- **迁移不会自动搬你的数据。** `data/` 下的日志与纠正记录需要你自己搬
  （或者用 `PRIVACY_GATE_DATA` 指过去）。安装之后，数据默认落在 `~/.privacy-gate`；
  在仓库里跑则落在 `<仓库>/data`。
- **`privacy-gate doctor` 只在仓库检出里可用。** 它检查插件、prompts、配置模板这些
  只有检出里才有的东西。装好的包里请用 `lint` / `classify` / `explain` / `stats`。
  这是如实报错，不是假装成功。
