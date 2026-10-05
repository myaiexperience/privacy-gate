# privacy-gate

> **Prompt is a promise. Put the gate on the wire.**

A privacy gate for local AI agents. It decides **which data may leave your intranet** —
in code, on the request path, without asking the model to behave and without asking your
agent framework to cooperate.

No fine-tuning. No GPU budget. No third-party runtime dependencies (Python standard
library only).

```
Your agent ──► 127.0.0.1:8787/v1 ──┬─ none        ──► cloud upstream
(opencode / Claude Code /          ├─ medium/high ──► local upstream (model rewritten,
 Cline / Cursor / any              │                  remote tools stripped)
 OpenAI-compatible client)         └─ or 403        (block policy)
```

The agent doesn't even know it was downgraded. That is the point.

**Primary repo: <https://gitee.com/playing-with-ai-x/privacy-gate>**
(GitHub mirror pending — the author's network reaches Gitee first.)

**Live demo (ModelScope Space):** <https://www.modelscope.cn/studios/xysrai/privacy-gate>
— runs the same rule engine, unmodified, so what you see there is what you get locally.

---

## The problem

Local agents need the network: docs, search, fetching pages. But the requests they carry
are mixed with data that must never leave the intranet — contracts, payroll, customer
lists, source code, keys.

The usual answer is to write the rules into the prompt. Measured result: **the model
drifts.** Over a long conversation it skips steps, forgets constraints, and "quality
reasons" its way into calling a remote tool. A prompt is a promise; it is not a gate.

This project moves the boundary out of the model and into code — and, in v6, out of the
agent framework and onto the transport layer.

## Three mechanisms

| Mechanism | What it does |
|---|---|
| **Silent re-route** | A sensitive request is rewritten to the local upstream and its `model` field replaced. The model is not consulted about this. |
| **Remote-tool stripping** | For a sensitive session, tools whose names match `*web*` / `*fetch*` / `*search*` … are **removed from `tools[]`**. The model doesn't lack permission — it lacks the ability. This is the one enforcement point no agent framework can route around. |
| **Fail-closed direction** | If the local upstream is unreachable the gateway returns **502**. It never falls back to the cloud. *A network failure must fail toward "more private", never toward "more public".* Falling back to the cloud on failure would silently turn an outage into data egress — the single most dangerous failure mode this project knows of. |

There is one more invariant: **every decision is logged and explainable.**
`x-privacy-gate: level=high policy=reroute inherited=true tools-stripped=web_search route=local`
comes back on the response, and `privacy-gate explain "…"` tells you why.

## Quick start

```bash
git clone https://gitee.com/playing-with-ai-x/privacy-gate
cd privacy-gate

# 1) Start the gateway
python tools/gateway.py \
    --cloud-upstream https://api.example.com/v1 \
    --cloud-key-env MY_CLOUD_KEY \
    --local-upstream http://127.0.0.1:11434/v1 \
    --local-model "qwen3:35b" \
    --policy reroute

# 2) Point your agent's base_url at http://127.0.0.1:8787/v1
#    (per-client settings, incl. the Claude Code exception: docs/clients.md)
# 3) Check it is alive
curl http://127.0.0.1:8787/healthz
```

No cloud upstream configured? Everything goes local. That is a valid setup.

```bash
python privacy_gate.py classify --json --stdin <<'EOF'
帮我写一份保密协议
EOF
# -> {"level": "high", "matched_keywords": ["保密"], ...}

python privacy_gate.py explain "帮我写保密协议，顺便看看收购的新闻"
python privacy_gate.py stats --top 20
python privacy_gate.py doctor
```

Three policies: `reroute` (default, actually enforces) / `block` (403, for compliance
workflows) / `annotate` (**dry run** — changes nothing, only annotates; use it to see what
the policy *would* do).

## Adapters

The gateway is the strong layer. Everything else attaches to a framework and inherits that
framework's reliability — so each adapter documents how strong it actually is, including
whether it was verified on a real machine.

| Adapter | Enforcement | Needs framework cooperation | Verified here |
|---|---|---|---|
| **[Gateway](tools/gateway.py)** | **strong** | **no** | 25 assertions |
| [opencode plugin](.opencode/plugins/privacy-gate.js) | medium | yes (v1 plugin hooks) | syntax + export contract; used daily by the author |
| [Claude Code hook](adapters/claude-code/) | medium | yes (`PreToolUse`) | protocol logic ✅, **wiring untested** |
| [MCP server](adapters/mcp/) | **weak** (voluntary) | yes (the model must choose to call it) | protocol layer |
| CLI | — | — | ✅ |

Two deliberate choices worth calling out:

- **The MCP adapter is read-only.** Exposing a "change the rules" tool would put the keys
  to the gate on the gate. Not happening.
- **Hooks are designed as if they will fail.** Every platform's hook layer has a history of
  bugs — opencode's `permission.ask`, and Claude Code's
  [#43407](https://github.com/anthropics/claude-code/issues/43407) /
  [#39344](https://github.com/anthropics/claude-code/issues/39344) /
  [#18312](https://github.com/anthropics/claude-code/issues/18312). So hook adapters return
  **deny** when their own internals break, and the gateway is what you rely on.

## The rule contract (v4)

The rules file is a contract, not a word list — and it used to be the latter, which meant
you had to read the engine's source to know what was actually honoured.

```json
{
  "schema": "v4",
  "rules": [
    { "id": "high-default", "level": "high", "action": "block_remote",
      "match": { "type": "substring", "patterns": ["保密", "contract"] } },
    { "id": "pricing-question", "level": "medium", "action": "prefer_local",
      "match": { "type": "regex", "pattern": "(底价|成本价)\\s*(是|多少)" } }
  ],
  "exceptions": [
    { "id": "acquisition-in-public-news", "demote_to": "none",
      "applies_to": [ { "rule": "high-default", "patterns": ["收购"] } ],
      "when": { "type": "substring", "patterns": ["新闻", "公告", "公开报道"] } }
  ],
  "topic_shift_keywords": ["换个话题"],
  "remote_tool_patterns": ["*web*", "*fetch*", "*search*"]
}
```

- **Match primitives are configurable**: `substring` / `word` / `regex`.
  ASCII terms in `substring` get word boundaries automatically (so `veranda` never matches
  `NDA` — that was a real bug, not a hypothetical).
- **`action` is actually read** by the engine now.
- **Exceptions are pattern-scoped, and that matters.** Scoping them to a whole *rule* would
  mean that exempting `收购` in news contexts also disables `保密` in the same rule — so
  *"write me an NDA, and by the way check the news"* would slip through. That would be a
  security hole, and it is now a regression test.
- v3 files are auto-detected and converted; nothing breaks on upgrade.

## Observability is not optional here

Silent re-routing has a cost: a false positive no longer *blocks* something you can see —
it quietly hands your request to a weaker local model and you never learn why the answer
got worse. So:

```bash
python privacy_gate.py explain "…"   # why is this level what it is
python privacy_gate.py stats         # which rules are noisiest, how often inheritance fired
```

`stats` reads the decision log and prints **aggregates only — never your input text**.
There is an automated test that plants a sentinel string in the log and asserts it never
appears in the report.

## What is verified, and how

```
python check.py           # the full doctor (coverage listed below, no fixed count:
                          # it varies with layout — see the note in check_docs.py)
python test_routes.py     # 61 assertions — classification, inheritance, exemptions,
                          # v3→v4 conversion and write-back without word loss
python test_gateway.py    # 38 assertions — re-route, tool stripping, fail-closed, SSE,
                          # upstream URL joining (POST + GET), field passthrough,
                          # no client-header leak, which key goes to which leg, banner
python test_adapters.py   # 45 assertions — MCP protocol (spec-conformant version
                          # negotiation + tool annotations), hook decisions (fail-closed +
                          # exit code 2), CLI, plus the JS↔Python field contract (D19)
python check_zero_deps.py # every import is stdlib — keeps the "zero deps" claim honest
python check_no_leaks.py  # no private IPs, user paths, token shapes, hostnames (incl.
                          # this machine's, derived from the environment), or local deny
                          # words — plus: runtime data must not be tracked by git
python check_docs.py      # every script, subcommand and link in the docs actually exists
```

The gateway tests run two fake upstreams (cloud and local) around the gateway, so
"the cloud never received this request" is an assertion, not a claim. The fail-closed
direction test points the local upstream at a dead port and asserts the cloud upstream
received **zero** requests.

Two claims are machine-guarded rather than promised: *zero third-party dependencies*
(`check_zero_deps.py` resolves each import and fails if it lives in `site-packages`), and
*no internal information in the repo* (`check_no_leaks.py` — which contains no
project-specific secret words itself, because an auditor that embeds the secrets is just
another copy of them).

## Honest limitations

This is a single-person homelab project. It is not a compliance control. Read this list
before trusting it with anything:

- **`bash` + `curl` walks around the gateway.** The gateway sits on the LLM API path; it
  cannot stop an arbitrary process from opening a socket. Closing that requires OS-level
  egress control (firewall allowing only the gateway). We document the recipe and **do not
  implement it**.
- **Keyword matching is a heuristic, not a guarantee.** Substring matching produces false
  positives (`收购` used to block public news searches — fixed with an exception) and poor
  recall for anything not literally containing a keyword ("that file from Dave"). The
  correction loop only learns what you notice.
- **Silent re-routing makes false positives invisible.** See above. This is a real trade-off,
  mitigated — not solved — by the response header and `stats`.
- **Tool stripping matches on tool *names*.** Naming conventions differ per platform; the
  Claude Code adapter deliberately ships its own default patterns rather than reusing this
  project's OpenAI-shaped ones, because they would over-match local MCP tools.
- **Sessions are keyed by request metadata.** OpenAI's API has no session concept. If a
  client sends no `x-session-id` / `user`, everything falls back to one shared session —
  correct (over-inherit) but coarse. `/healthz` reports the fallback ratio.
- **Python 3.9+ is the stated floor and the machine this was written on only has 3.12.**
  The CI matrix covers 3.9 / 3.12 / 3.13; treat the lower bound as *asserted by CI*, not
  verified by hand here.
- **The gateway has run against a real local upstream (llama.cpp / Ling-3.0-tiny), but never
  against Ollama.** The assertions in `test_gateway.py` wrap the gateway with a fake cloud and
  a fake local upstream, which proves the *mechanism* — re-route, tool stripping, inheritance,
  fail-closed direction. It does not prove it *works*. So I later pointed it at a **real** local
  OpenAI-compatible server (the llama.cpp backend bundled with LM Studio on this machine):

  > **First run: all 5 cases FAILED.** Routing was perfect (`route=local`, tools stripped,
  > inheritance firing) — and the upstream returned 404. The gateway was concatenating the
  > upstream base with the client path, doubling the prefix (`.../v1/v1/...`). All 26
  > assertions were green because **the fake upstream accepted any path**. An over-permissive
  > test double had been masking a real defect as "correct". See DECISIONS D23.

  Fixed (replace the client's `/v1` instead of appending, and make the fakes path-strict) —
  all 5 cases then pass. **The cloud leg is verified too**: pointed at ModelScope's inference
  API (a real cloud OpenAI-compatible endpoint), all five cases pass — including "topic shift
  resets back to the cloud", which requires *both* legs to be right.
  **Ollama itself remains unverified**: its OpenAI-compatible layer is a different
  implementation from llama.cpp's. One command closes that gap:

  ```bash
  python tools/gateway_smoke.py --local-upstream http://<your-ollama>:11434/v1 \
      --local-model <model>
  ```

  It sends real requests and asserts only the **routing decision**, never answer quality.
  The script has a `--self-test` that needs no upstream and runs in CI — so it cannot rot
  before you use it.
- **Single-person homelab.** Not production-grade, not load-tested. Issues and PRs welcome.

## Why this repo might still be worth your time

The design log is the most valuable thing here: six iterations, each recording **what was
cut and what it cost** — measured, not theorised.

A 1.5B router model was built, measured, and deleted (it was not smart enough to classify
privacy, and on CPU inference it stole memory bandwidth from the 35B worker). A four-subagent
pipeline was cut back to one (each subagent turn is a full context rebuild; with CPU inference
the latency was unacceptable). Both `annotate` semantics and the session-key derivation were
rewritten **because implementation contradicted the paper design** — four separate times in
one session.

→ [DECISIONS.md](DECISIONS.md)

## License

[MIT](LICENSE) — code. Docs live in the same repo; reuse freely with attribution.
