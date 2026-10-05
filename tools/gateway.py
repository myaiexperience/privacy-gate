#!/usr/bin/env python3
"""
本地 OpenAI 兼容隐私网关（传输层门禁）

为什么放在这一层
----------------
v5.1 把强制层放在 opencode 的插件 hook 上，代价是三次真实教训（D9 插件格式、
D11 durable event schema、D13 只读环境）**全都来自"依赖别人的 hook"**。
网关放在所有 Agent 的公共必经之路上——LLM API 调用本身：

    Agent ──► 127.0.0.1:8787/v1 ──┬─ none        ──► 云端上游
    （opencode / Claude Code /    ├─ medium/high ──► 本地上游（改写 model）
      Cline / Cursor / 裸脚本）    └─ 或 403 拒绝  （block 策略）

于是"哪些数据能出内网"不再依赖任何框架配合，opencode 从"基座"降级为"适配器之一"。

三件核心机制
-----------
1. **无声重路由**：敏感会话的请求改写到本地上游——模型自己都不知道被降级了。
2. **远程工具剥夺**：敏感会话时把 `tools[]` 里的搜索/抓取工具**摘掉**。
   模型不是"被要求别调"，是**没有能力调**。这一条不依赖任何框架 hook。
3. **fail-closed 的方向性**：本地上游不可达 → 502，**绝不回落到云端**。
   网络故障必须朝"更私密"的方向倒。（D13 的教训：修失败点 ≠ 修失败语义。）

用法
----
    python tools/gateway.py \
        --cloud-upstream https://api.example.com/v1 \
        --cloud-key-env MY_CLOUD_KEY \
        --local-upstream http://127.0.0.1:11434/v1 \
        --local-model qwen3:35b \
        --policy reroute

    然后把客户端的 base_url 指向 http://127.0.0.1:8787/v1

配置为什么**不**放进 rules.json
------------------------------
设计文档原来把上游地址也塞进 rules.json（出于"单一来源"）。实现时改成独立配置：

- `rules.json` 是要提交、要分享、要被人抄走的**策略**；
- 上游地址是**部署**信息（本机 Ollama 的内网 IP、云端 key 从哪来）。

把内网 IP 混进可分享的策略文件，正好违反本项目自己的发布纪律
（"发布前不暴露内网 IP、主机名"）。所以：策略归策略，部署归部署。

零第三方依赖，只用标准库。
"""

import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 两种上下文都要能导入（见 paths.sibling 的说明）
try:
    from . import paths
except ImportError:  # 脚本模式
    import paths

rules_engine = paths.sibling("rules_engine")
rules_model = paths.sibling("rules_model")
session_mod = paths.sibling("session")

_DATA = paths.data_dir()
DEFAULT_RULES = paths.rules_path()
DEFAULT_LOG = os.environ.get("PRIVACY_GATE_LOG") or os.path.join(_DATA, "routing_log.jsonl")
DEFAULT_STATE = (os.environ.get("PRIVACY_GATE_STATE")
                 or os.path.join(_DATA, "gateway_sessions.json"))

POLICIES = ("reroute", "block", "annotate")


# ── 工具剥夺 ───────────────────────────────────────────────

def tool_name(tool):
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    for k in ("name", "id", "tool"):
        if tool.get(k):
            return str(tool[k])
    return ""


def strip_remote_tools(body, patterns):
    """从 tools[] 摘掉远程能力，返回 (保留, 摘掉的名单)。

    这是"不依赖框架 hook"的核心机制：模型拿到的工具清单里**根本没有**搜索/抓取，
    所以它不需要"自觉"，也没有能力违规。
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools or not patterns:
        return [], []
    kept, stripped = [], []
    for t in tools:
        name = tool_name(t)
        low = name.lower()
        if name and any(fnmatch.fnmatch(low, str(p).lower()) for p in patterns):
            stripped.append(name)
        else:
            kept.append(t)
    if stripped:
        body["tools"] = kept
        if not kept:
            body.pop("tools", None)
            body.pop("tool_choice", None)
        elif isinstance(body.get("tool_choice"), dict):
            # 被点名的那个工具正好被摘了 → 改回 auto，否则上游可能报 400
            chosen = tool_name({"function": body["tool_choice"].get("function") or {}})
            if chosen and any(fnmatch.fnmatch(chosen.lower(), str(p).lower())
                              for p in patterns):
                body["tool_choice"] = "auto"
    return kept, stripped


def inject_system_note(body, note):
    """annotate 策略：把标注插成一条 system 消息（插在开头，保证消息序合法）。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return False
    msgs.insert(0, {"role": "system", "content": note})
    return True


