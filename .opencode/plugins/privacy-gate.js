/**
 * privacy-gate —— 隐私门禁插件（opencode v1 经典插件 API）
 *
 * 用 CommonJS .js 编写：CLI 版（Bun）与桌面版（Node.js sidecar）都能直接加载，
 * 不依赖 TypeScript 类型剥离，避免 sidecar Node 版本差异导致的加载失败。
 *
 * 职责（三层防线 + 云端护栏）：
 *   1. chat.message          用户消息到达 → 运行 tools/rules_engine.py 分级（带多轮继承）
 *                              → 注入标注 [系统隐私检测] effective_level=... 给模型
 *                              → 云端 agent（cloud / deepseek 等云模型）
 *                                收到非 none 内容时直接抛错拦截（best-effort）
 *   2. permission.ask        远程工具即将询问用户时按级别裁决（high 直接 deny，
 *                              medium 禁 webfetch，none 自动放行）——增强层，
 *                              历史上有 bug/回归，不能当唯一防线
 *   3. tool.execute.before   工具执行前硬拦截（throw 阻断）——强制层，最可靠
 *
 * 安装：把本文件放到项目的 .opencode/plugins/ 目录（或 ~/.config/opencode/plugins/ 全局），
 *       启动 opencode 即自动加载，无需在 opencode.json 里配置（v1 API）。
 *
 * 依赖：无第三方包（只用 node: 内置模块）。
 *       需要本机可执行 python 或 python3，且引擎路径存在。
 *
 * 降级策略（fail-closed）：
 *   - 引擎调用失败 / 解析失败 → 按 medium 处理（禁远程抓取，谨慎搜索）
 *   - 插件未加载（如桌面版环境异常）→ worker prompt 里有手动兜底路径
 *   - 会话状态只存内存 + .opencode/privacy-gate-state.json（跨重启恢复继承）
 */

const { spawnSync } = require("node:child_process")
const fs = require("node:fs")
const path = require("node:path")

const LEVELS = new Set(["none", "medium", "high"])
const levels = new Map()

/** 引擎脚本候选路径（按顺序找第一个存在的） */
function enginePath(directory) {
  const candidates = [
    path.join(directory, "tools", "rules_engine.py"),
    process.env.PRIVACY_GATE_ENGINE || "",
  ].filter(Boolean)
  for (const p of candidates) {
    if (fs.existsSync(p)) return p
  }
  return null
}

/** 状态文件（跨 opencode 重启恢复多轮继承） */
function stateFile(directory) {
  return path.join(directory, ".opencode", "privacy-gate-state.json")
}

function loadState(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"))
    for (const [k, v] of Object.entries(data)) {
      if (v && LEVELS.has(v.level)) {
        levels.set(k, { level: v.level, updatedAt: Date.now() })
      }
    }
  } catch {
    /* 首次运行或文件损坏，忽略 */
  }
}

function saveState(file) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, JSON.stringify(Object.fromEntries(levels), null, 2))
  } catch {
    /* 状态写入失败不影响门禁 */
  }
}

/** 防御式提取用户消息文本（不同版本 SDK 字段名有差异） */
function extractText(message) {
  const m = message || {}
  if (typeof m.text === "string" && m.text) return m.text
  if (typeof m.content === "string") return m.content
  const parts = Array.isArray(m.parts) ? m.parts : Array.isArray(m.content) ? m.content : []
  return parts
    .filter((p) => p && p.type === "text" && typeof p.text === "string")
    .map((p) => p.text)
    .join("\n")
}

/** 防御式获取工具名（不同版本可能是字符串或对象） */
function toolName(input) {
  const t = input && input.tool
  if (typeof t === "string") return t
  if (t && typeof t === "object") return String(t.name ?? t.id ?? t.tool ?? "")
  return ""
}

/** 防御式获取 agent 名 / 模型名（用于云端护栏判断） */
function agentNameOf(input) {
  const a = input && input.agent
  if (typeof a === "string") return a
  if (a && typeof a === "object") return String(a.name ?? a.id ?? "")
  return ""
}

