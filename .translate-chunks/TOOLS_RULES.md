# 翻译规则（Python 源码注释/docstring → 简体中文）

你在把 `tools/` 目录下的 Python 源码注释翻译成简体中文，目的是帮助用户学习。
**只翻译“人类可读的说明性文字”，绝不改动任何可执行代码。**

## 必须翻译（这些是注释/文档，翻译其文字内容）
- 模块 docstring（文件顶部 `"""..."""`）。
- 函数 / 方法 / 类的 docstring（`"""..."""` 或 `'''...'''`）。
- 行内注释和整行注释（`#` 开头的说明文字）。
- docstring 里的说明性段落、参数说明、示例说明等文字。
- docstring 的小节标题建议本地化：`Args:`→`参数：`、`Arguments:`→`参数：`、`Returns:`→`返回：`、`Yields:`→`生成：`、`Raises:`→`抛出：`、`Exceptions:`→`异常：`、`Note:`→`说明：`、`Notes:`→`说明：`、`Example:`→`示例：`、`Examples:`→`示例：`、`Warning:`→`警告：`、`Todo:`→`待办：`、`See Also:`→`参见：`。

## 必须保留、不要翻译（这些是代码或元信息，保持原样）
- **所有可执行代码**：语句、表达式、标识符（变量名、函数名、类名、参数名、属性名）、关键字、运算符、字面量、装饰器、类型注解（如 `-> Optional[str]`、`: list[int]`）。
- **字符串字面量**（普通 `"..."` / `'...'`，不是 docstring 的）：错误消息、日志文本、提示文案、JSON 值等一律**不翻译**——它们是运行时数据，翻译会改变行为。判断依据：`"""` 紧跟在 `def`/`class`/模块顶部之后的是 docstring（翻译）；赋值或表达式里的字符串是字面量（不翻译）。
- **lint / 类型指令注释**：`# type: ignore`、`# type: ignore[xxx]`、`# noqa`、`# noqa: E501`、`# pragma: no cover`、`# pylint: ...`、`# isort: off` 等保留原样；若同一行后面还有人类注释，只翻译人类注释部分。`noqa:` / `type:` 这些关键字本身不动。
- **shebang 和编码声明**：`#!/usr/bin/env python3`、`# -*- coding: utf-8 -*-` 不动。
- **docstring 里的 doctest 代码行**（`>>>` 开头和其输出）：保持代码原样，只翻译 doctest 周围的说明文字。
- **Sphinx 角色指令**：`:param`、`:type`、`:returns`、`:rtype`、`:raises`、`:ivar`、`:vartype` 这些角色名和紧跟的类型保留；只翻译它们后面的描述文字。例如 `:param path: the file path` → `:param path: 文件路径`。
- **魔法注释**：`# fmt: off`、`# fmt: on`、`# region`、`# endregion` 等不动。
- 文件路径、URL、环境变量名、CLI 参数（`--flag`）、版本号、尺寸单位、配置键名。

## 术语处理
- 技术缩写保持英文不翻译：API、SDK、MCP、GPU、CLI、JSON、HTTP、URL、CSS、HTML、TUI、LSP、SSE、OAuth、JWT、TLS、SSL、CSP、AST、REPL、TTS、STT、ASR 等。
- 专业术语首次出现可写「中文（English）」，之后用中文，例如「着色器（shader）」「钩子（hook）」。
- 人名、公司名、产品名、库/工具名保持英文原名：Claude、Anthropic、OpenAI、Discord、Feishu（飞书可写「飞书（Feishu）」）、Modal、Docker、Daytona、Bedrock、Gemini、Falcon、Fal 等。
- 日志级别词（INFO/DEBUG/WARNING/ERROR）保持英文。

## 格式与安全（极其重要）
- **保持 Python 语法完全有效**：缩进、空行、引号种类（`"""` vs `'''`、`"` vs `'`）、转义、续行、装饰器位置全部不变。
- 翻译后 docstring 仍是合法 docstring；行内注释仍是合法注释（`#` 不丢、`#` 后通常保留一个空格）。
- 中文注释里不要破坏 `# type:`、`# noqa` 等在同一行的位置。
- **一个文件必须完整翻译所有注释**，不要漏翻；不要只翻译一部分。
- 翻译要自然、准确、符合中文技术文档习惯，不要机翻腔。

## 方法策略（按分块指定执行）
- **Write 策略**（小文件，每个文件 <1500 行）：Read 整个文件 → 在内存里翻译所有注释/docstring → 用 Write 覆盖写回**原路径**。务必保证除注释外其余代码逐字符不变。
- **Edit 策略**（大文件，≥1500 行）：Read 整个文件 → 用 **Edit 工具**逐处把英文注释/docstring 替换为中文（一次 Edit 替换一处或多处相邻注释块）。**绝不要对大文件做整文件 Write**（会因输出过长被截断、丢失代码）。从文件顶部往下逐段处理，直到所有注释翻译完毕。

## 完成后
对每个文件简短记录：路径、翻译了多少处注释、是否遇到特殊情况（如无法判断是 docstring 还是字符串字面量）。最后汇总：处理了几个文件、共翻译多少处、有无遗留问题。
