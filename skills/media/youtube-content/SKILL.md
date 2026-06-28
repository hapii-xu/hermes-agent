---
name: youtube-content
description: "将 YouTube 字幕转换为摘要、推文串、博客文章。"
platforms: [linux, macos, windows]
---

# YouTube 内容工具

## 何时使用

当用户分享一个 YouTube URL 或视频链接、要求总结视频、索取字幕，或希望从任何 YouTube 视频中提取并重新整理内容时使用。可将字幕转换为结构化内容（章节、摘要、推文串、博客文章）。

从 YouTube 视频中提取字幕，并将其转换为有用的格式。

## 安装

使用 `uv`，以便把依赖安装到运行辅助脚本的同一个 Hermes 托管环境中：

```bash
uv pip install youtube-transcript-api
```

## 辅助脚本

`SKILL_DIR` 是包含本 SKILL.md 文件的目录。该脚本接受任何标准 YouTube URL 格式、短链接（youtu.be）、shorts、embed、live 链接，或原始的 11 字符视频 ID。

```bash
# 带元数据的 JSON 输出
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "https://youtube.com/watch?v=VIDEO_ID"

# 纯文本（适合管道传给后续处理）
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --text-only

# 带时间戳
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --timestamps

# 指定语言并带回退链
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --language tr,en
```

## 输出格式

获取字幕后，根据用户请求的格式进行整理：

- **章节**：按话题切换分组，输出带时间戳的章节列表
- **摘要**：覆盖整段视频的精炼 5-10 句概述
- **章节摘要**：每个章节配一小段摘要
- **推文串**：Twitter/X 推文串格式 —— 编号帖子，每条不超过 280 字符
- **博客文章**：完整文章，含标题、分节和关键要点
- **金句**：带时间戳的精彩引言

### 示例 —— 章节输出

```
00:00 Introduction — host opens with the problem statement
03:45 Background — prior work and why existing solutions fall short
12:20 Core method — walkthrough of the proposed approach
24:10 Results — benchmark comparisons and key takeaways
31:55 Q&A — audience questions on scalability and next steps
```

## 工作流

1. **获取**字幕：通过 `uv run python3` 运行辅助脚本，使用 `--text-only --timestamps`。
2. **校验**：确认输出非空且为预期语言。若为空，去掉 `--language` 重试以获取任何可用字幕。若仍为空，告诉用户该视频很可能已禁用字幕。
3. **按需分块**：如果字幕超过约 5 万字符，切分为带重叠的块（约 4 万字符、2 千重叠），先分别摘要再合并。
4. **转换**为请求的输出格式。若用户未指定格式，默认使用摘要。
5. **核对**：在呈现前重读转换后的输出，检查连贯性、时间戳正确性和完整性。

## 错误处理

- **字幕被禁用**：告知用户；建议他们在视频页面查看是否有字幕可用。
- **私密/不可用的视频**：转达错误并请用户核对 URL。
- **没有匹配的语言**：去掉 `--language` 重试以获取任何可用字幕，然后把实际语言告知用户。
- **缺少依赖**：运行 `uv pip install youtube-transcript-api` 后重试。