function modelNameOf(input) {
  const m = input && input.model
  if (typeof m === "string") return m
  if (m && typeof m === "object") return String(m.id ?? m.modelID ?? "")
  return ""
}

/** 判断当前消息是否会发送给云端模型（云端模型禁止接触非 none 内容） */
function isCloudTarget(input) {
  const a = agentNameOf(input).toLowerCase()
  const m = modelNameOf(input).toLowerCase()
  return (
    a.includes("cloud") ||
    m.includes("opencode/") ||
    m.includes("deepseek") ||
    m.includes("anthropic") ||
    m.includes("claude") ||
    m.includes("openai") ||
    m.includes("gpt")
  )
}

function hasExec(name) {
  try {
    // 注意：必须校验 status===0，Windows 商店的 python3.exe 占位 stub 能 spawn 但跑不起来
    const r = spawnSync(name, ["--version"], { timeout: 2000, stdio: "ignore" })
    return r.error === undefined && r.status === 0
  } catch {
    return false
  }
}

/** 调引擎分级；失败时返回 fail-closed 结果 */
function classify(directory, sessionID, prev, text) {
  const candidates = process.platform === "win32" ? ["py", "python", "python3"] : ["python3", "python"]
  const py = process.env.PRIVACY_GATE_PYTHON || candidates.find(hasExec) || "python"
  const engine = enginePath(directory)
  if (!engine) {
    console.warn("[privacy-gate] 未找到 tools/rules_engine.py，本次按 medium（默认保守）")
    return fallback()
  }
  try {
    const r = spawnSync(
      py,
      [engine, "--json", "--log", "--session-id", sessionID, "--prev-level", prev, "--source", "plugin", "--stdin"],
      { input: text, encoding: "utf8", timeout: 5000 },
    )
    if (r.error) throw r.error
    const data = JSON.parse((r.stdout || "").trim())
    // ⚠️ 只有拿到一个**能识别**的级别才算成功。
    //
    // 这里曾经写成「不是 high/medium 就当作 none」，那是一条 fail-open 路径：
    // 引擎字段改名、引擎打印 {"error": ...}、或者输了别的 JSON，
    // 门禁就会把远程工具**全打开**——而门禁失效时放开远程访问，
    // 正是本项目最不能有的失败方向（同理见 DECISIONS D15 的 fail-closed 方向性）。
    //
    // 教训：fail-closed 不只是"出错时怎么办"，还包括"看不懂时怎么办"。
    if (!LEVELS.has(data.effective_level)) {
      console.warn(
        `[privacy-gate] 引擎输出里没有可识别的 effective_level，按 medium（默认保守）: ` +
        `${JSON.stringify(data).slice(0, 160)}`,
      )
      return fallback()
    }
    return {
      level: data.effective_level,
      matched: Array.isArray(data.matched_keywords) ? data.matched_keywords : [],
      inherited: Boolean(data.inherited),
      topicShift: Boolean(data.topic_shift),
      ok: true,
    }
  } catch (e) {
    console.warn(`[privacy-gate] 引擎调用失败，按 medium（默认保守）: ${String(e)}`)
    return fallback()
  }
}

function fallback() {
  return { level: "medium", matched: [], inherited: false, topicShift: false, ok: false }
}

