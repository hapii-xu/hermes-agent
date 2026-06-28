---
name: maps
description: "通过 OpenStreetMap/OSRM 进行地理编码、POI 查询、路线规划和时区查询。"
version: 1.2.0
author: Mibayy
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [maps, geocoding, places, routing, distance, directions, nearby, location, openstreetmap, nominatim, overpass, osrm]
    category: productivity
    requires_toolsets: [terminal]
    supersedes: [find-nearby]
---

# Maps 技能

基于免费、开放数据源的位置智能服务。提供 8 个命令、44 个 POI 类别，零依赖（仅使用 Python 标准库），无需 API 密钥。

数据来源：OpenStreetMap/Nominatim、Overpass API、OSRM、TimeAPI.io。

本技能取代了旧的 `find-nearby` 技能 —— find-nearby 的全部功能已由下方的 `nearby` 命令覆盖，并保留了同样的 `--near "<place>"` 快捷方式和多类别支持。

## 何时使用

- 用户发送了 Telegram 位置定位（消息中含纬度/经度）→ `nearby`
- 用户想要某个地点名的坐标 → `search`
- 用户有坐标，想要对应地址 → `reverse`
- 用户想找附近的餐厅、医院、药店、酒店等 → `nearby`
- 用户想要驾车/步行/骑行的距离或耗时 → `distance`
- 用户想要两地之间的逐步导航 → `directions`
- 用户想要某地的时区信息 → `timezone`
- 用户想在某个地理区域内搜索 POI → `area` + `bbox`

## 前置条件

Python 3.8+（仅标准库 —— 无需 pip 安装）。

脚本路径：`~/.hermes/skills/maps/scripts/maps_client.py`

## 命令

```bash
MAPS=~/.hermes/skills/maps/scripts/maps_client.py
```

### search —— 对地点名进行地理编码

```bash
python3 $MAPS search "Eiffel Tower"
python3 $MAPS search "1600 Pennsylvania Ave, Washington DC"
```

返回：纬度、经度、显示名、类型、外接框（bounding box）、重要度评分。

### reverse —— 坐标转地址

```bash
python3 $MAPS reverse 48.8584 2.2945
```

返回：完整地址拆解（街道、城市、州/省、国家、邮编）。

### nearby —— 按类别查找地点

```bash
# 按坐标（例如来自 Telegram 的位置定位）
python3 $MAPS nearby 48.8584 2.2945 restaurant --limit 10
python3 $MAPS nearby 40.7128 -74.0060 hospital --radius 2000

# 按地址 / 城市 / 邮编 / 地标 —— --near 会自动进行地理编码
python3 $MAPS nearby --near "Times Square, New York" --category cafe
python3 $MAPS nearby --near "90210" --category pharmacy

# 多个类别合并到一次查询中
python3 $MAPS nearby --near "downtown austin" --category restaurant --category bar --limit 10
```

46 个类别：restaurant、cafe、bar、hospital、pharmacy、hotel、guest_house、
camp_site、supermarket、atm、gas_station、parking、museum、park、school、
university、bank、police、fire_station、library、airport、train_station、
bus_stop、church、mosque、synagogue、dentist、doctor、cinema、theatre、gym、
swimming_pool、post_office、convenience_store、bakery、bookshop、laundry、
car_wash、car_rental、bicycle_rental、taxi、veterinary、zoo、playground、
stadium、nightclub。

每条结果包含：`name`、`address`、`lat`/`lon`、`distance_m`、
`maps_url`（可点击的 Google Maps 链接）、`directions_url`（从搜索点出发的
Google Maps 路线），以及在可用时附带的高亮标签 ——
`cuisine`、`hours`（opening_hours）、`phone`、`website`。

### distance —— 行驶距离与时间

```bash
python3 $MAPS distance "Paris" --to "Lyon"
python3 $MAPS distance "New York" --to "Boston" --mode driving
python3 $MAPS distance "Big Ben" --to "Tower Bridge" --mode walking
```

模式：driving（默认）、walking、cycling。返回道路距离、耗时，
以及用于对比的直线距离。

### directions —— 逐步导航

```bash
python3 $MAPS directions "Eiffel Tower" --to "Louvre Museum" --mode walking
python3 $MAPS directions "JFK Airport" --to "Times Square" --mode driving
```

返回带编号的步骤，包含指令、距离、耗时、道路名以及
动作类型（turn、depart、arrive 等）。

### timezone —— 坐标对应的时区

```bash
python3 $MAPS timezone 48.8584 2.2945
python3 $MAPS timezone 35.6762 139.6503
```

返回时区名、UTC 偏移以及当前本地时间。

### area —— 某地的外接框和面积

```bash
python3 $MAPS area "Manhattan, New York"
python3 $MAPS area "London"
```

返回外接框坐标、以 km 为单位的宽/高，以及近似面积。
可用作 bbox 命令的输入。

### bbox —— 在外接框内搜索

```bash
python3 $MAPS bbox 40.75 -74.00 40.77 -73.98 restaurant --limit 20
```

在某个地理矩形内查找 POI。先用 `area` 获取某个具名地点的
外接框坐标。

## 处理 Telegram 位置定位

当用户发送一个位置定位时，消息中包含 `latitude:` 和
`longitude:` 字段。提取它们并直接传给 `nearby`：

```bash
# 用户发送了一个位于 36.17, -115.14 的定位，并问 "find cafes nearby"
python3 $MAPS nearby 36.17 -115.14 cafe --radius 1500
```

以带编号的列表呈现结果，包含名称、距离以及
`maps_url` 字段，以便用户在聊天中获得可点击打开的链接。对于 "现在
是否营业？" 这类问题，检查 `hours` 字段；如缺失或不明确，用
`web_search` 核实，因为 OSM 的营业时间由社区维护，未必是
最新的。

## 工作流示例

**"Find Italian restaurants near the Colosseum"（查找斗兽场附近的意大利餐厅）：**
1. `nearby --near "Colosseum Rome" --category restaurant --radius 500`
   —— 一条命令，自动地理编码

**"What's near this location pin they sent?"（他们发的定位附近有什么？）：**
1. 从 Telegram 消息中提取纬度/经度
2. `nearby LAT LON cafe --radius 1500`

**"How do I walk from hotel to conference center?"（怎样从酒店步行到会议中心？）：**
1. `directions "Hotel Name" --to "Conference Center" --mode walking`

**"What restaurants are in downtown Seattle?"（西雅图市中心有哪些餐厅？）：**
1. `area "Downtown Seattle"` → 获取外接框
2. `bbox S W N E restaurant --limit 30`

## 常见陷阱

- Nominatim 服务条款：最多 1 请求/秒（由脚本自动处理）
- `nearby` 需要纬度/经度 或 `--near "<address>"` —— 二者必居其一
- OSRM 的路线覆盖以欧洲和北美最佳
- Overpass API 在高峰时段可能较慢；脚本会自动
  在镜像之间回退（overpass-api.de → overpass.kumi.systems）
- `distance` 和 `directions` 使用 `--to` 标志指定目的地（不是位置参数）
- 如果仅用邮编在全球范围得到歧义结果，请加上国家/州

## 验证

```bash
python3 ~/.hermes/skills/maps/scripts/maps_client.py search "Statue of Liberty"
# 应返回纬度约 40.689，经度约 -74.044

python3 ~/.hermes/skills/maps/scripts/maps_client.py nearby --near "Times Square" --category restaurant --limit 3
# 应返回时代广场约 500m 内的餐厅列表
```
