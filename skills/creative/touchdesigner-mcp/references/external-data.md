# 外部数据参考

网络与设备 I/O —— HTTP 请求、WebSocket、MQTT、Serial、TCP、UDP。关于 MIDI/OSC 专门内容见 `midi-osc.md`。

常见生产需求：
- API 轮询 / webhook 接收
- 实时数据流（传感器、行情、聊天）
- IoT 设备控制（Arduino、ESP32、智能灯）
- 应用间消息通信
- 在 TD 内托管一个轻量 HTTP 服务器以供远程控制

---

## Web DAT —— HTTP 请求

```python
web = root.create(webDAT, 'api_call')
web.par.url = 'https://api.example.com/v1/status'
web.par.fetchmethod = 'get'           # 'get' | 'post' | 'put' | 'delete'
web.par.format = 'auto'                # 'auto' | 'text' | 'json'
web.par.timeout = 5.0
```

**触发请求：**

`webDAT` 不会在 cook 时自动抓取。需要显式触发：

```python
web.par.fetch.pulse()
```

或通过 chopExecuteDAT 监视某 CHOP 值变化来触发（见 `dat-scripting.md`）。

**鉴权头：**

使用 `webclientDAT`（更灵活）或通过 headers DAT 设置 `webDAT` 的请求头：

```python
web_headers = root.create(tableDAT, 'headers')
web_headers.appendRow(['Authorization', 'Bearer YOUR_TOKEN'])
web_headers.appendRow(['Accept', 'application/json'])
web.par.headers = web_headers.path
```

**解析 JSON 响应：**

```python
import json

def onTableChange(dat):
    response = dat.text          # 原始响应体
    data = json.loads(response)
    # 更新 tableDAT，或存到 constantCHOP 供下游使用
    op('/project1/api_status').par.value0 = data['count']
    return
```

将其接到一个监视 webDAT 的 `datExecuteDAT` 中。

**轮询模式：**

```python
# timerCHOP 每 N 秒触发一次
timer = root.create(timerCHOP, 'poll_timer')
timer.par.length = 5.0
timer.par.cycle = True

# 在计时器 'cycles' 通道上的 chopExecuteDAT 触发 webDAT 的 fetch
def offToOn(channel, sampleIndex, val, prev):
    op('/project1/api_call').par.fetch.pulse()
    return
```

---

## Web Client DAT —— 更稳健的 HTTP

`webclientDAT` 是 `webDAT` 的现代替代品 —— 支持流式响应、分块传输、自定义鉴权。

```python
client = root.create(webclientDAT, 'api')
client.par.method = 'POST'
client.par.url = 'https://api.example.com/events'
client.par.uploadtype = 'json'
client.par.uploaddata = '{"event": "scene_change", "scene": 3}'
client.par.request.pulse()
```

输出会进入其子级 `webclient1_response` DAT。用 `datExecuteDAT` 响应。

---

## Web Server DAT —— 把 TD 当 HTTP 服务器

在 TD 内托管一个轻量 HTTP 服务器。适用于：
- 状态/健康检查端点
- 从手机或另一台机器远程控制
- 接收外部服务的 webhook

```python
server = root.create(webserverDAT, 'control_server')
server.par.port = 8080
server.par.active = True

# 在停靠的回调 DAT 中定义处理函数
```

在自动创建的 `webserver1_callbacks` DAT 中：

```python
def onHTTPRequest(webServerDAT, request, response):
    path = request['uri']
    if path == '/status':
        response['statusCode'] = 200
        response['data'] = '{"fps": 60, "scene": "active"}'
    elif path == '/scene':
        idx = int(request['args'].get('index', 0))
        op('/project1/scene_switch').par.index = idx
        response['statusCode'] = 200
        response['data'] = 'OK'
    else:
        response['statusCode'] = 404
        response['data'] = 'Not Found'
    return response
```

从终端测试：`curl http://localhost:8080/status`。

**安全：** 默认无鉴权。请仅绑定到 localhost，或在回调中加入 token 校验。切勿在无鉴权情况下暴露到公网。

---

## WebSocket DAT —— 双向实时

用于低延迟双向流（聊天、实时数据流、控制器）。

### 客户端

```python
ws = root.create(websocketDAT, 'ws_client')
ws.par.netaddress = 'wss://api.example.com/socket'
ws.par.active = True
```

在停靠的回调 DAT 中：

```python
def onConnect(dat):
    dat.sendText('{"action": "subscribe", "channel": "ticks"}')
    return

def onReceiveText(dat, rowIndex, message):
    # message 是字符串；解析 JSON，分发给算子
    import json
    data = json.loads(message)
    op('/project1/price_chop').par.value0 = data['price']
    return

def onDisconnect(dat):
    # 可选：安排重连
    return
```

### 服务端

```python
ws = root.create(websocketDAT, 'ws_server')
ws.par.mode = 'server'
ws.par.port = 9001
ws.par.active = True
```

回调结构相同，但额外带有一个 `clientID` 参数。

---

## MQTT —— 面向 IoT 的发布/订阅