async function PrivacyGate(ctx) {
  const directory = (ctx && ctx.directory) || process.cwd()
  loadState(stateFile(directory))

  return {
    // ── 第 1 层：消息到达 → 分级 + 记住 + 注入标注 + 云端护栏 ──
    "chat.message": async (input, output) => {
      const sid = (input && input.sessionID) || "unknown"
      const out = output || {}
      const text = extractText(out.message) || extractText({ parts: out.parts })

      if (!text) return // 拿不到文本就跳过（worker 兜底路径仍会自己跑引擎）

      const prev = (levels.get(sid) || { level: "none" }).level
      const result = classify(directory, sid, prev, text)
      // 引擎失败（ok=false）时：不落会话状态（避免"假 medium"污染后续轮次），
      // 也不做消息级拦截（否则每条消息都抛错、输入完全锁死）。
      // fail-closed 仍由 permission.ask / tool.execute.before 两层兜住远程工具。
      if (result.ok) {
        levels.set(sid, { level: result.level, updatedAt: Date.now() })
        saveState(stateFile(directory))
      }

      // 云端护栏（best-effort）：仅在引擎正常给出判定、且判定为非公开时拦截。
      // 若当前版本 chat.message 抛错不阻断消息，worker.md / cloud.md 的规则仍会兜底。
      if (result.ok && result.level !== "none" && isCloudTarget(input)) {
        throw new Error(
          `[privacy-gate] 会话级别=${result.level}，云端模型只能处理公开任务。` +
          `敏感内容请改用本地 @worker（或让用户显式授权后重试）`,
        )
      }

      const matchedStr = result.matched.length ? result.matched.join(",") : "无"
      const annotation =
        `\n[系统隐私检测] effective_level=${result.level} matched=${matchedStr} ` +
        `inherited=${result.inherited}${result.ok ? "" : " engine_error=true"}`

      // 防御式注入：只把标注追加到已有文本 part 的 text 字段上，绝不 push 新 part。
      // 桌面端会把消息保存为 durable event，新增 part 缺 id/sessionID/messageID
      // 会触发 "invalid user part before save"，导致消息保存失败、每次输入都报错。
      try {
        const parts =
          Array.isArray(out.parts)
            ? out.parts
            : out.message && Array.isArray(out.message.parts)
              ? out.message.parts
              : null
        if (parts) {
          for (let i = parts.length - 1; i >= 0; i--) {
            const p = parts[i]
            if (p && p.type === "text" && typeof p.text === "string") {
              p.text += annotation
              break
            }
          }
        } else if (out.message && typeof out.message.text === "string") {
          out.message.text += annotation
        } else if (out.message && typeof out.message.content === "string") {
          out.message.content += annotation
        }
      } catch {
        /* 注入失败不阻断消息；worker 有手动兜底 */
      }
    },

    // ── 第 2 层：权限询问裁决（增强层，不可靠也不致命）──
    "permission.ask": async (input, output) => {
      const type = String((input && input.type) || "")
      if (type !== "webfetch" && type !== "websearch") return
      const level = (levels.get((input && input.sessionID) || "unknown") || { level: "none" }).level
      const out = output || {}
      const deny = (reason) => {
        if (out && typeof out === "object") {
          out.status = "deny"
          if ("reason" in out) out.reason = reason
        }
      }
      if (level === "high") {
        deny(`[privacy-gate] 级别=high，禁止远程${type === "webfetch" ? "抓取" : "搜索"}`)
      } else if (type === "webfetch" && level === "medium") {
        deny("[privacy-gate] 级别=medium，禁止远程抓取（webfetch）")
      } else if (level === "none") {
        if (out && typeof out === "object") out.status = "allow"
      }
      // medium + websearch：不改动，交用户看搜索词后裁决
    },

    // ── 第 3 层：工具执行前硬拦截（强制层，最可靠）──
    "tool.execute.before": async (input) => {
      const name = toolName(input)
      if (name !== "webfetch" && name !== "websearch") return
      const level = (levels.get((input && input.sessionID) || "unknown") || { level: "none" }).level
      if (name === "webfetch" && level !== "none") {
        throw new Error(`[privacy-gate] 会话级别=${level}，已阻止远程抓取 webfetch`)
      }
      if (name === "websearch" && level === "high") {
        throw new Error(`[privacy-gate] 会话级别=high，已阻止远程搜索 websearch`)
      }
    },
  }
}

// 注意：必须直接导出函数（module.exports = fn）。
// 桌面端（Node sidecar）的插件加载器只认"导出本身是函数"；
// 具名导出对象 { PrivacyGate } 会报 "Plugin export is not a function" 导致插件不加载。
module.exports = PrivacyGate
