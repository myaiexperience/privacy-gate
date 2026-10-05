#!/usr/bin/env python3
"""
网关回归测试 —— 传输层门禁

用两个假上游（云端 / 本地）夹住网关，验证四件核心机制：

  1. 无声重路由：none 走云端、敏感走本地，并且模型名被改写
  2. 远程工具剥夺：敏感会话的 tools[] 里，搜索/抓取工具**被摘掉**（不是"被劝不要用"）
  3. 会话继承：同一会话里无关键词的追问，仍然走本地
  4. **fail-closed 的方向性**：本地上游不可达 → 502，且**绝不能改走云端**。
     这是全项目最危险的一条失败路径：本地上游挂了就把请求发给云端，
     等于网络故障时自动降级为"数据出网"。

用法：python test_gateway.py
零第三方依赖。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "tools"))
import gateway  # noqa: E402

PASS = 0
FAIL = 0


def check(note, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", note, ("  " + extra) if extra else ""))


# ── 假上游 ─────────────────────────────────────────────────

def make_upstream(label, records, sse=False):
    """假上游。

    ⚠️ **路径必须严格**：真实上游只认自己的路径。

    这里以前对任何路径都回 200，于是"网关把上游 base 的 `/v1` 又拼了一遍
    （`.../v1/v1/...`）"这个错，被 26 条断言一路放过——直到接上真的
    llama.cpp，真上游回 404 才暴露。

    教训：**一个过于宽容的测试替身，比没有替身更危险**——它让被测代码的错
    看起来是对的。替身该像真东西一样挑剔。
    """
    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        OK_GET = "/v1/models"
        OK_POST = "/v1/chat/completions"

        def log_message(self, *a):
            pass

        def _not_found(self):
            payload = json.dumps({"error": {"message": "File Not Found",
                                            "type": "not_found_error",
                                            "code": 404}}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            records.append({"label": label, "path": self.path, "method": "GET"})
            if self.path.split("?", 1)[0] != self.OK_GET:
                return self._not_found()
            payload = json.dumps({"object": "list", "data": [],
                                  "served_by": label}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            records.append({
                "label": label,
                "path": self.path,
                "method": "POST",
                "body": body,
                "auth": self.headers.get("Authorization"),
            })
            # 路径严格：真上游只认自己的路径（见 make_upstream 的 docstring）
            if self.path.split("?", 1)[0] != self.OK_POST:
                return self._not_found()
            if sse:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for piece in (b'data: {"delta":"he"}\n\n',
                              b'data: {"delta":"llo"}\n\n',
                              b"data: [DONE]\n\n"):
                    self.wfile.write(piece)
                    self.wfile.flush()
                self.close_connection = True
                return
            names = []
            for t in body.get("tools") or []:
                fn = t.get("function") if isinstance(t, dict) else None
                names.append((fn or {}).get("name") or t.get("name"))
            payload = json.dumps({
                "served_by": label,
                "model": body.get("model"),
                "tool_names": names,
                "has_annotation": any(
                    isinstance(m, dict) and m.get("role") == "system"
                    and "系统隐私检测" in str(m.get("content"))
                    for m in (body.get("messages") or [])),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Upstream


def unused_port():
    """拿一个几乎肯定没人监听的端口：绑 0 号端口拿到号再关掉。

    不要写死 9 / 1 之类"看起来没人用"的端口——CI 机器上什么都可能监听，
    那样测试就会时灵时不灵，而"时灵时不灵的安全测试"比没有更糟。
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def post(url, body, headers=None, timeout=30):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, {"raw": raw}, dict(e.headers)


def get(url, timeout=30):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout)
        return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, {"raw": raw}, dict(e.headers)


def msg(text, **extra):
    body = {"model": "some-cloud-model",
            "messages": [{"role": "user", "content": text}]}
    body.update(extra)
    return body


REMOTE_TOOLS = [
    {"type": "function", "function": {"name": "web_search", "parameters": {}}},
    {"type": "function", "function": {"name": "web_fetch", "parameters": {}}},
    {"type": "function", "function": {"name": "read_file", "parameters": {}}},
]


