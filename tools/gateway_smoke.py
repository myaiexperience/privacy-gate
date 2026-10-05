#!/usr/bin/env python3
"""
网关冒烟：拿**真实上游**验一次端到端

为什么需要它
------------
`test_gateway.py` 用一对**假上游**（云 / 本地）夹住网关，验证了重路由、工具剥夺、
会话继承和 fail-closed 的方向性。那是**机制**验证。

但机制对了不等于能用：真实的 Ollama 可能因为模型名不对而 400、可能不支持某个字段、
可能因为上下文长度而拒绝。这些只有打一次真请求才知道。

（开发这套东西的时候推理服务器不在线，所以这一步**没在作者机器上做过**——
README 的诚实清单里写明了这一点。这个脚本就是把那个缺口交到你手里，
一条命令能补上。）

它断言什么、不断言什么
--------------------
  ✅ 断言：路由决策（响应头 `x-privacy-gate` 里的 route / tools-stripped / inherited）
  ✅ 断言：上游是否真的应答了（HTTP 状态）
  ❌ 不断言：模型回答的质量、延迟、是否正确理解内容

用法
----
    # 1) 自检：不需要任何真实上游，用内置假上游跑一遍，验证脚本本身没坏
    python tools/gateway_smoke.py --self-test

    # 2) 只连本地（所有请求都走本地）
    python tools/gateway_smoke.py \
        --local-upstream http://<你的Ollama地址>:11434/v1 \
        --local-model "qwen3:35b"

    # 3) 云 + 本地都配上，验证"公开走云、敏感走本地"的分流
    python tools/gateway_smoke.py \
        --local-upstream http://<你的Ollama地址>:11434/v1 --local-model "qwen3:35b" \
        --cloud-upstream https://api.example.com/v1 --cloud-key-env MY_CLOUD_KEY

退出码：0 全部符合预期 / 1 有不符合的项 / 2 参数或环境有问题
零第三方依赖。
"""

import argparse
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import paths
except ImportError:  # 脚本模式
    import paths

gateway = paths.sibling("gateway")
rules_model = paths.sibling("rules_model")

DEFAULT_RULES = paths.rules_path()

SAMPLE_TOOLS = [
    {"type": "function", "function": {"name": "web_search", "parameters": {}}},
    {"type": "function", "function": {"name": "web_fetch", "parameters": {}}},
    {"type": "function", "function": {"name": "read_file", "parameters": {}}},
]


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ── 内置假上游（只为 --self-test 服务）────────────────────

def make_fake_upstream(records, label):
    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            names = []
            for t in body.get("tools") or []:
                fn = t.get("function") if isinstance(t, dict) else None
                names.append((fn or {}).get("name") or t.get("name"))
            records.append({"label": label, "model": body.get("model"), "tools": names})
            payload = json.dumps({"object": "chat.completion", "served_by": label,
                                  "model": body.get("model")}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Upstream


# ── 分级样本：从规则文件里挑一个 high 模式，而不是写死 ────

def pick_sensitive_text(rules):
    """从规则里找一个 high 的字面模式来构造样本。

    写死一句"帮我写一份保密协议"是不行的：使用者可能用自己的词表（
    PRIVACY_GATE_RULES），那句话在他的规则下根本不触发，冒烟就会假失败。
    """
    for rule in rules.get("rules") or []:
        if rule.get("level") != "high":
            continue
        if (rule.get("match") or {}).get("type") == "regex":
            continue
        for p in (rule.get("match") or {}).get("patterns") or []:
            return "冒烟测试：%s 相关事项" % p, p
    return None, None


def start_gateway(cfg):
    gw = gateway.Gateway(cfg)
    gateway.Handler.gateway = gw
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), gateway.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, gw


