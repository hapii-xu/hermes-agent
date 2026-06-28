# Polymarket API 端点参考

所有端点均为公开 REST（GET），返回 JSON，且无需认证。

## Gamma API —— gamma-api.polymarket.com

### 搜索市场

```
GET /public-search?q=QUERY
```

响应结构：
```json
{
  "events": [
    {
      "id": "12345",
      "title": "Event title",
      "slug": "event-slug",
      "volume": 1234567.89,
      "markets": [
        {
          "question": "Will X happen?",
          "outcomePrices": "[\"0.65\", \"0.35\"]",
          "outcomes": "[\"Yes\", \"No\"]",
          "clobTokenIds": "[\"TOKEN_YES\", \"TOKEN_NO\"]",
          "conditionId": "0xabc...",
          "volume": 500000
        }
      ]
    }
  ],
  "pagination": {"hasMore": true, "totalResults": 100}
}
```

### 列出事件

```
GET /events?limit=N&active=true&closed=false&order=volume&ascending=false
```

参数：
- `limit` —— 最大结果数（默认值不固定）
- `offset` —— 分页偏移
- `active` —— true/false
- `closed` —— true/false
- `order` —— 排序字段：`volume`、`createdAt`、`updatedAt`
- `ascending` —— true/false
- `tag` —— 按标签 slug 过滤
- `slug` —— 按 slug 获取指定事件

响应：事件对象数组。每个事件包含一个 `markets` 数组。

事件字段：`id`、`title`、`slug`、`description`、`volume`、`liquidity`、
`openInterest`、`active`、`closed`、`category`、`startDate`、`endDate`、
`markets`（市场对象数组）。

### 列出市场

```
GET /markets?limit=N&active=true&closed=false&order=volume&ascending=false
```

与事件的过滤参数相同，另加：
- `slug` —— 按 slug 获取指定市场

市场字段：`id`、`question`、`conditionId`、`slug`、`description`、
`outcomes`、`outcomePrices`、`volume`、`liquidity`、`active`、`closed`、
`marketType`、`clobTokenIds`、`endDate`、`category`、`createdAt`。

重要：`outcomePrices`、`outcomes` 和 `clobTokenIds` 是 JSON 字符串
（双重编码）。在 Python 中用 json.loads() 解析。

### 列出标签

```
GET /tags
```

返回标签对象数组：`id`、`label`、`slug`。
按标签过滤事件/市场时使用 `slug` 值。

---

## CLOB API —— clob.polymarket.com

所有 CLOB 价格端点都使用市场的 `clobTokenIds` 字段中的 `token_id`。
索引 0 = Yes 结果，索引 1 = No 结果。

### 当前价格

```
GET /price?token_id=TOKEN_ID&side=buy
```

响应：`{"price": "0.650"}`

`side` 参数：`buy` 或 `sell`。

### 中间价

```
GET /midpoint?token_id=TOKEN_ID
```

响应：`{"mid": "0.645"}`

### 价差

```
GET /spread?token_id=TOKEN_ID
```

响应：`{"spread": "0.02"}`

### 订单簿

```
GET /book?token_id=TOKEN_ID
```

响应：
```json
{
  "market": "condition_id",
  "asset_id": "token_id",
  "bids": [{"price": "0.64", "size": "500"}, ...],
  "asks": [{"price": "0.66", "size": "300"}, ...],
  "min_order_size": "5",
  "tick_size": "0.01",
  "last_trade_price": "0.65"
}
```

bids 和 asks 按价格排序。size 以份额为单位（USDC 计价）。

### 价格历史

```
GET /prices-history?market=CONDITION_ID&interval=INTERVAL&fidelity=N
```

参数：
- `market` —— conditionId（带 0x 前缀的十六进制字符串）
- `interval` —— 时间范围：`all`、`1d`、`1w`、`1m`、`3m`、`6m`、`1y`
- `fidelity` —— 返回的数据点数量

响应：
```json
{
  "history": [
    {"t": 1709000000, "p": "0.55"},
    {"t": 1709100000, "p": "0.58"}
  ]
}
```

`t` 为 Unix 时间戳，`p` 为价格（概率）。

注意：非常新的市场可能返回空的历史。

### CLOB 市场列表

```
GET /markets?limit=N
```

响应：
```json
{
  "data": [
    {
      "condition_id": "0xabc...",
      "question": "Will X?",
      "tokens": [
        {"token_id": "123...", "outcome": "Yes", "price": 0.65},
        {"token_id": "456...", "outcome": "No", "price": 0.35}
      ],
      "active": true,
      "closed": false
    }
  ],
  "next_cursor": "cursor_string",
  "limit": 100,
  "count": 1000
}
```

---

## Data API —— data-api.polymarket.com

### 近期成交

```
GET /trades?limit=N
GET /trades?market=CONDITION_ID&limit=N
```

成交字段：`side`（BUY/SELL）、`size`、`price`、`timestamp`、
`title`、`slug`、`outcome`、`transactionHash`、`conditionId`。

### 未平仓量

```
GET /oi?market=CONDITION_ID
```

---

## 字段交叉参考

要从 Gamma 市场跳转到 CLOB 数据：

1. 从 Gamma 获取市场：含 `clobTokenIds` 和 `conditionId`
2. 解析 `clobTokenIds`（JSON 字符串）：`["YES_TOKEN", "NO_TOKEN"]`
3. 将 YES_TOKEN 用于 `/price`、`/book`、`/midpoint`、`/spread`
4. 将 `conditionId` 用于 `/prices-history` 以及 Data API 端点
