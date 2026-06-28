---
name: polymarket
description: "查询 Polymarket：市场、价格、订单簿、历史数据。"
version: 1.0.0
author: Hermes Agent + Teknium
tags: [polymarket, prediction-markets, market-data, trading]
platforms: [linux, macos, windows]
---

# Polymarket —— 预测市场数据

使用 Polymarket 的公开 REST API 查询预测市场数据。
所有端点均为只读，且无需任何认证。

完整的端点参考（含 curl 示例）见 `references/api-endpoints.md`。

## 何时使用

- 用户询问预测市场、投注赔率或事件概率
- 用户想知道"X 发生的概率是多少？"
- 用户专门询问 Polymarket
- 用户想要市场价格、订单簿数据或价格历史
- 用户要求监控或追踪预测市场走势

## 关键概念

- **事件（Events）** 包含一个或多个 **市场（Markets）**（1:多关系）
- **市场** 是二元结果，有介于 0.00 和 1.00 之间的 Yes/No 价格
- 价格即概率：价格 0.65 表示市场认为可能性为 65%
- `outcomePrices` 字段：JSON 编码的数组，例如 `["0.80", "0.20"]`
- `clobTokenIds` 字段：JSON 编码的两个 token ID 数组 [Yes, No]，用于价格/订单簿查询
- `conditionId` 字段：十六进制字符串，用于价格历史查询
- 成交量以 USDC（美元）计价

## 三大公开 API

1. **Gamma API**，地址 `gamma-api.polymarket.com` —— 发现、搜索、浏览
2. **CLOB API**，地址 `clob.polymarket.com` —— 实时价格、订单簿、历史
3. **Data API**，地址 `data-api.polymarket.com` —— 成交、未平仓量

## 典型工作流

当用户询问预测市场赔率时：

1. 使用 Gamma API 的 public-search 端点，按其查询**搜索**
2. **解析**响应 —— 提取事件及其嵌套的市场
3. **呈现**市场问题、以百分比表示的当前价格，以及成交量
4. 如被要求**深入分析** —— 用 clobTokenIds 查订单簿，用 conditionId 查历史

## 呈现结果

为可读性起见，将价格格式化为百分比：
- outcomePrices `["0.652", "0.348"]` 应呈现为 "Yes: 65.2%, No: 34.8%"
- 始终展示市场问题和概率
- 在可用时附上成交量

示例：`"Will X happen?" — 65.2% Yes ($1.2M volume)`

## 解析双重编码字段

Gamma API 在 JSON 响应中以 JSON 字符串形式返回 `outcomePrices`、`outcomes` 和 `clobTokenIds`
（即双重编码）。用 Python 处理时，使用
`json.loads(market['outcomePrices'])` 解析以获得真正的数组。

## 速率限制

相当宽松 —— 正常使用基本不会触及：
- Gamma：每 10 秒 4,000 次请求（通用）
- CLOB：每 10 秒 9,000 次请求（通用）
- Data：每 10 秒 1,000 次请求（通用）

## 限制

- 本技能是只读的 —— 不支持下单交易
- 交易需要基于钱包的加密认证（EIP-712 签名）
- 某些新市场可能没有价格历史
- 交易受地理限制，但只读数据在全球范围可访问
