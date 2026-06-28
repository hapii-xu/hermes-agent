---
name: ascii-art
description: "ASCII 艺术：pyfiglet、cowsay、boxes、图片转 ASCII。"
version: 4.0.0
author: 0xbyt4, Hermes Agent
license: MIT
dependencies: []
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ASCII, Art, Banners, Creative, Unicode, Text-Art, pyfiglet, figlet, cowsay, boxes]
    related_skills: [excalidraw]

---

# ASCII 艺术 Skill

多种工具满足不同的 ASCII 艺术需求。所有工具都是本地 CLI 程序或免费的 REST API —— 无需 API 密钥。

## 工具 1：文本横幅（pyfiglet —— 本地）

把文字渲染成大型 ASCII 艺术横幅。内置 571 款字体。

### 安装

```bash
pip install pyfiglet --break-system-packages -q
```

### 用法

```bash
python3 -m pyfiglet "YOUR TEXT" -f slant
python3 -m pyfiglet "TEXT" -f doom -w 80    # 设置宽度
python3 -m pyfiglet --list_fonts             # 列出全部 571 款字体
```

### 推荐字体

| 风格 | 字体 | 最适合 |
|-------|------|----------|
| 干净现代 | `slant` | 项目名称、标题 |
| 粗壮块状 | `doom` | 标题、徽标 |
| 大号易读 | `big` | 横幅 |
| 经典横幅 | `banner3` | 宽屏展示 |
| 紧凑 | `small` | 副标题 |
| 赛博朋克 | `cyberlarge` | 科技主题 |
| 3D 效果 | `3-d` | 启动画面 |
| 哥特风 | `gothic` | 戏剧化文字 |

### 技巧

- 预览 2-3 款字体，让用户挑选最喜欢的
- 短文本（1-8 个字符）适合搭配 `doom` 或 `block` 等细节丰富的字体
- 长文本更适合 `small` 或 `mini` 等紧凑字体

## 工具 2：文本横幅（asciified API —— 远程，无需安装）

免费的 REST API，把文字转换成 ASCII 艺术。提供 250+ 款 FIGlet 字体。直接返回纯文本 —— 无需解析。当 pyfiglet 未安装时，或作为快速替代方案时使用。

### 用法（通过终端 curl）

```bash
# 基础文本横幅（默认字体）
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello+World"

# 指定字体
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Slant"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Doom"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Star+Wars"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=3-D"
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=Hello&font=Banner3"

# 列出所有可用字体（返回 JSON 数组）
curl -s "https://asciified.thelicato.io/api/v2/fonts"
```

### 技巧

- 文本参数中的空格用 `+` 进行 URL 编码
- 响应是纯文本 ASCII 艺术 —— 没有 JSON 包裹，可直接展示
- 字体名称区分大小写；用字体列表接口获取准确名称
- 任何带 curl 的终端都能用 —— 无需 Python 或 pip

## 工具 3：Cowsay（消息艺术）

经典工具，把文字包进带 ASCII 角色的对话气泡里。

### 安装

```bash
sudo apt install cowsay -y    # Debian/Ubuntu
# brew install cowsay         # macOS
```

### 用法

```bash
cowsay "Hello World"
cowsay -f tux "Linux rules"       # 企鹅 Tux
cowsay -f dragon "Rawr!"          # 龙
cowsay -f stegosaurus "Roar!"     # 剑龙
cowthink "Hmm..."                  # 思考气泡
cowsay -l                          # 列出所有角色
```

### 可用角色（50+）

`beavis.zen`, `bong`, `bunny`, `cheese`, `daemon`, `default`, `dragon`,
`dragon-and-cow`, `elephant`, `eyes`, `flaming-skull`, `ghostbusters`,
`hellokitty`, `kiss`, `kitty`, `koala`, `luke-koala`, `mech-and-cow`,
`meow`, `moofasa`, `moose`, `ren`, `sheep`, `skeleton`, `small`,
`stegosaurus`, `stimpy`, `supermilker`, `surgery`, `three-eyes`,
`turkey`, `turtle`, `tux`, `udder`, `vader`, `vader-koala`, `www`

### 眼睛/舌头修饰符

