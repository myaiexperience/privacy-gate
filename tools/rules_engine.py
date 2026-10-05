#!/usr/bin/env python3
"""
硬规则隐私检测引擎 v5

在 LLM 看到用户输入之前，用代码层确定性匹配检测隐私信号。
**规则的表达与求值交给 tools/rules_model.py**（v4 契约：匹配原语可配 + exceptions 豁免 + id）；
本模块只负责三件事：分级编排、会话继承、决策日志。

规则模型 v4 带来的能力（详见 rules_model.py 顶部说明）：
  - 匹配原语可配：substring（ASCII 自动词边界）/ word / regex
  - exceptions + when：能表达"这个词在这个语境里别拦"
  - id：决策日志能指向具体规则

历史增量（保留原因见 DECISIONS.md）：
  - stdin 传参（消除 shell 注入/转义风险）
  - 会话级隐私继承（多轮追问不降级）+ 话题切换检测
  - 决策日志（data/routing_log.jsonl），**写盘失败不影响分级**
  - ASCII 关键词词边界匹配（防 "veranda" 命中 NDA 之类误伤）

用法：
  # 推荐：stdin 传参（heredoc 单引号定界符，无 shell 展开）
  python tools/rules_engine.py --json --log --session-id conv_123 --prev-level high --stdin <<'EOF'
  <用户输入原文>
  EOF

  # 兼容旧用法（不推荐：命令行参数拼接有 shell 转义风险）
  python tools/rules_engine.py --json "<用户输入>"

  # 决策日志默认写 data/routing_log.jsonl；--log-path 可覆盖
  # （环境变量 PRIVACY_GATE_LOG 同效；优先级 --log-path > 环境变量 > 默认）
  # 日志写入失败只告警，不影响分级结果与退出码。

输出 JSON 字段：
  level            本轮检测结果: none | medium | high
  matched_keywords 命中且**未被豁免**的词
  matched_rules    命中的规则（含被豁免的，带豁免后的级别，供排查用）
  exemptions       实际生效的豁免
  inherited        是否因会话继承抬升（多轮不降级）
  topic_shift      是否检测到话题切换（重置继承）
  effective_level  最终生效级别 = max(level, 继承级别)
"""

import json
import os
import sys
from datetime import datetime, timezone

# 两种上下文都要能导入（见 paths.sibling 的说明）：
#   - 被当作包导入（pip 安装后 / python -m privacy_gate.rules_engine）
#   - 被当作脚本直接跑（python tools/rules_engine.py，插件和 prompts 就是这么调的）
try:
    from . import paths
except ImportError:  # 脚本模式
    import paths

rules_model = paths.sibling("rules_model")

# 规则文件与数据目录交给 paths 统一解析（三种运行环境各不一样），
# 仍支持 PRIVACY_GATE_RULES / PRIVACY_GATE_DATA 覆盖——理由见 paths.py。
RULES_PATH = paths.rules_path()
DATA_DIR = paths.data_dir()
LOG_PATH = os.path.join(DATA_DIR, "routing_log.jsonl")

# 话题切换信号：默认值来自规则模型；规则文件里可用 topic_shift_keywords 覆盖
TOPIC_SHIFT_KEYWORDS = rules_model.DEFAULT_TOPIC_SHIFT_KEYWORDS

_LEVELS = rules_model.LEVELS
_LEVEL_RANK = rules_model.RANK


def load_rules(path: str) -> dict:
    """读规则文件（v3 / v4 自动识别）→ 归一化 v4 结构。"""
    return rules_model.load_rules(path)


def normalize(text: str) -> str:
    return text.lower().strip()


def _ascii_word_match(kw: str, text: str) -> bool:
    """[已迁移] 词边界匹配。

    v5 起由 rules_model.match_hits 统一负责。保留此函数名，避免历史调用方断裂。
    """
    return bool(rules_model.match_hits(
        {"type": "substring", "patterns": [kw]}, text, normalize(text)))


def check_privacy(user_input: str, rules: dict) -> dict:
    """分级（不涉及会话状态）。

    返回字段兼容 v5.1：privacy_hit / level / matched_keywords；
    另附 matched_rules 与 exemptions，供决策日志与 explain 使用。
    """
    ev = rules_model.evaluate(user_input, rules)
    return {
        "privacy_hit": ev["level"] != "none",
        "level": ev["level"],
        "matched_keywords": ev["matched_keywords"],
        "matched_rules": ev["contributions"],
        "exemptions": ev["exemptions"],
    }


def apply_inheritance(level: str, prev_level: str, text: str, topic_shift_keywords=None):
    """多轮隐私继承。

    规则（fail-closed）：
    - 本轮无关键词命中，且上一轮为 high/medium，且无话题切换信号 → 继承上一轮
    - 本轮命中关键词但级别低于上一轮 → 取更高级别（宁可多挡）
    - 检测到话题切换信号 → 一律重置为本轮检测结果

    topic_shift_keywords 缺省用内置默认；规则文件里定义了就用文件里的
    （保持旧的 3 参调用不破，同时让策略层能改这个话题切换词表）。
    """
    t = normalize(text)
    keywords = topic_shift_keywords or TOPIC_SHIFT_KEYWORDS
    topic_shift = any(kw in t for kw in keywords)
    prev = prev_level if prev_level in _LEVELS else "none"

    inherited = False
    if level == "none" and _LEVEL_RANK[prev] >= _LEVEL_RANK["medium"] and not topic_shift:
        effective = prev
        inherited = True
    elif _LEVEL_RANK[prev] > _LEVEL_RANK[level] and not topic_shift:
        effective = prev
        inherited = True
    else:
        effective = level

    if topic_shift:
        inherited = False
        effective = level

    return effective, inherited, topic_shift


