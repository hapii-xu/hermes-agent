---
name: gif-search
description: "通过 curl + jq 从 Tenor 搜索/下载 GIF。"
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [TENOR_API_KEY]
  commands: [curl, jq]
metadata:
  hermes:
    tags: [GIF, Media, Search, Tenor, API]
---

# GIF 搜索（Tenor API）

使用 curl 直接通过 Tenor API 搜索和下载 GIF。无需额外工具。

## 适用场景

适用于查找反应类 GIF、制作视觉内容以及在聊天中发送 GIF。

## 设置

将你的 Tenor API key 设置到环境中（添加到 `${HERMES_HOME:-~/.hermes}/.env`）：

```bash
TENOR_API_KEY=your_key_here
```

可在 https://developers.google.com/tenor/guides/quickstart 免费获取 API key — Google Cloud Console 提供的 Tenor API key 是免费的，且速率限制相当宽裕。

## 前置条件

- `curl` 和 `jq`（macOS/Linux 上均为标准工具）
- `TENOR_API_KEY` 环境变量

## 搜索 GIF

```bash
# 搜索并获取 GIF URL
curl -s "https://tenor.googleapis.com/v2/search?q=thumbs+up&limit=5&key=${TENOR_API_KEY}" | jq -r '.results[].media_formats.gif.url'

# 获取更小/预览版本
curl -s "https://tenor.googleapis.com/v2/search?q=nice+work&limit=3&key=${TENOR_API_KEY}" | jq -r '.results[].media_formats.tinygif.url'
```

## 下载 GIF

```bash
# 搜索并下载第一个结果
URL=$(curl -s "https://tenor.googleapis.com/v2/search?q=celebration&limit=1&key=${TENOR_API_KEY}" | jq -r '.results[0].media_formats.gif.url')
curl -sL "$URL" -o celebration.gif
```

## 获取完整元数据

```bash
curl -s "https://tenor.googleapis.com/v2/search?q=cat&limit=3&key=${TENOR_API_KEY}" | jq '.results[] | {title: .title, url: .media_formats.gif.url, preview: .media_formats.tinygif.url, dimensions: .media_formats.gif.dims}'
```

## API 参数

| 参数 | 说明 |
|-----------|-------------|
| `q` | 搜索查询（URL 编码时空格用 `+`） |
| `limit` | 最大结果数（1-50，默认 20） |
| `key` | API key（来自 `$TENOR_API_KEY` 环境变量） |
| `media_filter` | 筛选格式：`gif`、`tinygif`、`mp4`、`tinymp4`、`webm` |
| `contentfilter` | 安全级别：`off`、`low`、`medium`、`high` |
| `locale` | 语言：`en_US`、`es`、`fr` 等 |

## 可用的媒体格式

每个结果在 `.media_formats` 下都包含多种格式：

| 格式 | 用途 |
|--------|----------|
| `gif` | 全质量 GIF |
| `tinygif` | 小尺寸预览 GIF |
| `mp4` | 视频版本（文件更小） |
| `tinymp4` | 小尺寸预览视频 |
| `webm` | WebM 视频 |
| `nanogif` | 极小缩略图 |

## 注意事项

- 对查询进行 URL 编码：空格用 `+`，特殊字符用 `%XX`
- 在聊天中发送时，`tinygif` 的 URL 体积更小、更轻量
- GIF URL 可直接在 markdown 中使用：`![alt](url)`
