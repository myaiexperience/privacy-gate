#!/usr/bin/env python3
"""
会话状态 —— 把多轮继承从"调用方传参"变成"由门禁自己管"

背景：D6 的结论是**路由要按会话建模，不能按单条消息建模**。v5.1 里会话状态存在
opencode 插件的内存 + 一个 state 文件里——也就是说"会话"这个概念被绑在了某个
具体框架上。网关要做同样的事，就得有一份不依赖任何框架的实现。

本模块就是那份实现：给定一批 HTTP 头与请求体，推导出会话键，维护它的级别。

关于会话键的推导（这里有一处我**改了设计文档的方案**）
----------------------------------------------------
OpenAI API 没有会话概念。设计文档给的优先级是：
  1. x-session-id 头 → 2. body.user → 3. metadata.session_id
  4. sha1(首条 system 消息 + 客户端指纹) → 5. 兜底 "default"

第 4 条我**去掉了**，直接落到第 5 条。原因：

  sha1(system 前缀) 只有在客户端每次发同样的 system 消息时才稳定。一旦它随请求
  变化（很多客户端会把时间戳、工具清单、上下文长度塞进 system），每个请求就会
  得到**一个新的会话键 = 一个新会话 = none = 解锁远程**。那正是最危险的失败方向。

  换句话说：第 4 条表面上是"尽力识别会话"，实际效果可能是"每次请求都当作新会话"。
  按 fail-closed 的原则，不确定时应该落到一个**共享的**兜底键上（宁可过度继承），
  而不是落到"看起来更聪明"的派生键上。

代价：多人共用一个网关实例时会互相污染（一人的敏感会话会把所有人拖到本地）。
单机单用户无感。多用户部署必须让客户端带上 x-session-id——网关会在日志和
/healthz 里报告"多少请求没带会话标识"。

零第三方依赖。
"""

import hashlib
import json
import os

SESSION_HEADER = "x-session-id"
FALLBACK_KEY = "default"
LEVELS = ("none", "medium", "high")
RANK = {"none": 0, "medium": 1, "high": 2}

# 话题切换信号：命中即重置继承。默认值与 rules_model 保持一致，
# 实际以规则文件里的 topic_shift_keywords 为准（load 时注入）。
DEFAULT_TOPIC_SHIFT = [
    "换个话题", "不谈这个了", "另外开一个", "新的话题", "不聊这个了",
    "下一个任务", "新任务", "说点别的", "换一个主题",
]


def _lower(text):
    return (text or "").lower()


def derive_key(headers, body, topic_shift_keywords=None):
    """推导会话键。

    返回 (key, how)：
      how ∈ header | user | metadata | fallback
      fallback 的比例是需要盯的运维指标（见模块顶部说明）。
    """
    hdrs = headers or {}
    # HTTP 头在 http.server 里是大小写不敏感的 Message 对象，也兼容普通 dict
    raw = None
    try:
        raw = hdrs.get(SESSION_HEADER) or hdrs.get(SESSION_HEADER.title())
    except Exception:
        raw = None
    if not raw:
        try:
            raw = hdrs.get("X-Session-Id") or hdrs.get("X-Session-ID")
        except Exception:
            raw = None
    if raw and str(raw).strip():
        return str(raw).strip(), "header"

    body = body if isinstance(body, dict) else {}
    user = body.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip(), "user"

    meta = body.get("metadata")
    if isinstance(meta, dict):
        sid = meta.get("session_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip(), "metadata"

    return FALLBACK_KEY, "fallback"


def text_of_messages(body):
    """取出用于分级的文本：**只看最后一条 user 消息**。

    不是全部历史。理由：历史会累积旧的敏感词，导致会话被**永久锁死**——
    一个 high 词进了历史，后面每一轮都变 high，用户没有任何办法解锁，只能重开会话。

    最后一条是 tool / assistant（工具返回、或客户端把 assistant 消息作为结尾）时
    返回 None，表示"本轮不重新分类，沿用会话级别"。
    """
    msgs = (body or {}).get("messages")
    if not isinstance(msgs, list):
        return None
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # 多模态：把 text 片段拼起来
                parts = [p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in (None, "text")]
                return "\n".join(p for p in parts if p)
            return ""
        if role in ("tool", "function", "assistant"):
            return None
    return None


def key_digest(key):
    """给日志/响应头用的短摘要——不要把会话键原文写进日志（它可能是用户标识）。"""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:8]


class SessionStore:
    """会话级别表。线程安全靠外部串行化（网关是单线程 accept + 每请求一线程，
    对 dict 的读写在这里足够；真要并发写同一会话，写坏的也只是级别，不会越权放行）。"""

    def __init__(self, path=None, topic_shift_keywords=None):
        self.path = path
        self.topic_shift_keywords = list(topic_shift_keywords or DEFAULT_TOPIC_SHIFT)
        self._levels = {}
        self.stats = {"total": 0, "header": 0, "user": 0, "metadata": 0, "fallback": 0}
        if path:
            self.load()

    # ── 持久化（可选：跨网关重启恢复继承）──────────────────
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data or {}).items():
                if isinstance(v, dict) and v.get("level") in LEVELS:
                    self._levels[str(k)] = v["level"]
        except FileNotFoundError:
            pass
        except Exception:
            pass  # 状态文件损坏不该拖垮门禁（D13 的教训：失败要往私密那侧倒）

    def save(self):
        if not self.path:
            return False
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({k: {"level": v} for k, v in self._levels.items()},
                          f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    # ── 核心 ──────────────────────────────────────────────
    def level_of(self, key):
        return self._levels.get(key, "none")

    def set_level(self, key, level):
        if level not in LEVELS:
            level = "medium"  # 未知级别按保守处理
        if level == "none":
            # 不保留 none，避免文件无限增长；缺省即 none
            self._levels.pop(key, None)
        else:
            self._levels[key] = level

    def observe(self, headers, body):
        """从一次请求里推导键并记账。"""
        key, how = derive_key(headers, body, self.topic_shift_keywords)
        self.stats["total"] += 1
        self.stats[how] = self.stats.get(how, 0) + 1
        return key, how

    def topic_shifted(self, text):
        t = _lower(text)
        return any(kw in t for kw in self.topic_shift_keywords)

    def effective(self, key, level, text):
        """把本轮判定与会话状态合成最终级别（fail-closed，规则同 rules_engine）。

        与 rules_engine.apply_inheritance 保持一致的语义：
        - 本轮 none 且上一轮 medium/high 且无话题切换 → 继承
        - 本轮更低于上一轮且无话题切换 → 取高
        - 有话题切换 → 一律重置
        """
        prev = self.level_of(key)
        shift = self.topic_shifted(text)
        if shift:
            return level, prev, False, True
        if RANK[prev] > RANK[level]:
            return prev, prev, True, False
        return level, prev, False, False


def canonical_topic_shift(rules):
    """从规则文件里取话题切换词表（规则文件是单一来源）。"""
    if isinstance(rules, dict):
        ts = rules.get("topic_shift_keywords")
        if isinstance(ts, list) and ts:
            return [str(x) for x in ts]
    return list(DEFAULT_TOPIC_SHIFT)
