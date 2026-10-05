# 把客户端接到网关上

网关说的是 **OpenAI 兼容协议**（`/v1/chat/completions`、`/v1/models`）。
所以接法只有一句话：

> **在客户端里选"OpenAI 兼容 / 自定义 base URL"，把地址填成 `http://127.0.0.1:8787/v1`。**

不需要插件、不需要装东西、不需要客户端知道 privacy-gate 的存在。

## 前提：先把网关起起来

```bash
python tools/gateway.py \
    --cloud-upstream https://api.example.com/v1 \
    --cloud-key-env MY_CLOUD_KEY \
    --local-upstream http://127.0.0.1:11434/v1 \
    --local-model "qwen3:35b"
```

只想全部走本地（不配云端上游）也行，那就没有"公开走云"那一档，所有请求都到本地。

**确认它在跑：**

```bash
curl http://127.0.0.1:8787/healthz
```

会返回当前策略、规则条数、上游地址、以及一个**会话键来源分布**（下面第 4 节会说为什么这个数字重要）。

## 1. curl（用来验证，也用来排查）

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'x-session-id: my-test' \
  -d '{"model":"whatever","messages":[{"role":"user","content":"帮我写一份保密协议"}],"max_tokens":16}' \
  -D - -o /dev/null
```

看响应头里的那一行，它就是这次判定的全部信息：

```
x-privacy-gate: level=high policy=reroute tools-stripped=web_search,web_fetch route=local session=e78d39a6
```

`route=local` 说明它被无声重路由了——**客户端那边什么感觉都没有**，这就是设计目标。

## 2. opencode

`opencode.jsonc` 里的 provider `baseURL` 指过来即可：

```jsonc
{
  "provider": {
    "privacy-gate": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "privacy-gate gateway",
      "options": { "baseURL": "http://127.0.0.1:8787/v1" },
      "models": { "qwen3:35b": { "name": "qwen3:35b" } }
    }
  }
}
```

opencode 从"基座"变成"适配器之一"就是这个意思：它的插件仍然可以装（多一层标注），
但**强制层不再依赖它**。

## 3. Aider

Aider 读 `.env`（会依次找：家目录、git 仓库根、当前目录），配置项按[官方示例](https://aider.chat/docs/config/dotenv.html)：

```bash
# .env
AIDER_OPENAI_API_BASE=http://127.0.0.1:8787/v1
AIDER_OPENAI_API_KEY=not-needed
```

## 4. Cline（VS Code）

在设置面板里（[官方文档](https://docs.cline.bot/provider-config/openai-compatible)）：

- **API Provider** 选 `OpenAI Compatible`
- **Base URL** 填 `http://127.0.0.1:8787/v1`
- **API Key** 随便填（网关不校验它；**它只用于转发给云端上游**）
- **Model** 填你本地模型的 tag

## 5. 其他客户端

只要设置里有"OpenAI 兼容 / 自定义 base URL / API Base"这一项，就填 `http://127.0.0.1:8787/v1`。
名字各不相同，但找的东西是一样的。

> **本文没有逐一罗列几十个客户端的设置项名称**，这是刻意的：写错的配置项名比不写更浪费时间。
> 上面四条里的 opencode 来自本仓库自己的配置，curl 是通用的，Aider 与 Cline 两条按各自官方文档写。
> 其余的"找那个设置项"这件事，你自己三十秒就能确认，而我猜错一个键名要你排查半小时。

## ⚠️ 一条重要例外：Claude Code

**网关代理的是 OpenAI 那套接口（`/v1/chat/completions`）。Claude Code 说的是 Anthropic 那套
（`/v1/messages`），网关目前不代理那种格式**——所以不能靠改 base URL 把 Claude Code 接到网关上。

Claude Code 的正确接法是 [hook 适配器](../adapters/claude-code/)：
`PreToolUse` 拦远程工具、`UserPromptSubmit` 做分级。

（要不要让网关也吃 Anthropic 格式？设计文档里刻意留成待定——先占住一条接口，比同时维护两套强。）

## 接好之后要检查的两件事

**① 敏感内容真的被改路由了吗？**

```bash
curl -s -D - -o /dev/null http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"m","messages":[{"role":"user","content":"帮我写一份保密协议"}]}' \
  | grep -i x-privacy-gate
```

应该看到 `route=local`。

**② 客户端带不带会话标识？**

看 `/healthz` 里的 `session_key_fallback_ratio`。**这个数字高说明你的客户端没带
`x-session-id` 或 `user` 字段**——于是所有请求共用同一个会话，一个人的敏感会话会把
所有人拖到本地。

单人用无所谓；**多人共用一个网关实例时必须配上**，否则会话会互相污染。

要让它带上，一般是给客户端加一个自定义请求头（很多客户端支持），或者用中间层补一个
`user` 字段。网关不会替你猜会话边界——它只会老实地在 `/healthz` 里把这个比例报出来。

## 排查：请求没被按预期处理

```bash
# 网关自己的日志（每次判定一条）
python privacy_gate.py stats --top 20

# 这条规则为什么这么判
python privacy_gate.py explain "帮我写一份保密协议"

# 拿真实上游跑一遍端到端（不需要客户端参与）
python tools/gateway_smoke.py --local-upstream http://127.0.0.1:11434/v1 --local-model "qwen3:35b"
```

最后那个脚本会打真请求，并只断言**路由决策**；它自带 `--self-test`，不需要任何真实上游。
