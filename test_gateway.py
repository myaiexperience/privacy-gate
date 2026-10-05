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
    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
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
                "body": body,
                "auth": self.headers.get("Authorization"),
            })
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

        # ── 收尾：关服务器 ──
        for s in (srv1, srv2, srv3, srv4, srv5, cloud_srv, local_srv, sse_srv):
            try:
                s.shutdown()
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 60)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