def log_decision(record: dict, log_path: str = LOG_PATH) -> bool:
    """追加一条路由决策日志（jsonl）。

    best-effort：写盘失败绝不影响分级。分级是引擎的核心职责，日志是副产物。

    返回 True=已落盘，False=写入失败（已向 stderr 告警，调用方无需处理）。

    为什么必须容错（真实回归）：
      只读目录 / 容器只读挂载 / CI checkout / 受限沙箱下，日志写失败会抛
      PermissionError，让整个进程以退出码 1 结束。插件侧拿到非零退出码
      即 fail-closed 兜底成 medium，于是**每条消息都被当作敏感内容**、
      所有远程工具被拦——与 D11 记录的"引擎失败连锁锁死"同族，只是触发
      条件从编码崩溃换成了目录只读。

    newline="\\n"：显式锁定 LF，避免 Windows 文本模式写出 CRLF，
    让 jsonl 跨平台字节一致（也避免 git 出现整文件行尾 diff）。
    """
    try:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # 权限、磁盘满、路径非法……
        print(
            f"[rules-engine] 决策日志写入失败（不影响分级结果）: {e}",
            file=sys.stderr,
        )
        return False


def _clean(text: str) -> str:
    """清洗 surrogate 字符（Windows 管道/控制台按 ANSI 代码页解码时会产生）。

    这类字符写 UTF-8 文件或打印时会触发 UnicodeEncodeError，必须清洗。
    """
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def main(argv=None):
    # Windows 下 stdin/stdout 默认按 ANSI 代码页（如 cp936）编解码，
    # 而调用方（opencode 插件）通过管道传/收 UTF-8；不重配置时中文会
    # 变乱码甚至产生 surrogate 字符导致 UnicodeEncodeError。统一改为 UTF-8。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = sys.argv[1:] if argv is None else argv
    flags = [a for a in argv if a.startswith("--")]
    positionals = [a for a in argv if not a.startswith("--")]
    use_json = "--json" in flags
    use_stdin = "--stdin" in flags
    use_log = "--log" in flags

    def flag_value(name, default=None):
        # 注意：必须在原始 argv 上取值（值 token 不以 "--" 开头，会被 flags 过滤掉）
        for i, a in enumerate(argv):
            if a == name and i + 1 < len(argv):
                v = argv[i + 1]
                if not v.startswith("--"):
                    return v
                return default
        return default

    session_id = flag_value("--session-id", "unknown")
    prev_level = flag_value("--prev-level", "none")
    source = flag_value("--source", "cli")
    log_path = flag_value("--log-path", os.environ.get("PRIVACY_GATE_LOG") or LOG_PATH)

    if use_stdin:
        user_input = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    elif positionals:
        user_input = positionals[0]
    else:
        print(
            "用法: python rules_engine.py --json --stdin "
            "[--log --session-id ID --prev-level LEVEL]",
            file=sys.stderr,
        )
        sys.exit(2)

    user_input = _clean(user_input)

    if not os.path.exists(RULES_PATH):
        print(json.dumps({"error": f"规则文件不存在: {RULES_PATH}"}, ensure_ascii=False))
        sys.exit(1)

    rules = load_rules(RULES_PATH)
    result = check_privacy(user_input, rules)
    effective, inherited, topic_shift = apply_inheritance(
        result["level"], prev_level, user_input, rules.get("topic_shift_keywords")
    )
    result.update({
        "inherited": inherited,
        "topic_shift": topic_shift,
        "effective_level": effective,
    })

    if use_log:
        log_decision({
            "session_id": session_id,
            "source": source,
            "user_input": user_input,
            "prev_level": prev_level,
            "level": result["level"],
            "matched_keywords": result["matched_keywords"],
            # 规则级溯源：stats / 误伤报告要靠它算出"最吵的是哪条规则"
            "matched_rules": [
                {"id": c.get("rule_id", ""), "level": c.get("level", ""),
                 "action": c.get("action", ""),
                 **({"exempted_by": c["exempted_by"]} if c.get("exempted_by") else {})}
                for c in result.get("matched_rules") or []
            ],
            "exemptions": [e.get("id", "") for e in result.get("exemptions") or []],
            "inherited": inherited,
            "topic_shift": topic_shift,
            "effective_level": effective,
        }, log_path)

    if use_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"level: {result['level']}")
        print(f"matched: {result['matched_keywords']}")
        print(f"inherited: {inherited}  topic_shift: {topic_shift}")
        print(f"effective_level: {effective}")

    sys.exit(0 if effective in _LEVELS else 1)


if __name__ == "__main__":
    main()