def ask(base, text, session, tools=None, timeout=120, stream=False):
    """发一次 chat/completions，返回 (状态码, 响应头, 解析后的体或 None)。"""
    body = {"model": "smoke-test-model",
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 16}
    if tools:
        body["tools"] = tools
    if stream:
        body["stream"] = True
    req = urllib.request.Request(
        base, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-session-id": session},
        method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            payload = None
        return r.status, dict(r.headers), payload
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            payload = None
        return e.code, dict(e.headers), payload
    except Exception as e:
        return None, {}, {"transport_error": str(e)}


def route_of(headers):
    val = headers.get("x-privacy-gate") or headers.get("X-Privacy-Gate") or ""
    for part in val.split():
        if part.startswith("route="):
            return part.split("=", 1)[1]
    return ""


def check(note, ok, extra=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", note, ("  " + extra) if extra else ""))
    return 0 if ok else 1


def run_suite(base, sensitive_text, has_cloud, timeout, verbose=False):
    """跑一组判定。返回失败数。"""
    fails = 0
    expect_none_route = "cloud" if has_cloud else "local"

    # 1) 公开内容
    st, hdr, body = ask(base, "帮我看看今天的天气怎么样", "smoke-public",
                        tools=SAMPLE_TOOLS, timeout=timeout)
    r = route_of(hdr)
    fails += check("公开内容走 %s" % expect_none_route,
                   st == 200 and r == expect_none_route,
                   "status=%s route=%s" % (st, r))
    if st != 200:
        fails += check("  上游真的应答了（不是连接失败）", False,
                       str(body)[:200] if body else "")

    # 2) 敏感内容：必须走本地
    st, hdr, body = ask(base, sensitive_text, "smoke-sensitive",
                        tools=SAMPLE_TOOLS, timeout=timeout)
    r = route_of(hdr)
    fails += check("敏感内容走本地（无声重路由生效）",
                   st == 200 and r == "local", "status=%s route=%s" % (st, r))
    gate = hdr.get("x-privacy-gate") or hdr.get("X-Privacy-Gate") or ""
    if "tools-stripped=" in gate:
        print("       摘掉的工具：%s" % gate.split("tools-stripped=")[1].split()[0])
    else:
        fails += check("远程工具被摘掉", False,
                       "响应头里没有 tools-stripped（检查规则里的 remote_tool_patterns）")

    # 3) 同会话无关键词追问：继承
    st, hdr, body = ask(base, "那第三条怎么改", "smoke-sensitive",
                        tools=SAMPLE_TOOLS, timeout=timeout)
    r = route_of(hdr)
    gate = hdr.get("x-privacy-gate") or ""
    fails += check("无关键词追问仍走本地（多轮继承）",
                   st == 200 and r == "local" and "inherited=true" in gate,
                   "status=%s route=%s" % (st, r))

    # 4) 话题切换：重置
    if has_cloud:
        st, hdr, body = ask(base, "换个话题，聊聊 Docker 怎么用", "smoke-sensitive",
                            tools=SAMPLE_TOOLS, timeout=timeout)
        r = route_of(hdr)
        fails += check("话题切换后重置回云端", st == 200 and r == "cloud",
                       "status=%s route=%s" % (st, r))

    # 5) 流式（真实模型下这条最慢，放最后）
    st, hdr, body = ask(base, sensitive_text, "smoke-stream",
                        timeout=timeout, stream=True)
    r = route_of(hdr)
    fails += check("流式请求同样被路由到本地", st == 200 and r == "local",
                   "status=%s route=%s" % (st, r))

    if verbose:
        print("\n原始响应头（最后一条）：")
        for k, v in hdr.items():
            if k.lower().startswith("x-privacy-gate") or k.lower() == "content-type":
                print("   %s: %s" % (k, v))
    return fails


def build_cfg(args, local_upstream, cloud_upstream, port):
    argv = ["--rules", args.rules, "--port", str(port), "--policy", args.policy,
            "--local-upstream", local_upstream, "--timeout", str(args.timeout)]
    if args.local_model:
        argv += ["--local-model", args.local_model]
    if cloud_upstream:
        argv += ["--cloud-upstream", cloud_upstream]
    if args.state:
        argv += ["--state", args.state]
    if args.log:
        argv += ["--log", args.log]
    if args.cloud_key_env:
        argv += ["--cloud-key-env", args.cloud_key_env]
    return gateway.Config(gateway.build_parser().parse_args(argv))


def self_test(args):
    """不需要任何真实上游：起两个假上游，跑同一组判定。"""
    print("模式：自检（内置假上游，不碰网络）")
    records = []
    cloud_port, local_port = free_port(), free_port()
    cloud_srv = ThreadingHTTPServer(("127.0.0.1", cloud_port),
                                    make_fake_upstream(records, "cloud"))
    local_srv = ThreadingHTTPServer(("127.0.0.1", local_port),
                                    make_fake_upstream(records, "local"))
    for srv in (cloud_srv, local_srv):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    rules = rules_model.load_rules(args.rules)
    sensitive, _ = pick_sensitive_text(rules)
    if not sensitive:
        print("规则里找不到可用的 high 字面模式，自检无法进行", file=sys.stderr)
        return 2

    port = free_port()
    cfg = build_cfg(args, "http://127.0.0.1:%d/v1" % local_port,
                    "http://127.0.0.1:%d/v1" % cloud_port, port)
    srv, _ = start_gateway(cfg)
    try:
        fails = run_suite("http://127.0.0.1:%d/v1/chat/completions" % port,
                          sensitive, True, args.timeout, args.verbose)
    finally:
        for s in (srv, cloud_srv, local_srv):
            try:
                s.shutdown()
            except Exception:
                pass
    print("=" * 62)
    print("自检结果：%s" % ("通过——脚本本身是好的" if not fails
                          else "%d 项不符（脚本有问题，先修它再拿去连真模型）" % fails))
    return 1 if fails else 0


def real_run(args):
    print("模式：真实上游")
    print("  本地上游：%s" % args.local_upstream)
    print("  云端上游：%s" % (args.cloud_upstream or "（未配置，全部走本地）"))
    print("  超时：%ds（CPU 推理可能很慢，必要时调大）" % args.timeout)

    rules = rules_model.load_rules(args.rules)
    sensitive, keyword = pick_sensitive_text(rules)
    if not sensitive:
        print("规则里找不到可用的 high 字面模式，无法构造敏感样本。", file=sys.stderr)
        print("可以用 --rules 指向你自己的词表。", file=sys.stderr)
        return 2
    print("  敏感样本用到了规则里的词：%s" % keyword)
    print("  断言的只是路由决策，不是模型回答质量。")
    print("")

    port = free_port()
    cfg = build_cfg(args, args.local_upstream, args.cloud_upstream or "", port)
    srv, _ = start_gateway(cfg)
    try:
        fails = run_suite("http://127.0.0.1:%d/v1/chat/completions" % port,
                          sensitive, bool(args.cloud_upstream), args.timeout,
                          args.verbose)
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass

    print("=" * 62)
    if fails:
        print("结果：%d 项不符合预期。上面前带 FAIL 的行就是线索——" % fails)
        print("如果错在\"上游真的应答了\"，多半是模型名不对或该模型没拉起。")
        return 1
    print("结果：全部符合预期。真实上游上的路由、工具剥夺、继承都生效了。")
    return 0


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="网关冒烟：验证真实上游上的路由决策")
    p.add_argument("--self-test", action="store_true",
                   help="用内置假上游跑一遍，验证脚本本身")
    p.add_argument("--local-upstream", default=os.environ.get("PRIVACY_GATE_LOCAL_UPSTREAM"),
                   help="真实本地上游（如 Ollama 的 /v1）")
    p.add_argument("--local-model", default=os.environ.get("PRIVACY_GATE_LOCAL_MODEL", ""))
    p.add_argument("--cloud-upstream", default=os.environ.get("PRIVACY_GATE_CLOUD_UPSTREAM", ""))
    p.add_argument("--cloud-key-env", default="PRIVACY_GATE_CLOUD_KEY")
    p.add_argument("--rules", default=DEFAULT_RULES)
    p.add_argument("--policy", default="reroute", choices=list(gateway.POLICIES))
    p.add_argument("--timeout", type=int, default=120, help="单次请求超时（秒）")
    p.add_argument("--state", default=None)
    p.add_argument("--log", default=None)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if not os.path.isfile(args.rules):
        print("规则文件不存在：%s" % args.rules, file=sys.stderr)
        return 2
    if args.self_test:
        return self_test(args)
    if not args.local_upstream:
        print("请给 --local-upstream，或加 --self-test 做自检。", file=sys.stderr)
        return 2
    return real_run(args)


if __name__ == "__main__":
    sys.exit(main())