```python
mqtt = root.create(mqttClientDAT, 'iot')
mqtt.par.brokeraddress = 'broker.hivemq.com'
mqtt.par.brokerport = 1883
mqtt.par.clientid = 'td_install_01'
mqtt.par.connect.pulse()

# 在回调 DAT 中订阅：
def onConnect(dat):
    dat.subscribe('home/lights/+', qos=1)
    return

def onReceive(dat, topic, payload, qos, retained, dup):
    # payload 是 bytes —— 若为 JSON 需先解码
    msg = payload.decode('utf-8')
    # 按 topic 分发
    return

# 从任意位置发布：
op('iot').publish('show/scene', 'sunset', qos=0, retain=False)
```

对于自托管的 Mosquitto / HiveMQ broker，使用相同的设置，地址填 `tcp://192.168.x.x` 和你的本地端口。

---

## Serial DAT —— Arduino、USB 设备

```python
serial = root.create(serialDAT, 'arduino')
serial.par.port = '/dev/cu.usbmodem14101'   # macOS —— 在 Arduino IDE 中查看
# Windows：'COM3'、'COM4' 等
serial.par.baudrate = 115200
serial.par.active = True
```

在回调中：

```python
def onReceive(dat, rowIndex, line):
    # Arduino 发来的每个以换行结尾的行都到这里
    parts = line.split(',')
    op('/project1/sensors').par.value0 = float(parts[0])
    op('/project1/sensors').par.value1 = float(parts[1])
    return
```

向 Arduino 发送：
```python
op('arduino').send('LED_ON\n')
```

---

## TCP/IP DAT —— 自定义协议

用于与非 HTTP 服务器通信（游戏服务器、自定义协议、遗留系统）。

```python
tcp = root.create(tcpipDAT, 'show_control')
tcp.par.netaddress = '192.168.1.50'
tcp.par.port = 7000
tcp.par.protocol = 'tcp'        # 'tcp' | 'udp'
tcp.par.active = True
```

收发通过回调完成，类似 websocketDAT。

若仅需 UDP（即发即弃、无连接），用 `udpoutDAT` + `udpinDAT` —— 更简单，但跨网络不可靠。

---

## 常见模式

### REST API → 视觉

```
timerCHOP（5 秒循环）
   → chopExecuteDAT（每个循环触发 webDAT.par.fetch）
   → webDAT（返回 JSON）
   → datExecuteDAT（解析、写入 constantCHOP）
   → CHOP 驱动 glsl uniform → 视觉
```

### Webhook 接收器

```
webserverDAT（端口 8080，/webhook 端点）
   → 回调写入 tableDAT 日志 + 触发场景切换
```

### 实时股票/加密货币行情

```
websocketDAT（订阅数据流）
   → onReceiveText 回调解析 JSON
   → 写入 constantCHOP
   → 驱动柱状图 / 文字动画
```

### IoT 控制的装置

```
MQTT → 回调按 topic 分发
   → /lights/main → constantCHOP 驱动灯光渲染
   → /audio/volume → mathCHOP 作主推子
```

### 手机双向控制

```
TD 内的 WebSocket 服务端
   → 手机上的简单 HTML 页面连接，发送滑块值
   → 回调写入算子
   → TD 通过 dat.sendText() 把状态回推到手机 UI
```

---

## 陷阱

1. **`webDAT` 不会自动抓取** —— 必须显式触发 `par.fetch`。很容易忘记。
2. **慢 API 造成阻塞** —— `webDAT` 在 cook 线程上运行。一个 30 秒的 API 调用会让 TD 卡住 30 秒。对任何可能较慢的请求使用 `webclientDAT`（异步）。
3. **WebSocket 重连** —— TD 在断开时不会自动重连。请在 `onDisconnect` 中实现退避重连。
4. **macOS 串口权限** —— TD 需要“完全磁盘访问权限”，或每会话用 `sudo chmod 666 /dev/cu.usbmodem...` 解锁端口。
5. **MQTT broker 连接状态** —— `mqttClientDAT` 可能显示 `connected=true`，但若 QoS 错误或 topic ACL 拦截，消息不会流动。请查看 broker 日志。
6. **JSON 解析错误会静默崩溃回调** —— 把解析包在 try/except 里并记录到 textport。否则回调会停止触发。
7. **Windows 防火墙** —— 首次 `webserverDAT` 绑定时，Windows 会弹出防火墙对话框。必须同意，否则服务器不可达。
8. **CORS** —— `webserverDAT` 默认不加 CORS 头。若要从不同源提供 webapp，需在响应中加入 `Access-Control-Allow-Origin: *`。
9. **轮询 vs 推送** —— 轮询会消耗 API 配额。对高频数据，优先使用 WebSocket / webhook / MQTT。
10. **浮点解析** —— 经 Serial 传来的传感器数据通常是字符串。`float()` 遇到 `'\n'` 或 `'NaN'` 会崩溃。转换前先校验。

---

## 快速配方

| 目标 | 算子链 |
|---|---|
| 周期性 API 抓取 | `timerCHOP` → `chopExecuteDAT` 触发 → `webDAT` → `datExecuteDAT` 解析 |
| Webhook 接收器 | `webserverDAT`（端口 + 路径），回调写入算子 |
| 实时数据流 | `websocketDAT` 客户端 → onReceiveText → CHOP/DAT |
| Arduino 传感器 → 视觉 | `serialDAT` → 回调 → `constantCHOP` → 在视觉算子上加表达式 |
| TD ↔ 手机控制 | `websocketDAT` 服务端 + 手机上的简单 HTML 页面 |
| MQTT IoT 集成 | `mqttClientDAT` 订阅 → 回调按 topic 分发 |
