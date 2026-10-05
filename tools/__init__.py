"""
privacy_gate —— 本地 AI Agent 的隐私门禁

这个目录在仓库里叫 `tools/`，安装之后叫 `privacy_gate`。
两种身份都成立，靠的是 `pyproject.toml` 里的 `package-dir` 映射，以及各模块
开头那句 try/except 导入。

为什么保留 `tools/` 这个名字
--------------------------
opencode 插件、prompts、以及文档里的命令都按 `tools/<mod>.py` 这个路径调用它们；
把它改名会让所有既有安装路径一次性失效。仓库里叫 `tools/`、装出来叫
`privacy_gate`，是两边都能用的唯一办法。

零第三方依赖：整个包只用 Python 标准库。
"""

__version__ = "6.0.0"