```bash
cowsay -b "Borg"       # =_= 眼睛
cowsay -d "Dead"       # x_x 眼睛
cowsay -g "Greedy"     # $_$ 眼睛
cowsay -p "Paranoid"   # @_@ 眼睛
cowsay -s "Stoned"     # *_* 眼睛
cowsay -w "Wired"      # O_O 眼睛
cowsay -e "OO" "Msg"   # 自定义眼睛
cowsay -T "U " "Msg"   # 自定义舌头
```

## 工具 4：Boxes（装饰边框）

给任何文字绘制装饰性 ASCII 艺术边框/框。内置 70+ 款设计。

### 安装

```bash
sudo apt install boxes -y    # Debian/Ubuntu
# brew install boxes         # macOS
```

### 用法

```bash
echo "Hello World" | boxes                    # 默认框
echo "Hello World" | boxes -d stone           # 石头边框
echo "Hello World" | boxes -d parchment       # 羊皮纸卷轴
echo "Hello World" | boxes -d cat             # 猫边框
echo "Hello World" | boxes -d dog             # 狗边框
echo "Hello World" | boxes -d unicornsay      # 独角兽
echo "Hello World" | boxes -d diamonds        # 菱形图案
echo "Hello World" | boxes -d c-cmt           # C 风格注释
echo "Hello World" | boxes -d html-cmt        # HTML 注释
echo "Hello World" | boxes -a c               # 文本居中
boxes -l                                       # 列出全部 70+ 款设计
```

### 与 pyfiglet 或 asciified 组合

```bash
python3 -m pyfiglet "HERMES" -f slant | boxes -d stone
# 或者在未安装 pyfiglet 时：
curl -s "https://asciified.thelicato.io/api/v2/ascii?text=HERMES&font=Slant" | boxes -d stone
```

## 工具 5：TOIlet（彩色文字艺术）

类似 pyfiglet，但带 ANSI 颜色效果和视觉滤镜。非常适合终端里的视觉效果。

### 安装

```bash
sudo apt install toilet toilet-fonts -y    # Debian/Ubuntu
# brew install toilet                      # macOS
```

### 用法

```bash
toilet "Hello World"                    # 基础文字艺术
toilet -f bigmono12 "Hello"            # 指定字体
toilet --gay "Rainbow!"                 # 彩虹配色
toilet --metal "Metal!"                 # 金属效果
toilet -F border "Bordered"             # 加边框
toilet -F border --gay "Fancy!"         # 组合效果
toilet -f pagga "Block"                 # 块状字体（toilet 独有）
toilet -F list                          # 列出可用滤镜
```

### 滤镜

`crop`, `gay`（彩虹）, `metal`, `flip`, `flop`, `180`, `left`, `right`, `border`

**注意**：toilet 输出 ANSI 转义码来上色 —— 在终端里能正常显示，但在某些场景（例如纯文本文件、部分聊天平台）中可能无法渲染。

## 工具 6：图片转 ASCII 艺术

把图片（PNG、JPEG、GIF、WEBP）转换成 ASCII 艺术。

### 选项 A：ascii-image-converter（推荐，现代）

```bash
# 安装
sudo snap install ascii-image-converter
# 或者：go install github.com/TheZoraiz/ascii-image-converter@latest
```

```bash
ascii-image-converter image.png                  # 基础
ascii-image-converter image.png -C               # 彩色输出
ascii-image-converter image.png -d 60,30         # 设置尺寸
ascii-image-converter image.png -b               # 盲文字符
ascii-image-converter image.png -n               # 反相/负片
ascii-image-converter https://url/image.jpg      # 直接用 URL
ascii-image-converter image.png --save-txt out   # 保存为文本
```

### 选项 B：jp2a（轻量，仅支持 JPEG）

```bash
sudo apt install jp2a -y
jp2a --width=80 image.jpg
jp2a --colors image.jpg              # 上色
```

## 工具 7：搜索现成的 ASCII 艺术

从网上搜索精选的 ASCII 艺术。用 `terminal` 配合 `curl`。

### 来源 A：ascii.co.uk（推荐用于现成艺术）

大型经典 ASCII 艺术合集，按主题分类。艺术内容位于 HTML `<pre>` 标签内。先用 curl 抓取页面，再用一小段 Python 脚本提取艺术内容。

**URL 模式：** `https://ascii.co.uk/art/{subject}`