# ── 配置 ───────────────────────────────────────────────────

class Config(object):
    def __init__(self, args):
        self.host = args.host
        self.port = args.port
        self.rules_path = args.rules
        self.log_path = args.log
        self.state_path = args.state
        self.policy = args.policy
        self.timeout = args.timeout
        self.local_upstream = (args.local_upstream or "").rstrip("/")
        self.local_model = args.local_model
        self.cloud_upstream = (args.cloud_upstream or "").rstrip("/")
        self.cloud_key = os.environ.get(args.cloud_key_env, "") if args.cloud_key_env else ""
        self.cloud_key_env = args.cloud_key_env
        self.extra_patterns = [p for p in (args.remote_tool_patterns or "").split(",") if p.strip()]


def build_parser():
    p = argparse.ArgumentParser(
        description="本地 OpenAI 兼容隐私网关：按隐私级别重路由 / 剥夺远程工具")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--rules", default=DEFAULT_RULES)
    p.add_argument("--log", default=DEFAULT_LOG)
    p.add_argument("--state", default=DEFAULT_STATE, help="会话状态文件（跨重启恢复继承）")
    p.add_argument("--policy", default=os.environ.get("PRIVACY_GATE_POLICY", "reroute"),
                   choices=POLICIES)
    p.add_argument("--timeout", type=int, default=300, help="上游超时（秒）")
    p.add_argument("--cloud-upstream",
                   default=os.environ.get("PRIVACY_GATE_CLOUD_UPSTREAM", ""),
                   help="云端上游 base（如 https://api.example.com/v1）；不填则所有请求走本地")
    p.add_argument("--cloud-key-env", default="PRIVACY_GATE_CLOUD_KEY",
                   help="存放云端 API key 的环境变量名（key 不进命令行，避免留在进程列表里）")
    p.add_argument("--local-upstream",
                   default=os.environ.get("PRIVACY_GATE_LOCAL_UPSTREAM",
                                          "http://127.0.0.1:11434/v1"))
    p.add_argument("--local-model",
                   default=os.environ.get("PRIVACY_GATE_LOCAL_MODEL", ""),
                   help="重路由时改写的模型名；留空则沿用客户端请求的模型名")
    p.add_argument("--remote-tool-patterns", default="",
                   help="覆盖规则文件里的 remote_tool_patterns（逗号分隔）")
    return p


# ── 网关主体 ───────────────────────────────────────────────