def main():
    tmp = tempfile.mkdtemp(prefix="privacy-gate-gwtest-")
    crashed = None
    try:
        # 规则文件：一条 high（含"保密"）、一条 medium、一条语境豁免
        rules_path = os.path.join(tmp, "rules.json")
        with open(rules_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({
                "schema": "v4",
                "rules": [
                    {"id": "high-default", "level": "high", "action": "block_remote",
                     "match": {"type": "substring", "patterns": ["保密", "收购"]}},
                    {"id": "medium-default", "level": "medium", "action": "prefer_local",
                     "match": {"type": "substring", "patterns": ["预算"]}},
                ],
                "exceptions": [
                    {"id": "news", "demote_to": "none",
                     "applies_to": [{"rule": "high-default", "patterns": ["收购"]}],
                     "when": {"type": "substring", "patterns": ["新闻"]}},
                ],
                "topic_shift_keywords": ["换个话题"],
                "remote_tool_patterns": ["*web*", "*fetch*", "*browse*", "*search*"],
            }, f, ensure_ascii=False, indent=2)
            f.write("\n")

        # 云端密钥走**环境变量**（`--cloud-key-env`）。这里给测试进程设一个假值，
        # 好验证"密钥去哪条腿"那三条性质（见 6.7）。
        os.environ["PRIVACY_GATE_TEST_CLOUD_KEY"] = "sk-test-cloud-secret"

        # ── 0. 上游 URL 拼接（纯函数，不需要起服务器）──
        # 这条是接上真上游之后补的：以前网关把上游 base 的 /v1 又拼了一遍
        # （.../v1/v1/...）→ 真上游 404。而当时的假上游接收任何路径，
        # 所以 26 条断言全是绿的。先单测这个函数，再从真实请求路径上验一遍。
        _cases = [
            ("http://h/v1", "/v1/chat/completions", "http://h/v1/chat/completions"),
            ("http://h/openai/v1", "/v1/chat/completions",
             "http://h/openai/v1/chat/completions"),
            ("http://h", "/v1/chat/completions", "http://h/chat/completions"),
            ("http://h/v1", "/v1/models", "http://h/v1/models"),
        ]
        _bad = [(b, p, gateway.upstream_url(b, p), w) for b, p, w in _cases
                if gateway.upstream_url(b, p) != w]
        check("上游 URL 拼接不重复版本前缀（三种 base 形式）", not _bad,
              str(_bad[:1]) if _bad else "三种都对")

        cloud_records, local_records = [], []
        cloud_srv, cloud_port = start(make_upstream("cloud", cloud_records))
        local_srv, local_port = start(make_upstream("local", local_records))

        def build_gw(policy="reroute", local_upstream=None, state=None):
            args = gateway.build_parser().parse_args([
                "--rules", rules_path,
                "--log", os.path.join(tmp, "log.jsonl"),
                "--state", state or os.path.join(tmp, "state.json"),
                "--policy", policy,
                "--cloud-upstream", "http://127.0.0.1:%d/v1" % cloud_port,
                "--local-upstream", local_upstream or ("http://127.0.0.1:%d/v1" % local_port),
                "--local-model", "local-small-model",
                "--cloud-key-env", "PRIVACY_GATE_TEST_CLOUD_KEY",
                "--timeout", "15",
            ])
            return gateway.Gateway(gateway.Config(args))

        def serve_gw(gw):
            gateway.Handler.gateway = gw
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return httpd, httpd.server_address[1]

        # ── 1. none → 云端，工具不动、模型不改 ──
        gw = build_gw()
        srv1, port1 = serve_gw(gw)
        base = "http://127.0.0.1:%d/v1/chat/completions" % port1
        st, body, hdr = post(base, msg("帮我看看今天的天气", tools=REMOTE_TOOLS))
        check("none → 云端", st == 200 and body.get("served_by") == "cloud",
              "status=%s served_by=%s" % (st, body.get("served_by")))
        check("none → 模型名不被改写", body.get("model") == "some-cloud-model",
              "model=%s" % body.get("model"))
        check("none → 工具一个不少", body.get("tool_names") == ["web_search", "web_fetch", "read_file"],
              "tools=%s" % body.get("tool_names"))
        check("响应头带 route=cloud", "route=cloud" in hdr.get("x-privacy-gate", ""),
              hdr.get("x-privacy-gate", ""))

        # ── 2. 敏感 → 本地，模型改写，远程工具被剥夺 ──
        local_records[:] = []
        st, body, hdr = post(base, msg("帮我写一份保密协议", tools=REMOTE_TOOLS),
                             headers={"x-session-id": "sensitive-1"})
        check("敏感 → 本地", st == 200 and body.get("served_by") == "local",
              "status=%s served_by=%s" % (st, body.get("served_by")))
        check("敏感 → 模型名被改写成本地模型", body.get("model") == "local-small-model",
              "model=%s" % body.get("model"))
        check("敏感 → 远程工具被摘掉，本地工具保留",
              body.get("tool_names") == ["read_file"], "tools=%s" % body.get("tool_names"))
        check("响应头标注被摘掉的工具",
              "tools-stripped=" in hdr.get("x-privacy-gate", "")
              and "web_search" in hdr.get("x-privacy-gate", ""),
              hdr.get("x-privacy-gate", ""))
        check("云端**没有**收到这次敏感请求",
              not any(r["label"] == "cloud" and "保密" in json.dumps(r["body"], ensure_ascii=False)
                      for r in cloud_records))

        # ── 3. 会话继承：同一会话里无关键词的追问仍走本地 ──
        local_records[:] = []
        st, body, hdr = post(base, msg("那第三条怎么改", tools=REMOTE_TOOLS),
                             headers={"x-session-id": "sensitive-1"})
        check("继承：无关键词追问仍走本地", body.get("served_by") == "local",
              "served_by=%s" % body.get("served_by"))
        check("继承被标注", "inherited=true" in hdr.get("x-privacy-gate", ""),
              hdr.get("x-privacy-gate", ""))

        # ── 4. 话题切换 → 重置回云端 ──
        st, body, hdr = post(base, msg("换个话题，聊聊 Docker 怎么用", tools=REMOTE_TOOLS),
                             headers={"x-session-id": "sensitive-1"})
        check("话题切换 → 重置回云端", body.get("served_by") == "cloud",
              "served_by=%s" % body.get("served_by"))

        # ── 5. 语境豁免在网关里同样生效 ──
        st, body, hdr = post(base, msg("看看某公司收购的公开新闻", tools=REMOTE_TOOLS),
                             headers={"x-session-id": "news-1"})
        check("豁免生效 → 走云端", body.get("served_by") == "cloud",
              "served_by=%s" % body.get("served_by"))

        # ── 6. 会话键：不给头就落到 default，并在 healthz 里可见 ──
        post(base, msg("帮我看看天气"))
        hz = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/healthz" % port1, timeout=15).read().decode("utf-8"))
        check("healthz 报告会话键来源", hz.get("session_key_sources", {}).get("fallback", 0) >= 1,
              "sources=%s" % hz.get("session_key_sources"))
        check("healthz 报告重路由上游", str(local_port) in hz.get("upstreams", {}).get("local", ""),
              str(hz.get("upstreams")))

        # ── 6.5 GET /v1/models 透传 ──
        # 客户端（Cline / Continue / Cursor 之类）启动时先调这个列模型，所以这条路径
        # 不通的话是"接上就报错"，而不是某个功能失灵。它与 POST 共用同一个 upstream_url()，
        # 但走单测之外再端到端验一遍，成本只有一次请求。
        st_m, body_m, _ = get("http://127.0.0.1:%d/v1/models" % port1)
        check("GET /v1/models 透传成功（客户端列模型走这条）",
              st_m == 200 and body_m.get("served_by") == "local",
              "status=%s served_by=%s" % (st_m, body_m.get("served_by")))

        # ── 6.6 透明转发：额外字段原样过去，客户端请求头绝不能混进 body ──
        #
        # 真实客户端会带一堆我们没见过的字段（temperature / top_p / stream_options /
        # parallel_tool_calls / response_format / 厂商扩展……）。网关应该是"只改两个
        # 字段"的中间人，不是"重建请求"的转换器——后者会让真客户端在意想不到的地方失灵。
        #
        # 同时验一条**安全**性质：handler 为了推会话键，会把客户端请求头以 `_headers`
        # 临时挂进 body，转发前必须摘掉。否则客户的 Authorization 会以 **body 字段**的
        # 形式被送到上游（走云端腿时更严重）。代码里确实是 try/finally，但此前
        # **没有任何断言守着它**——而"读代码看着对"和"有断言守着"是两件事。
        extra_body = {
            "temperature": 0.7,
            "top_p": 0.95,
            "seed": 12345,
            "stream_options": {"include_usage": True},
            "parallel_tool_calls": False,
            "response_format": {"type": "json_object"},
            "stop": ["\n\n"],
            "x_vendor_extension": {"weird": ["nested", 1, None]},
        }
        _cloud_before = len(cloud_records)
        st_pt, body_pt, _ = post(
            base, msg("今天天气不错", **extra_body),
            headers={"x-session-id": "passthrough-1",
                     "Authorization": "Bearer CLIENT-SECRET-DO-NOT-FORWARD"})
        check("带额外字段的请求仍然 200", st_pt == 200, "status=%s" % st_pt)
        _sent = cloud_records[_cloud_before:]
        _got = _sent[-1]["body"] if _sent else {}
        _missing = {k: v for k, v in extra_body.items() if _got.get(k) != v}
        check("额外字段被原样透传（网关不是「重建请求」的转换器）", not _missing,
              ("对不上的字段：%s" % list(_missing)) if _missing else "8 个字段都在")
        _dump = json.dumps(_got, ensure_ascii=False)
        _leak_hdr = "_headers" in _got
        _leak_auth = "CLIENT-SECRET-DO-NOT-FORWARD" in _dump
        # 补充说明必须跟着结果走：写死的说明文字会让 PASS 读起来像 FAIL
        # （这个错我在 test_adapters.py 里修过一次，这里又犯了一次——
        #  说明"说明文字的准确性"不是一次性的问题，而是每次都要注意的。）
        check("客户端请求头没有混进转发出去的 body（_headers 已摘掉）",
              not _leak_hdr,
              "★ body 里出现了 _headers" if _leak_hdr else "body 里没有 _headers")
        check("客户端 Authorization 的值没有出现在 body 里",
              not _leak_auth,
              "★ Authorization 的值被写进了 body" if _leak_auth else "body 里没有该值")

        # ── 6.7 密钥去哪条腿（安全性质）──
        #
        # 三条性质，此前一条断言都没有：
        #   1. 云端腿必须带上**网关侧**配置的密钥（否则云端调用 401）
        #   2. **本地腿绝不能带密钥**——那等于把你的云端密钥送到本地/第三方服务上
        #   3. 客户端送来的 Authorization **不参与**：网关既不校验也不转发它
        #      （服务端持密钥，客户端不需要知道真钥匙）
        # 第 3 条顺带修了一处文档与行为不符的地方：clients.md 原先写"API Key 只用于
        # 转发给云端上游"，而实际上它被整个丢掉了。
        _cloud_auth = [r.get("auth") for r in cloud_records if r.get("method") == "POST"]
        _local_auth = [r.get("auth") for r in local_records if r.get("method") == "POST"]
        check("云端腿带上了网关侧配置的密钥",
              bool(_cloud_auth) and all(a == "Bearer sk-test-cloud-secret" for a in _cloud_auth),
              "云端收到的 auth：%s" % sorted(set(_cloud_auth)))
        check("本地腿没有 Authorization（云端密钥不会被送到本地）",
              bool(_local_auth) and all(a is None for a in _local_auth),
              "本地收到的 auth：%s" % sorted(set(_local_auth)))
        _all_auth = json.dumps(_cloud_auth + _local_auth, ensure_ascii=False)
        check("客户端送来的 Authorization 没有被转发（用的是网关侧密钥）",
              "CLIENT-SECRET-DO-NOT-FORWARD" not in _all_auth,
              # str() 一下：这里混着 None 和字符串，直接 sorted 会 TypeError
              # （而补充说明是提前求值的，那样的错会让整个套件崩掉——见 main 的兜底）
              "上游收到的 auth：%s" % sorted({str(a) for a in _cloud_auth + _local_auth}))

        # ── 7. ★ fail-closed 方向性：本地上游不可达 → 502，且不回落云端 ──
        dead = gateway.Gateway(gateway.Config(gateway.build_parser().parse_args([
            "--rules", rules_path,
            "--log", os.path.join(tmp, "log2.jsonl"),
            "--state", os.path.join(tmp, "state2.json"),
            "--policy", "reroute",
            "--cloud-upstream", "http://127.0.0.1:%d/v1" % cloud_port,
            "--local-upstream", "http://127.0.0.1:%d/v1" % unused_port(),
            "--timeout", "3",
        ])))
        srv2, port2 = serve_gw(dead)
        cloud_records[:] = []
        st, body, hdr = post("http://127.0.0.1:%d/v1/chat/completions" % port2,
                             msg("帮我写一份保密协议"), headers={"x-session-id": "dead-1"})
        check("★本地上游不可达 → 502", st == 502, "status=%s" % st)
        check("★502 时**绝不**改走云端（云上游收到 0 个请求）",
              len(cloud_records) == 0, "cloud 收到 %d 个" % len(cloud_records))
        check("★错误信息说清了 fail-closed 语义",
              "不会自动改走另一个上游" in json.dumps(body, ensure_ascii=False),
              json.dumps(body, ensure_ascii=False)[:120])

        # ── 8. block 策略 → 403 ──
        blk = build_gw(policy="block", state=os.path.join(tmp, "state3.json"))
        srv3, port3 = serve_gw(blk)
        st, body, hdr = post("http://127.0.0.1:%d/v1/chat/completions" % port3,
                             msg("帮我写一份保密协议"), headers={"x-session-id": "blk-1"})
        check("block 策略 → 403", st == 403, "status=%s" % st)
        check("403 的 error.type 可识别",
              (body.get("error") or {}).get("type") == "privacy_gate_blocked",
              str(body.get("error"))[:100])

        # ── 9. annotate 策略 → 纯演练：不改路由、不动工具，只注入标注 ──
        ann = build_gw(policy="annotate", state=os.path.join(tmp, "state4.json"))
        srv4, port4 = serve_gw(ann)
        st, body, hdr = post("http://127.0.0.1:%d/v1/chat/completions" % port4,
                             msg("帮我写一份保密协议", tools=REMOTE_TOOLS),
                             headers={"x-session-id": "ann-1"})
        check("annotate → 仍走云端（不改路由）", body.get("served_by") == "cloud",
              "served_by=%s" % body.get("served_by"))
        check("annotate → 工具一个不少（演练模式不剥夺能力）",
              body.get("tool_names") == ["web_search", "web_fetch", "read_file"],
              "tools=%s" % body.get("tool_names"))
        check("annotate → 注入了系统标注", body.get("has_annotation") is True,
              "has_annotation=%s" % body.get("has_annotation"))

        # ── 10. 流式透传 ──
        sse_srv, sse_port = start(make_upstream("local-sse", local_records, sse=True))
        sse_gw = gateway.Gateway(gateway.Config(gateway.build_parser().parse_args([
            "--rules", rules_path,
            "--log", os.path.join(tmp, "log5.jsonl"),
            "--state", os.path.join(tmp, "state5.json"),
            "--policy", "reroute",
            "--local-upstream", "http://127.0.0.1:%d/v1" % sse_port,
            "--timeout", "15",
        ])))
        srv5, port5 = serve_gw(sse_gw)
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % port5,
            data=json.dumps(msg("帮我写一份保密协议", stream=True,
                                stream_options={"include_usage": True})).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-session-id": "sse-1"},
            method="POST"), timeout=20)
        streamed = r.read().decode("utf-8", "replace")
        check("流式：三块内容都透传到了", '{"delta":"he"}' in streamed
              and '{"delta":"llo"}' in streamed and "[DONE]" in streamed,
              repr(streamed[:80]))
        check("流式：路由头仍然带上", "route=local" in r.headers.get("x-privacy-gate", ""),
              r.headers.get("x-privacy-gate", ""))

        # ── 11. 冒烟脚本自检 ──
        # gateway_smoke.py 是给使用者拿去连**真实 Ollama** 的。它自己必须先被跑过——
        # 一个从没执行过的脚本，等你真接上模型时大概率是坏的，而那正是最不该出岔子的时刻。
        smoke = os.path.join(_HERE, "tools", "gateway_smoke.py")
        try:
            rs = subprocess.run([sys.executable, smoke, "--self-test"],
                                capture_output=True, timeout=180)
            sout = rs.stdout.decode("utf-8", "replace")
            ok = rs.returncode == 0 and "通过" in sout
            check("网关冒烟脚本自检通过（它要被拿去连真模型）", ok,
                  (sout.strip().splitlines() or [""])[-1][:80])
        except Exception as e:
            check("网关冒烟脚本自检通过（它要被拿去连真模型）", False, str(e))

        # ── 上游实际收到的路径（端到端验一遍那个拼接）──
        # 单测过了还不够：这里看的是**真实请求打过去之后，上游看到的是什么路径**。
        # 按 method 分开断言，因为 GET 与 POST 是两条不同的转发分支
        # （_proxy_get 与 _handle），任何一条拼错都该被抓到。
        _post_paths = sorted({r["path"].split("?")[0]
                              for r in cloud_records + local_records
                              if r.get("method") == "POST"})
        _get_paths = sorted({r["path"].split("?")[0]
                             for r in cloud_records + local_records
                             if r.get("method") == "GET"})
        check("POST 转发到上游的路径就是 /v1/chat/completions（没被拼成 /v1/v1/...）",
              _post_paths == ["/v1/chat/completions"],
              "实际上游收到：%s" % _post_paths)
        check("GET 转发到上游的路径就是 /v1/models（没被拼成 /v1/v1/...）",
              _get_paths == ["/v1/models"],
              "实际上游收到：%s" % _get_paths)

        # ── 12. 启动横幅在重定向下必须实时可见 ──
        # 服务类程序的输出要能被重定向后实时看到。Python 的 stdout 在管道/文件下是
        # **块缓冲**的：不修的话 `gateway.py > gateway.log` 会长时间一片空白，
        # 被强杀时连启动横幅一起丢——而横幅是用户判断"服务到底起没起"的唯一线索。
        #
        # 这里用 PIPE 复现重定向，然后**硬杀**（TerminateProcess，不触发 flush）：
        # 横幅还在，说明它当时真的已经写出去了，而不是躺在缓冲区里。
        gport = unused_port()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(_HERE, "tools", "gateway.py"),
             "--port", str(gport),
             "--local-upstream", "http://127.0.0.1:%d/v1" % unused_port()],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        banner = ""
        try:
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/healthz" % gport, timeout=2).read()
                    break
                except Exception:
                    time.sleep(0.3)
            time.sleep(0.6)      # 给横幅写出的时间
            proc.terminate()     # 硬杀，不触发 flush
            try:
                banner = proc.stdout.read().decode("utf-8", "replace")
            except Exception:
                banner = ""
        finally:
            if proc.poll() is None:
                proc.kill()
        check("启动横幅在重定向下也能实时看到（不是块缓冲）",
              "网关已启动" in banner,
              ("拿到 %d 字节" % len(banner)) if banner else "重定向后一个字都没有")

        # ── 收尾：关服务器 ──
        for s in (srv1, srv2, srv3, srv4, srv5, cloud_srv, local_srv, sse_srv):
            try:
                s.shutdown()
            except Exception:
                pass
    except Exception as e:
        # 测试脚本**自己**出错时（例如某条断言的消息在求值时就抛异常），
        # 不要让整个套件以一个裸 traceback 收场——那样连"跑到哪一步、哪些已经过了"
        # 都看不到，而这两样恰恰是排查时最需要的。
        crashed = e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 60)
    if crashed is not None:
        print("★ 测试脚本自身出错（不是被测代码失败）：%s: %s"
              % (type(crashed).__name__, crashed))
        traceback.print_exception(type(crashed), crashed, crashed.__traceback__)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    return 1 if (FAIL or crashed is not None) else 0


if __name__ == "__main__":
    sys.exit(main())
