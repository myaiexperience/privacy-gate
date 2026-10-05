#!/usr/bin/env python3
"""
泄露面审计 —— 发布前不该出现的东西，不该靠人记得检查

为什么需要它
------------
本项目要发布到公开仓库，而"发布前不暴露内网 IP、主机名、真实业务关键词"这件事
目前是靠**人记得手工跑一遍**。手工检查的问题不是不准，是**下一轮就不会再跑了**。

这与 D17 是同一条原则：能被机器守的承诺，就不要留在人的记忆里。

★ 一个必须绕开的陷阱：扫描器不能把敏感词本身写进代码
--------------------------------------------------
如果为了检查某个**业务指纹词**，就把它写进这个文件的 pattern 里，
那这个文件本身就成了泄露源——**审计工具不该是秘密的副本**。
（这段原本举了一个真实的业务词当例子，而那恰好犯了它自己警告的错。
 词表接上以后立刻被自己抓出来了——见 DECISIONS D32。）

所以这里只放**通用类别**（私网地址、用户主目录、令牌形态），
项目专属的敏感词走**本地词表**：
  - 环境变量 PRIVACY_GATE_DENY_WORDS（逗号分隔），或
  - 仓库根目录的 leaks.local.txt（每行一个词，已在 .gitignore 里，不会被提交）
两条路都不会把词带进公开仓库。

允许清单
--------
回环地址（127.0.0.1）是正常用法；RFC 5737 的文档地址段（192.0.2.x / 198.51.100.x /
203.0.113.x）就是给文档用的，必须放行——**不然写示例的人只能去编一个真实内网 IP**，
那才是真的埋雷。

主机名怎么查
------------
两条一起用：
  1. **本机主机名从环境变量推导**（COMPUTERNAME / HOSTNAME）——手写黑名单会漏，
     而环境变量是权威值；
  2. 一个**通用模式**认 Windows 默认机器名，这样连**别人机器**的名字也能抓到。
只查主机名、不查用户名：用户名常是普通英文词（runner / admin / dave），
加了词边界也会误报，在 CI 上必然变成噪音——**有噪音的检查会先被忽略、然后被删掉**。

用法：
  python check_no_leaks.py
  python check_no_leaks.py --roots tools adapters
  PRIVACY_GATE_DENY_WORDS="某某客户,某项目代号" python check_no_leaks.py
退出码：0 干净 / 1 有发现
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules",
             ".pytest_cache", ".idea", ".vscode", "dist", "build"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".7z", ".exe",
            ".dll", ".so", ".dylib", ".woff", ".woff2", ".pdf", ".pyc"}
# **本地词表文件本身必须排除。** 它按定义就装着要被查的词——不排除的话，
# 每接上一个词，审计都会先把自己报一遍（"审计器不能扫自己"）。
# 用**前缀**匹配而不是精确文件名：`leaks.local.txt.bak`、`~` 这类备份同样装着词，
# 只挡一个准确名字等于给"顺手备份一下"留了个后门。
# （这不是理论问题：接上词表的第一次运行就报了 7 处，其中 6 处来自它自己。）
SKIP_FILE_PREFIX = ("leaks.local",)
MAX_BYTES = 2 * 1024 * 1024

# 通用类别。**不要在这里放项目专属的敏感词**——见文件头说明。
#
# 下面这行把路径前缀拆开拼接，是为了让**本文件自身不命中自己的规则**：
# 审计工具把自己报成泄露源，会逼出一个更糟的解法——给扫描器开后门。
_HOME_PREFIXES = "|".join([r"[A-Za-z]:\\Users\\", "/" + "home/", "/" + "Users/"])

PATTERNS = [
    ("私网地址 192.168/16", re.compile(r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("私网地址 10/8", re.compile(r"(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("私网地址 172.16/12", re.compile(
        r"(?<![\d.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("用户主目录路径", re.compile(r"(?:" + _HOME_PREFIXES + r")([^\\/\s\"']+)")),
    ("疑似 API 令牌", re.compile(
        r"\b(?:sk|ms|ghp|gho|glpat|xoxb)-[A-Za-z0-9_\-]{12,}")),
    ("疑似私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("邮箱地址", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    # Windows 默认机器名（DESKTOP- 加 7 位）——足够独特，几乎不会误报，
    # 而且能抓到**别人机器**的名字（不只本机）。
    ("Windows 默认主机名", re.compile(r"\bDESKTOP-[A-Z0-9]{7}\b", re.IGNORECASE)),
]


def machine_tokens():
    """从环境里**推导**本机标识符——不要靠人记得把机器名写进黑名单。

    只取**主机名**，不取用户名：机器名（Windows 默认那串大写字母加数字）足够独特，
    而用户名常常是普通英文词（runner / admin / dave），即使加词边界也会误报，
    在 CI 上必然变成噪音——**有噪音的检查会先被忽略、然后被删掉**。
    用户名那条泄露路径（`C:\\Users\\<名>`）已由"用户主目录路径"模式覆盖。

    只收长度 >= 5 的，进一步压掉误报。环境变量是权威值，手写清单只会漏。
    """
    out = []
    for var in ("COMPUTERNAME", "HOSTNAME"):
        v = (os.environ.get(var) or "").strip()
        if len(v) >= 5:
            out.append((var, v))
    return out

# 放行：回环、文档地址段、演示域名、占位符写法
ALLOW = [
    re.compile(r"(?<![\d.])127\.0\.0\.\d{1,3}(?![\d.])"),
    re.compile(r"(?<![\d.])192\.0\.2\.\d{1,3}(?![\d.])"),        # RFC 5737 TEST-NET-1
    re.compile(r"(?<![\d.])198\.51\.100\.\d{1,3}(?![\d.])"),     # TEST-NET-2
    re.compile(r"(?<![\d.])203\.0\.113\.\d{1,3}(?![\d.])"),      # TEST-NET-3
    re.compile(r"0\.0\.0\.0"),
    re.compile(r"example\.(?:com|org|net)"),
    re.compile(r"users\.noreply\.gitee\.com"),
    re.compile(r"C:\\Users\\<"),          # 文档里的占位写法
    re.compile(r"/home/<|/Users/<"),
    re.compile(r"<[^>]*(?:IP|ip|地址|路径|名字|昵称|令牌|token)[^>]*>"),  # <你的Ollama服务器IP>
]

# ── 「内容扫不出问题」≠「可以发布」────────────────────────────
#
# 运行时数据（决策日志 / 纠正记录 / 会话状态）里装的是**真实输入**，
# 而通用模式（内网地址 / 用户路径 / 令牌形态）扫不出"这句话本身是业务机密"。
# 一个装着真实输入的日志被提交了，上面的内容扫描会全绿——因为它没有 IP、没有令牌。
#
# 所以再守一条：**这些文件绝不能被 git 跟踪**——被跟踪就等于会被发布。
# 当前状态是对的（全被 .gitignore 挡着），但**没有任何东西阻止某天有人
# `git add -f` 或者删掉一条 ignore 规则**。
RUNTIME_PATHS = (
    "data/routing_log.jsonl",
    "data/corrections.jsonl",
    "data/gateway_sessions.json",
    "data/claude_sessions.json",
    "leaks.local.txt",
    ".opencode/privacy-gate-state.json",
)


def tracked_runtime_files():
    """RUNTIME_PATHS 里**被 git 跟踪**的那些（被跟踪 = 会被发布）。

    返回 None 表示"查不了"（没有 git / 不在仓库里 / git 出错）——
    **"查不了"不等于"没问题"**，所以调用方必须把这两种情况分开说。
    """
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return None
    try:
        r = subprocess.run(["git", "ls-files", "--"] + list(RUNTIME_PATHS),
                           cwd=ROOT, capture_output=True, timeout=30)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.decode("utf-8", "replace").splitlines() if p.strip()]

# 用户主目录路径里这些"用户名"是文档占位，不算泄露
USERNAME_ALLOW = {"<user>", "<name>", "<你的用户名>", "studio"}


def load_deny_words():
    words = []
    env = os.environ.get("PRIVACY_GATE_DENY_WORDS", "")
    words += [w.strip() for w in env.split(",") if w.strip()]
    path = os.path.join(ROOT, "leaks.local.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line)
    return words


def is_allowed(line):
    return any(rx.search(line) for rx in ALLOW)


def iter_files(roots):
    for base in roots:
        full = base if os.path.isabs(base) else os.path.join(ROOT, base)
        if os.path.isfile(full):
            if not os.path.basename(full).startswith(SKIP_FILE_PREFIX):
                yield full
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.startswith(SKIP_FILE_PREFIX):
                    continue
                if os.path.splitext(name)[1].lower() in SKIP_EXT:
                    continue
                yield os.path.join(dirpath, name)


def scan_file(path, deny_words):
    findings = []
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return findings
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return findings
    if b"\x00" in raw:
        return findings
    text = raw.decode("utf-8", "replace")

    for lineno, line in enumerate(text.splitlines(), 1):
        if is_allowed(line):
            continue
        for label, rx in PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            if label == "用户主目录路径" and m.group(1) in USERNAME_ALLOW:
                continue
            findings.append((lineno, label, m.group(0)))
        # 本机主机名（环境变量推导；手写黑名单会漏）
        low = line.lower()
        for var, tok in machine_tokens():
            if tok.lower() in low:
                findings.append((lineno, "本机主机名（$%s）" % var, tok))
        for w in deny_words:
            if w and w in line:
                findings.append((lineno, "本地词表命中", w))
    return findings


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = list(sys.argv[1:] if argv is None else argv)
    roots = []
    i = 0
    while i < len(argv):
        if argv[i] == "--roots":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                roots.append(argv[i])
                i += 1
            continue
        roots.append(argv[i])
        i += 1
    roots = roots or ["."]

    deny_words = load_deny_words()
    total, hits = 0, []
    for path in iter_files(roots):
        total += 1
        for lineno, label, sample in scan_file(path, deny_words):
            hits.append((os.path.relpath(path, ROOT), lineno, label,
                         sample[:60].replace("\n", " ")))

    print("泄露面审计")
    print("=" * 62)
    print("扫描文件：%d 个" % total)
    print("本地词表：%s" % ("、".join(deny_words) if deny_words
                          else "（无。可用 PRIVACY_GATE_DENY_WORDS 或 leaks.local.txt 补充）"))

    # 再守一条：运行时数据不能被 git 跟踪（被跟踪 = 会被发布）。
    # 内容扫描对这类文件是**盲的**——真实输入里没有 IP、没有令牌。
    tracked = tracked_runtime_files()
    if tracked is None:
        print("  [略过] 运行数据跟踪检查：没有 git 或不可用（**这不等于没问题**）")
    elif tracked:
        for p in tracked:
            hits.append((p, 0, "运行时数据被 git 跟踪（等于会被发布）", ""))
    else:
        print("  [ OK ] 运行时数据都没被 git 跟踪（里面的真实输入不会进发布）")

    if hits:
        print("")
        for rel, lineno, label, sample in hits:
            print("  [%s] %s:%d  %s" % (label, rel, lineno, sample))
        print("")
        print("失败：发现 %d 处不该公开的内容。" % len(hits))
        print("提示：示例里要写内网地址，请用 RFC 5737 文档段（192.0.2.x /")
        print("      198.51.100.x / 203.0.113.x）——它们就是为此保留的。")
        return 1
    print("结果：未见私网地址、用户路径、令牌形态或本地词表命中。")
    if not deny_words:
        # 空的词表不是"没问题"，是"这一类没查"。别让它读起来像体检通过。
        print("      注：**本地词表是空的**——目标里那句「不暴露真实业务关键词」，")
        print("          这一类**这次没有被检查**。填你自己的词：")
        print("            echo '客户代号' >> leaks.local.txt   （已 gitignore，不会被提交）")
        print("          或 PRIVACY_GATE_DENY_WORDS='词1,词2' python check_no_leaks.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