class Gateway(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.rules = rules_model.load_rules(cfg.rules_path) if os.path.isfile(cfg.rules_path) \
            else {"schema": "v4", "rules": [], "exceptions": [],
                  "topic_shift_keywords": session_mod.DEFAULT_TOPIC_SHIFT,
                  "remote_tool_patterns": []}
        self.store = session_mod.SessionStore(
            cfg.state_path, session_mod.canonical_topic_shift(self.rules))
        if cfg.extra_patterns:
            self.tool_patterns = cfg.extra_patterns
        else:
            self.tool_patterns = list(self.rules.get("remote_tool_patterns") or [])

    # ── 分级 ──
    def classify(self, body):
        """返回 dict：raw / effective / key / how / text / inherited / topic_shift / engine_ok。

        会话键来自请求头（handler 已把头以 `_headers` 挂进 body，转发前会摘掉）。
        """
        key, how = self.store.observe(body.get("_headers") or {}, body)
        text = session_mod.text_of_messages(body)
        engine_ok = True
        if text is None:
            # 本轮没有可分类的 user 文本（工具返回 / assistant 结尾）→ 沿用会话级别
            effective = self.store.level_of(key)
            return {"raw": None, "effective": effective, "key": key, "how": how,
                    "text": "", "inherited": False, "topic_shift": False,
                    "engine_ok": engine_ok}
        try:
            raw = rules_model.evaluate(text, self.rules)["level"]
        except Exception as e:
            # 引擎失败 → 按 medium，且只走本地（fail-closed）
            print("[gateway] 分级失败，按 medium 处理: %s" % e, file=sys.stderr)
            raw, engine_ok = "medium", False
        effective, prev, inherited, shift = self.store.effective(key, raw, text)
        self.store.set_level(key, effective)
        self.store.save()
        return {"raw": raw, "effective": effective, "key": key, "how": how,
                "text": text, "inherited": inherited, "topic_shift": shift,
                "engine_ok": engine_ok}

    # ── 选上游 ──
    def pick_upstream(self, level):
        """返回 (url, 是否本地)。云端不可用时**不**回落——那是最危险的失败方向。

        annotate 是**演练模式**：只观测、不改路由。它存在的意义就是"先把策略接上，
        看它想怎么判，但先不让它管事"——所以它连路由都不许动。
        （写成测试之后才发现第一版这里漏了策略判断，敏感请求被悄悄送到了本地。）
        """
        if self.cfg.policy == "annotate":
            if self.cfg.cloud_upstream:
                return self.cfg.cloud_upstream, False
            return self.cfg.local_upstream, True
        if level == "none" and self.cfg.cloud_upstream:
            return self.cfg.cloud_upstream, False
        return self.cfg.local_upstream, True

    def log(self, **kw):
        """写一条决策日志。用关键字参数：位置参数在这个函数里太容易接错位。"""
        rec = {
            "source": "gateway",
            "session_id": session_mod.key_digest(kw.get("key", "")),
            "session_key_source": kw.get("how"),
            "user_input": kw.get("text") or "",
            "level": kw.get("raw"),
            "matched_keywords": [],
            "inherited": bool(kw.get("inherited")),
            "topic_shift": bool(kw.get("shift")),
            "effective_level": kw.get("effective"),
            "route": kw.get("route"),
            "tools_stripped": kw.get("stripped") or [],
            "policy": self.cfg.policy,
        }
        if kw.get("note"):
            rec["note"] = kw["note"]
        rules_engine.log_decision(rec, self.cfg.log_path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "privacy-gate/0.1"
    gateway = None  # 由 serve() 注入

    # 不让标准库把请求日志打到 stderr（我们要的是自己的决策日志）
    def log_message(self, fmt, *args):
        pass

    # ── 响应助手 ──
    def _gate_headers(self, gate):
        out = [("x-privacy-gate", gate)]
        return out

    def _send_json(self, code, payload, gate=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (self._gate_headers(gate) if gate else []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _openai_error(self, code, message, etype="invalid_request_error"):
        return {"error": {"message": message, "type": etype, "code": code}}

    # ── 路由 ──
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/healthz", "/health"):
            g = self.gateway
            st = dict(g.store.stats)
            fb = st.get("fallback", 0)
            total = st.get("total", 0) or 1
            body = {
                "status": "ok",
                "policy": g.cfg.policy,
                "rules": {"schema": g.rules.get("schema"),
                          "rules": len(g.rules.get("rules") or []),
                          "exceptions": len(g.rules.get("exceptions") or [])},
                "remote_tool_patterns": g.tool_patterns,
                "upstreams": {"cloud": g.cfg.cloud_upstream or None,
                              "local": g.cfg.local_upstream},
                "sessions_tracked": len(g.store._levels),
                "session_key_sources": st,
                "session_key_fallback_ratio": round(float(fb) / total, 3),
                "note": ("fallback 比例高说明客户端没带 x-session-id；"
                         "多人共用时要配，否则会话会互相污染"),
            }
            return self._send_json(200, body)
        if path.startswith("/v1/"):
            return self._proxy_get(path)
        return self._send_json(404, self._openai_error(404, "not found", "not_found"))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.startswith("/v1/"):
            return self._send_json(404, self._openai_error(404, "not found", "not_found"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return self._send_json(400, self._openai_error(400, "请求体不是合法 JSON"))
        if not isinstance(body, dict):
            return self._send_json(400, self._openai_error(400, "请求体必须是 JSON 对象"))
        self._handle(body, path)

    # ── 无 body 的 GET 透传（如 /v1/models）──
    def _proxy_get(self, path):
        g = self.gateway
        url = upstream_url(g.cfg.local_upstream, path)
        try:
            req = urllib.request.Request(url, method="GET")
            up = urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            return self._send_json(502, self._openai_error(
                502, "本地上游不可达：%s" % e, "upstream_unreachable"))
        data = up.read()
        return self._relay(up, data, [])

    # ── 核心：一次 chat/completions ──
    def _handle(self, body, path):
        g = self.gateway
        # 会话键推导需要请求头，而分级只吃 body —— 把头先挂进去（转发前会摘掉）
        body["_headers"] = {k.lower(): v for k, v in self.headers.items()}
        try:
            info = g.classify(body)
        finally:
            body.pop("_headers", None)

        raw, effective = info["raw"], info["effective"]
        key, how, text = info["key"], info["how"], info["text"]
        inherited, shift = info["inherited"], info["topic_shift"]

        gate = "level=%s policy=%s" % (effective, g.cfg.policy)
        if inherited:
            gate += " inherited=true"
        if shift:
            gate += " topic-shift=true"
        if not info["engine_ok"]:
            gate += " engine-error=true"
        gate += " session=%s" % session_mod.key_digest(key)

        # ── block 策略 ──
        if g.cfg.policy == "block" and effective != "none":
            g.log(key=key, how=how, text=text, raw=raw, effective=effective,
                  inherited=inherited, shift=shift, route="blocked")
            return self._send_json(403, self._openai_error(
                403,
                "privacy-gate: 会话级别=%s，已按 block 策略拒绝。" % effective,
                "privacy_gate_blocked"), gate)

        # ── 选上游 ──
        target, is_local = g.pick_upstream(effective)

        # ── 远程工具剥夺（annotate 是演练模式，不动任何东西）──
        stripped = []
        if effective != "none" and g.cfg.policy != "annotate":
            _, stripped = strip_remote_tools(body, g.tool_patterns)
            if stripped:
                gate += " tools-stripped=%s" % ",".join(stripped)

        # ── annotate 策略：注入标注，但**不改路由**（兼容 v5.1 行为）──
        if g.cfg.policy == "annotate" and effective != "none":
            inject_system_note(body, "[系统隐私检测] effective_level=%s" % effective)

        # ── reroute 策略：改写模型名 ──
        if g.cfg.policy == "reroute" and is_local and g.cfg.local_model:
            body["model"] = g.cfg.local_model

        stream = bool(body.get("stream"))
        route = "local" if is_local else "cloud"
        gate += " route=%s" % route

        g.log(key=key, how=how, text=text, raw=raw, effective=effective,
              inherited=inherited, shift=shift, route=route, stripped=stripped)

        api_key = None if is_local else (g.cfg.cloud_key or None)
        url = upstream_url(target, path)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        for h in ("Accept", "OpenAI-Beta", "OpenAI-Organization", "OpenAI-Project"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        if api_key:
            req.add_header("Authorization", "Bearer " + api_key)

        try:
            up = urllib.request.urlopen(req, timeout=g.cfg.timeout)
        except urllib.error.HTTPError as e:
            # 上游自己的错误：原样透传状态码与体，别把它伪装成我们的失败
            try:
                err_body = e.read()
            except Exception:
                err_body = b""
            self.send_response(e.code)
            self.send_header("Content-Type",
                             e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(err_body)))
            for k, v in self._gate_headers(gate):
                self.send_header(k, v)
            self.end_headers()
            return self.wfile.write(err_body)
        except Exception as e:
            # 连不上 → 502。**这里绝不回落到另一个上游**：
            # 本地上游挂了就把请求发给云端，等于网络故障时自动降级为"数据出网"，
            # 是这个项目里最危险的一种失败方向。
            g.log(key=key, how=how, text=text, raw=raw, effective=effective,
                  inherited=inherited, shift=shift, route=route + "-unreachable",
                  stripped=stripped, note=str(e))
            return self._send_json(502, self._openai_error(
                502,
                "privacy-gate: %s上游不可达（%s）。已按 fail-closed 拒绝，"
                "不会自动改走另一个上游。" % (route, e),
                "upstream_unreachable"), gate)

        if stream:
            return self._relay_stream(up, gate)
        data = up.read()
        return self._relay(up, data, gate)

    # ── 透传 ──
    _HOP_BY_HOP = ("transfer-encoding", "content-length", "connection",
                   "keep-alive", "proxy-authenticate", "proxy-authorization",
                   "te", "trailer", "upgrade")

    def _relay(self, up, data, gate):
        self.send_response(up.status)
        for k, v in up.headers.items():
            if k.lower() in self._HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        if gate:
            self.send_header("x-privacy-gate", gate)
        self.end_headers()
        self.wfile.write(data)

    def _relay_stream(self, up, gate):
        """SSE 原样透传，边读边写，不缓冲——本地模型首 token 延迟敏感。

        用 chunked 编码：长度未知且连接要保持打开，Content-Length 用不了。
        """
        self.send_response(up.status)
        for k, v in up.headers.items():
            if k.lower() in self._HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        if gate:
            self.send_header("x-privacy-gate", gate)
        self.end_headers()
        try:
            while True:
                chunk = up.read(512)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass


def upstream_url(base, path):
    """把客户端路径接到上游 base 上，**不重复版本前缀**。

    客户端请求的是 `<网关>/v1/chat/completions`（网关对外就是 OpenAI 的形状），
    而上游 base 按约定自带它自己的前缀。所以要做的是**替换**那层 `/v1`，不是拼接：

        base=http://h/v1          path=/v1/chat/completions  ->  http://h/v1/chat/completions
        base=http://h/openai/v1   path=/v1/chat/completions  ->  http://h/openai/v1/chat/completions
        base=http://h             path=/v1/chat/completions  ->  http://h/chat/completions

    ⚠️ 这里曾经是错的——直接 `base + path`。上游写 `.../v1` 时会拼成 `.../v1/v1/...`，
    真上游返回 404，而**当时的假上游接收任何路径**，于是 26 条断言全绿。
    这是"机制对了不等于能用"的真实版本：只有接到真上游才暴露。
    接真 Ollama / llama.cpp 复验之后，假的那些上游也改成了**路径严格**，免得再躲过去。
    """
    rest = path[3:] if path.startswith("/v1") else path
    if not rest.startswith("/"):
        rest = "/" + rest
    return base + rest


def serve(cfg):
    # 起服务前把 stdout 切成行缓冲。
    #
    # 为什么必须做：Python 的 stdout 在**重定向到文件或管道**时是块缓冲的
    # （不是 TTY 就攒够几 KB 才写）。于是
    #     python tools/gateway.py ... > gateway.log
    # 会长时间看起来一片空白，被 kill 时连启动横幅一起丢——用户根本不知道
    # 服务到底起没起。这是发布级验证里真撞到的：用 Start-Process 重定向日志，
    # 横幅一个字都没有，而进程其实活得好好的。
    #
    # 服务类程序的输出必须能被重定向后实时看到，这是它和一次性脚本的区别。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    gw = Gateway(cfg)
    Handler.gateway = gw
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    print("privacy-gate 网关已启动")
    print("  监听        http://%s:%d/v1" % (cfg.host, cfg.port))
    print("  策略        %s" % cfg.policy)
    print("  云端上游    %s" % (cfg.cloud_upstream or "(未配置，全部走本地)"))
    print("  本地上游    %s" % cfg.local_upstream)
    print("  规则文件    %s（%d 条规则 / %d 条豁免）"
          % (cfg.rules_path, len(gw.rules.get("rules") or []),
             len(gw.rules.get("exceptions") or [])))
    print("  远程工具模式 %s" % (", ".join(gw.tool_patterns) or "(无)"))
    print("  健康检查    http://%s:%d/healthz" % (cfg.host, cfg.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，退出")
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    cfg = Config(build_parser().parse_args(argv))
    if not os.path.isfile(cfg.rules_path):
        print("规则文件不存在：%s" % cfg.rules_path, file=sys.stderr)
        return 1
    if not cfg.local_upstream:
        print("必须给一个本地上游（--local-upstream）", file=sys.stderr)
        return 1
    return serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
