# 项目规则（供 opencode 与协作者阅读）

## 设计原则

**纯规则路由 + 框架级强制门禁 + 纠正回流闭环 + 云端分层。** 详见 [DECISIONS.md](DECISIONS.md)。
公开任务委派云端免费模型（@cloud），敏感任务只在本地 @worker 执行——"本地脑路由 + 云手执行公开活"。

## 隐私级别（由规则引擎决定）

| 级别 | 触发 | 执行位置 | 约束 |
|------|------|---------|------------|
| none | 未命中关键词 | 可委派 @cloud | 所有工具可用 |
| medium | 命中 medium 关键词 | 仅本地 @worker | 禁 webfetch；websearch 仅搜公开技术文档 |
| high | 命中 high 关键词 | 仅本地 @worker | 禁所有远程搜索，数据不出域 |

## 默认保守策略

规则未命中关键词但任务可能涉密（内部称呼、文件名、业务代号等）时，按 medium 处理。

## 多轮隐私继承（fail-closed）

- 上一轮 high/medium，本轮无关键词命中且无话题切换信号 → 继承上一轮
- 本轮命中关键词但级别低于上一轮 → 取更高级别
- 话题切换信号（换个话题 / 不谈这个了 / 新任务…）→ 重置为本轮检测结果

## 用户显式授权（升降级）

- **升级**（可以联网 / 用云端 / 这不是机密）→ 本轮按 none，输出标注 `[审计]` 行
- **降级**（这个不用保密 / 解除限制）→ 本轮按 none，输出标注 `[审计]` 行
- **纠正**（这个也算机密 / 分类错了）→ 调用 tools/correct.py 入库关键词 + 回归用例
- worker 禁止自行升级——授权只能由用户发起

## 规则维护

- 关键词单一来源：`keywords/rules.json`（由 `tools/correct.py` 自动追加，勿手改格式）
- 每次纠正自动生成回归用例到 `keywords/test_cases.json`

## 测试纪律

```bash
python check.py      # 一键体检：配置/引擎中文端到端/correct.py/插件/状态文件/Ollama/回归
python test_routes.py
```

修改 `keywords/rules.json` / `tools/rules_engine.py` / `tools/correct.py` /
`.opencode/plugins/privacy-gate.js` 后必须跑 `check.py` 全绿才算完成。

## 决策日志

每次路由决策落 `data/routing_log.jsonl`（由 `tools/rules_engine.py --log` 写入）。
升级频率、误伤最多的关键词、模糊地带占比都从这里统计。

## 文件结构

```
├── .opencode/plugins/privacy-gate.js  # 框架级门禁插件（含云端护栏）
├── tools/rules_engine.py              # 确定性检测 + 多轮继承 + 决策日志
├── tools/correct.py                   # 纠正回流
├── keywords/rules.json                # 隐私关键词（单一来源）
├── keywords/test_cases.json           # 纠正回流生成的回归用例
├── prompts/worker.md                  # 本地执行 agent 系统提示（兜底 + 委派）
├── prompts/cloud.md                   # 云端 agent 系统提示（非 none 即拒绝）
├── check.py                           # 一键体检
├── test_routes.py                     # 回归测试
├── data/                              # 运行时日志（勿提交，见 .gitignore）
├── opencode.jsonc.example             # 配置模板（worker + cloud；桌面端建议 provider 放全局配置）
├── docs/                              # 早期设计文档（存档）
└── DECISIONS.md                       # 决策日志
```
