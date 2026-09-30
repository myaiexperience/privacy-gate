# data/ 目录说明

| 文件 | 写入方 | 内容 | 格式 |
|---|---|---|---|
| `routing_log.jsonl` | `tools/rules_engine.py --log` | 每次路由决策：时间、会话、输入、命中词、级别、继承状态 | 每行一个 JSON |
| `corrections.jsonl` | `tools/correct.py` | 用户纠正记录（漏标关键词、纠正类型） | 每行一个 JSON |

- 决策日志是调关键词库的依据（升级频率、误伤最多的词都从这里统计）
- 纠正记录统一写在 `corrections.jsonl`，关键词自动追加进 `keywords/rules.json`