**第 1 步 —— 抓取页面：**

```bash
curl -s 'https://ascii.co.uk/art/cat' -o /tmp/ascii_art.html
```

**第 2 步 —— 从 pre 标签中提取艺术内容：**

```python
import re, html
with open('/tmp/ascii_art.html') as f:
    text = f.read()
arts = re.findall(r'<pre[^>]*>(.*?)</pre>', text, re.DOTALL)
for art in arts:
    clean = re.sub(r'<[^>]+>', '', art)
    clean = html.unescape(clean).strip()
    if len(clean) > 30:
        print(clean)
        print('\n---\n')
```

**可用主题**（用作 URL 路径）：
- 动物：`cat`, `dog`, `horse`, `bird`, `fish`, `dragon`, `snake`, `rabbit`, `elephant`, `dolphin`, `butterfly`, `owl`, `wolf`, `bear`, `penguin`, `turtle`
- 物品：`car`, `ship`, `airplane`, `rocket`, `guitar`, `computer`, `coffee`, `beer`, `cake`, `house`, `castle`, `sword`, `crown`, `key`
- 自然：`tree`, `flower`, `sun`, `moon`, `star`, `mountain`, `ocean`, `rainbow`
- 角色：`skull`, `robot`, `angel`, `wizard`, `pirate`, `ninja`, `alien`
- 节日：`christmas`, `halloween`, `valentine`

**技巧：**
- 保留艺术家的署名/缩写 —— 这点很重要，是基本礼仪
- 每个页面有多件作品 —— 为用户挑最好的一件
- 通过 curl 即可稳定工作，不需要 JavaScript

### 来源 B：GitHub Octocat API（有趣的彩蛋）

返回一只随机的 GitHub Octocat 配上一句睿智语录。无需鉴权。

```bash
curl -s https://api.github.com/octocat
```

## 工具 8：趣味 ASCII 工具（通过 curl）

这些免费服务直接返回 ASCII 艺术 —— 非常适合做趣味附加内容。

### 二维码转 ASCII 艺术

```bash
curl -s "qrenco.de/Hello+World"
curl -s "qrenco.de/https://example.com"
```

### 天气转 ASCII 艺术

```bash
curl -s "wttr.in/London"          # 带 ASCII 图形的完整天气报告
curl -s "wttr.in/Moon"            # ASCII 艺术月相
curl -s "v2.wttr.in/London"       # 详细版本
```

## 工具 9：LLM 生成的自定义艺术（兜底方案）

当上面的工具都不满足需求时，直接用以下 Unicode 字符生成 ASCII 艺术：

### 字符调色板

**制表符（Box Drawing）：** `╔ ╗ ╚ ╝ ║ ═ ╠ ╣ ╦ ╩ ╬ ┌ ┐ └ ┘ │ ─ ├ ┤ ┬ ┴ ┼ ╭ ╮ ╰ ╯`

**块元素（Block Elements）：** `░ ▒ ▓ █ ▄ ▀ ▌ ▐ ▖ ▗ ▘ ▝ ▚ ▞`

**几何与符号：** `◆ ◇ ◈ ● ○ ◉ ■ □ ▲ △ ▼ ▽ ★ ☆ ✦ ✧ ◀ ▶ ◁ ▷ ⬡ ⬢ ⌂`

### 规则

- 最大宽度：每行 60 个字符（终端安全）
- 最大高度：横幅 15 行，场景 25 行
- 仅限等宽：输出必须在固定宽度字体下正确渲染

## 决策流程

1. **把文字做成横幅** → 已装 pyfiglet 就用它，否则用 curl 调 asciified API
2. **把消息包进趣味角色艺术** → cowsay
3. **加装饰边框/外框** → boxes（可与 pyfiglet/asciified 组合）
4. **某个具体事物的艺术**（猫、火箭、龙）→ 用 curl 抓 ascii.co.uk 再解析
5. **把图片转成 ASCII** → ascii-image-converter 或 jp2a
6. **二维码** → 用 curl 调 qrenco.de
7. **天气/月相艺术** → 用 curl 调 wttr.in
8. **自定义/创意内容** → 用 Unicode 调色板由 LLM 生成
9. **任何工具未安装** → 安装它，或退到下一个选项
