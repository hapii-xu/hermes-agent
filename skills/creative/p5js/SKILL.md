---
name: p5js
description: "p5.js 草图：生成艺术、着色器、交互式、3D。"
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative-coding, generative-art, p5js, canvas, interactive, visualization, webgl, shaders, animation]
    related_skills: [ascii-video, manim-video, excalidraw]
---

# p5.js 生产流水线

## 何时使用

当用户请求以下内容时使用：p5.js 草图、创意编程、生成艺术、交互式可视化、画布动画、基于浏览器的视觉艺术、数据可视化、着色器特效，或任何 p5.js 项目。

## 内容概览

使用 p5.js 的交互式与生成式视觉艺术生产流水线。创作基于浏览器的草图、生成艺术、数据可视化、交互体验、3D 场景、音频反应视觉以及动态图形——导出为 HTML、PNG、GIF、MP4 或 SVG。涵盖：2D/3D 渲染、噪声与粒子系统、流场、着色器（GLSL）、像素操作、动态字体、WebGL 场景、音频分析、鼠标/键盘交互，以及无头高分辨率导出。

## 创作标准

这是在浏览器中渲染的视觉艺术。画布是媒介；算法是画笔。

**在写下任何一行代码之前**，先阐明创作理念。这件作品要传达什么？是什么让观看者停下滚动的手指？是什么把它和代码教程示例区分开来？用户的提示只是起点——用创作野心去诠释它。

**首次渲染的卓越品质不可妥协。** 输出在首次加载时就必须视觉惊艳。如果它看起来像一个 p5.js 教程练习、一个默认配置，或是"AI 生成的创意编程作业"，那就是错的。发布前重新构思。

**超越参考词汇库。** 参考资料中的噪声函数、粒子系统、调色板和着色器特效只是一个起点词汇表。对每个项目，都要组合、分层、再创造。这份目录是一盒颜料——而你来作画。

**主动发挥创造力。** 如果用户要"一个粒子系统"，就交付一个具备涌现式群聚行为、带拖尾幽灵回声、调色板偏移的深度雾，以及会呼吸的背景噪声场的粒子系统。至少包含一个用户没要求但会欣赏的视觉细节。

**密集、分层、经过深思熟虑。** 每一帧都应值得观看。永远不要纯白背景。始终要有构图层次。始终要有刻意的配色。始终要有只在近距离审视时才会显现的微观细节。

**统一的美学胜过功能数量。** 所有元素都必须服务于统一的视觉语言——共享的色温、一致的描边粗细体系、和谐的运动速度。一个塞了十个互不相关特效的草图，不如一个只有三个彼此契合特效的草图。

## 模式

| 模式 | 输入 | 输出 | 参考 |
|------|-------|--------|-----------|
| **生成艺术** | 种子 / 参数 | 程序化视觉构图（静态或动画） | `references/visual-effects.md` |
| **数据可视化** | 数据集 / API | 交互式图表、图形、自定义数据展示 | `references/interaction.md` |
| **交互体验** | 无（由用户驱动） | 鼠标/键盘/触控驱动的草图 | `references/interaction.md` |
| **动画 / 动态图形** | 时间轴 / 分镜 | 定时序列、动态字体、转场 | `references/animation.md` |
| **3D 场景** | 概念描述 | WebGL 几何体、光照、相机、材质 | `references/webgl-and-3d.md` |
| **图像处理** | 图像文件 | 像素操作、滤镜、马赛克、点彩派 | `references/visual-effects.md` § 像素操作 |
| **音频反应** | 音频文件 / 麦克风 | 声音驱动的生成视觉 | `references/interaction.md` § 音频输入 |

## 技术栈

每个项目使用单个自包含的 HTML 文件。无需构建步骤。

| 层 | 工具 | 用途 |
|-------|------|---------|
| 核心 | p5.js 1.11.3 (CDN) | 画布渲染、数学、变换、事件处理 |
| 3D | p5.js WebGL 模式 | 3D 几何体、相机、光照、GLSL 着色器 |
| 音频 | p5.sound.js (CDN) | FFT 分析、振幅、麦克风输入、振荡器 |
| 导出 | 内置 `saveCanvas()` / `saveGif()` / `saveFrames()` | PNG、GIF、帧序列输出 |
| 捕获 | CCapture.js（可选） | 确定性帧率视频捕获（WebM、GIF） |
| 无头 | Puppeteer + Node.js（可选） | 自动化高分辨率渲染、通过 ffmpeg 输出 MP4 |
| SVG | p5.js-svg 1.6.0（可选） | 用于印刷的矢量输出——需要 p5.js 1.x |
| 自然媒介 | p5.brush（可选） | 水彩、炭笔、钢笔——需要 p5.js 2.x + WEBGL |
| 纹理 | p5.grain（可选） | 胶片颗粒、纹理叠加 |
| 字体 | Google Fonts / `loadFont()` | 通过 OTF/TTF/WOFF2 的自定义字体 |

### 版本说明

**p5.js 1.x**（1.11.3）是默认版本——稳定、文档完善、库兼容性最广。除非项目需要 2.x 特性，否则一律使用此版本。

**p5.js 2.x**（2.2+）新增：替代 `preload()` 的 `async setup()`、OKLCH/OKLAB 色彩模式、`splineVertex()`、着色器 `.modify()` API、可变字体、`textToContours()`、指针事件。p5.brush 必须使用此版本。参见 `references/core-api.md` § p5.js 2.0。

## 流水线

每个项目都遵循同样的 6 阶段路径：

```
CONCEPT → DESIGN → CODE → PREVIEW → EXPORT → VERIFY
```

1. **CONCEPT（构思）** — 阐明创作愿景：氛围、色彩世界、运动词汇、独特之处
2. **DESIGN（设计）** — 选择模式、画布尺寸、交互模型、色彩系统、导出格式。把概念映射为技术决策
3. **CODE（编码）** — 编写带内联 p5.js 的单个 HTML 文件。结构：全局变量 → `preload()` → `setup()` → `draw()` → 辅助函数 → 类 → 事件处理
4. **PREVIEW（预览）** — 在浏览器中打开，验证视觉质量。在目标分辨率下测试。检查性能
5. **EXPORT（导出）** — 捕获输出：`saveCanvas()` 导 PNG、`saveGif()` 导 GIF、`saveFrames()` + ffmpeg 导 MP4、Puppeteer 做无头批量
6. **VERIFY（验证）** — 输出是否符合概念？在目标显示尺寸下是否视觉惊艳？你愿意把它装裱起来吗？

## 创作方向

### 美学维度

| 维度 | 选项 | 参考 |
|-----------|---------|-----------|
| **色彩系统** | HSB/HSL、RGB、命名调色板、程序化和声、渐变插值 | `references/color-systems.md` |
| **噪声词汇** | 柏林噪声、单纯形噪声、分形（多倍频）、域畸变、旋度噪声 | `references/visual-effects.md` § 噪声 |
| **粒子系统** | 基于物理、群聚、绘拖尾、吸引子驱动、跟随流场 | `references/visual-effects.md` § 粒子 |
| **形状语言** | 几何图元、自定义顶点、贝塞尔曲线、SVG 路径 | `references/shapes-and-geometry.md` |
| **运动风格** | 缓动、弹簧、噪声驱动、物理仿真、线性插值、步进 | `references/animation.md` |
| **字体排版** | 系统字体、加载的 OTF、`textToPoints()` 粒子文字、动态字体 | `references/typography.md` |
| **着色器特效** | GLSL 片段/顶点着色器、滤镜着色器、后期处理、反馈回路 | `references/webgl-and-3d.md` § 着色器 |
| **构图** | 网格、放射状、黄金比例、三分法、有机散布、平铺 | `references/core-api.md` § 构图 |
| **交互模型** | 鼠标跟随、点击生成、拖拽、键盘状态、滚动驱动、麦克风输入 | `references/interaction.md` |
| **混合模式** | `BLEND`、`ADD`、`MULTIPLY`、`SCREEN`、`DIFFERENCE`、`EXCLUSION`、`OVERLAY` | `references/color-systems.md` § 混合模式 |
| **分层** | `createGraphics()` 离屏缓冲、Alpha 合成、遮罩 | `references/core-api.md` § 离屏缓冲 |
| **纹理** | 柏林曲面、点画、排线、半色调、像素排序 | `references/visual-effects.md` § 纹理生成 |

### 每个项目的变化规则

永远不要使用默认配置。每个项目都要：
- **自定义调色板** — 永远不要裸用 `fill(255, 0, 0)`。始终使用 3-7 种颜色的设计调色板
- **自定义描边粗细体系** — 细装饰（0.5）、中等结构（1-2）、粗强调（3-5）
- **背景处理** — 永远不要纯 `background(0)` 或 `background(255)`。始终带纹理、渐变或分层
- **运动多样性** — 不同元素用不同速度。主体 1x，次要 0.3x，氛围 0.1x
- **至少一个原创元素** — 自定义粒子行为、新颖的噪声应用、独特的交互响应

### 项目专属原创

每个项目至少原创以下之一：
- 匹配氛围的自定义调色板（非预设）
- 新颖的噪声场组合（例如：旋度噪声 + 域畸变 + 反馈）
- 独特的粒子行为（自定义受力、自定义拖尾、自定义生成）
- 用户未要求但能提升作品的交互机制
- 创造视觉层次的构图技法

### 参数设计哲学

参数应从算法中自然涌现，而非来自通用菜单。要问："*这个*系统的哪些属性应该可调？"

**好的参数**揭示算法的个性：
- **数量** — 多少粒子、多少分支、多少单元（控制密度）
- **尺度** — 噪声频率、元素大小、间距（控制纹理）
- **速率** — 速度、生长率、衰减（控制能量）
- **阈值** — 行为何时改变？（控制戏剧性）
- **比例** — 各部分比例、力之间的平衡（控制和声）

**差的参数**是与算法无关的通用控件：
- "color1"、"color2"、"size" — 脱离上下文毫无意义
- 用于无关特效的开关
- 只改变外观、不改变行为的参数

每个参数都应改变算法*思考*的方式，而不只是*看起来*的样子。一个能改变噪声倍频数的"湍流"参数是好的。一个只改 `ellipse()` 半径的"粒子大小"滑块是肤浅的。

## 工作流

### 第 1 步：创作愿景

在任何代码之前，先阐明：
- **氛围 / 情绪**：观看者应感受到什么？沉思？振奋？不安？俏皮？
- **视觉故事**：随时间（或交互）发生什么？生长？衰减？变形？振荡？
- **色彩世界**：暖/冷？单色？互补？主色调是什么？强调色是什么？
- **形状语言**：有机曲线？锐利几何？点？线？混合？
- **运动词汇**：缓慢漂移？爆发式迸发？呼吸式脉动？机械精准？
- **这件作品的独特之处**：让这个草图独一无二的那一点是什么？

把用户的提示映射到美学选择。"令人放松的生成式背景"和"故障数据可视化"对一切都要求不同处理。

### 第 2 步：技术设计

- **模式** — 上表 7 种模式中的哪一种
- **画布尺寸** — 横向 1920x1080、纵向 1080x1920、方形 1080x1080，或响应式 `windowWidth/windowHeight`
- **渲染器** — `P2D`（默认）或 `WEBGL`（用于 3D、着色器、高级混合模式）
- **帧率** — 60fps（交互式）、30fps（氛围动画）或 `noLoop()`（静态生成）
- **导出目标** — 浏览器显示、PNG 静态图、GIF 循环、MP4 视频、SVG 矢量
- **交互模型** — 被动（无输入）、鼠标驱动、键盘驱动、音频反应、滚动驱动
- **观看器 UI** — 对于交互式生成艺术，从 `templates/viewer.html` 起步，它提供种子导航、参数滑块和下载功能。对于简单草图或视频导出，使用裸 HTML

### 第 3 步：编写草图

对于**交互式生成艺术**（种子探索、参数调校）：从 `templates/viewer.html` 起步。先读模板，保留固定部分（种子导航、操作），替换算法和参数控件。这样用户就获得了种子的上一个/下一个/随机/跳转、带实时更新的参数滑块，以及 PNG 下载——全部接好线。

对于**动画、视频导出或简单草图**：使用裸 HTML：

单个 HTML 文件。结构：

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Project Name</title>
  <script>p5.disableFriendlyErrors = true;</script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js"></script>
  <!-- <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/addons/p5.sound.min.js"></script> -->
  <!-- <script src="https://unpkg.com/p5.js-svg@1.6.0"></script> -->  <!-- SVG 导出 -->
  <!-- <script src="https://cdn.jsdelivr.net/npm/ccapture.js-npmfixed/build/CCapture.all.min.js"></script> -->  <!-- 视频捕获 -->
  <style>
    html, body { margin: 0; padding: 0; overflow: hidden; }
    canvas { display: block; }
  </style>
</head>
<body>
<script>
// === 配置 ===
const CONFIG = {
  seed: 42,
  // ... 项目专属参数
};

// === 调色板 ===
const PALETTE = {
  bg: '#0a0a0f',
  primary: '#e8d5b7',
  // ...
};

// === 全局状态 ===
let particles = [];

// === 预加载（字体、图像、数据）===
function preload() {
  // font = loadFont('...');
}

// === 初始化 ===
function setup() {
  createCanvas(1920, 1080);
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
  colorMode(HSB, 360, 100, 100, 100);
  // 初始化状态...
}

// === 绘制循环 ===
function draw() {
  // 渲染帧...
}

// === 辅助函数 ===
// ...

// === 类 ===
class Particle {
  // ...
}

// === 事件处理 ===
function mousePressed() { /* ... */ }
function keyPressed() { /* ... */ }
function windowResized() { resizeCanvas(windowWidth, windowHeight); }
</script>
</body>
</html>
```

关键实现模式：
- **带种子的随机性**：始终用 `randomSeed()` + `noiseSeed()` 保证可复现
- **色彩模式**：用 `colorMode(HSB, 360, 100, 100, 100)` 获得直观的颜色控制
- **状态分离**：CONFIG 用于参数，PALETTE 用于颜色，全局变量用于可变状态
- **基于类的实体**：粒子、智能体、形状都做成带 `update()` + `display()` 方法的类
- **离屏缓冲**：`createGraphics()` 用于分层合成、拖尾、遮罩

### 第 4 步：预览与迭代

- 直接在浏览器中打开 HTML 文件——基础草图无需服务器
- 若从本地文件 `loadImage()`/`loadFont()`：使用 `scripts/serve.sh` 或 `python3 -m http.server`
- 用 Chrome DevTools 的 Performance 面板验证 60fps
- 在目标导出分辨率下测试，而不只是窗口尺寸
- 调整参数直到视觉效果匹配第 1 步的概念

### 第 5 步：导出

| 格式 | 方法 | 命令 |
|--------|--------|---------|
| **PNG** | 在 `keyPressed()` 中调用 `saveCanvas('output', 'png')` | 按 's' 保存 |
| **高分辨率 PNG** | Puppeteer 无头捕获 | `node scripts/export-frames.js sketch.html --width 3840 --height 2160 --frames 1` |
| **GIF** | `saveGif('output', 5)` — 捕获 N 秒 | 按 'g' 保存 |
| **帧序列** | `saveFrames('frame', 'png', 10, 30)` — 10 秒、30fps | 然后 `ffmpeg -i frame-%04d.png -c:v libx264 output.mp4` |
| **MP4** | Puppeteer 帧捕获 + ffmpeg | `bash scripts/render.sh sketch.html output.mp4 --duration 30 --fps 30` |
| **SVG** | 配合 p5.js-svg 的 `createCanvas(w, h, SVG)` | `save('output.svg')` |

### 第 6 步：质量验证

- **是否符合愿景？** 把输出和创作概念对比。如果显得平庸，回到第 1 步
- **分辨率检查**：在目标显示尺寸下是否锐利？有无锯齿伪影？
- **性能检查**：浏览器中能否保持 60fps？（动画至少 30fps）
- **配色检查**：颜色搭配是否协调？在亮屏和暗屏显示器上都测试一下
- **边界情况**：画布边缘发生什么？调整尺寸时？运行 10 分钟后？

## 关键实现说明

### 性能 — 先禁用 FES

友好错误系统（FES）会增加高达 10 倍的开销。每个生产草图都要禁用它：

```javascript
p5.disableFriendlyErrors = true;  // 必须在 setup() 之前

function setup() {
  pixelDensity(1);  // 防止视网膜屏上 2x-4x 的过度绘制
  createCanvas(1920, 1080);
}
```

在热点循环（粒子、像素操作）中，使用 `Math.*` 而非 p5 包装函数——可测得更快：

```javascript
// 在 draw() 或 update() 热点路径中：
let a = Math.sin(t);          // 而非 sin(t)
let r = Math.sqrt(dx*dx+dy*dy); // 而非 dist()——或者更好：跳过 sqrt，比较 magSq
let v = Math.random();        // 而非 random()——当不需要种子时
let m = Math.min(a, b);       // 而非 min(a, b)
```

永远不要在 `draw()` 里 `console.log()`。永远不要在 `draw()` 里操作 DOM。参见 `references/troubleshooting.md` § 性能。

### 带种子的随机性 — 必须做到

每个生成式草图都必须可复现。同样的种子，同样的输出。

```javascript
function setup() {
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
  // 此后所有 random() 与 noise() 调用都是确定性的
}
```

永远不要把 `Math.random()` 用于生成式内容——只用于性能关键的非视觉代码。视觉元素一律用 `random()`。如果需要随机种子：`CONFIG.seed = floor(random(99999))`。

### 生成艺术平台支持（fxhash / Art Blocks）

对于生成艺术平台，用平台的确定性随机数替换 p5 的 PRNG：

```javascript
// fxhash 约定
const SEED = $fx.hash;              // 每次铸造唯一
const rng = $fx.rand;               // 确定性 PRNG
$fx.features({ palette: 'warm', complexity: 'high' });

// 在 setup() 中：
randomSeed(SEED);   // 用于 p5 的 noise()
noiseSeed(SEED);

// 用 rng() 替换 random() 以实现平台确定性
let x = rng() * width;  // 而非 random(width)
```

参见 `references/export-pipeline.md` § 平台导出。

### 色彩模式 — 使用 HSB

HSB（色相、饱和度、亮度）在生成艺术中比 RGB 容易操作得多：

```javascript
colorMode(HSB, 360, 100, 100, 100);
// 此后：fill(hue, sat, bri, alpha)
// 旋转色相：fill((baseHue + offset) % 360, 80, 90)
// 降低饱和度：fill(hue, sat * 0.3, bri)
// 变暗：fill(hue, sat, bri * 0.5)
```

永远不要硬编码原始 RGB 值。定义一个调色板对象，程序化地派生变体。参见 `references/color-systems.md`。

### 噪声 — 多倍频，而非裸用

裸用的 `noise(x, y)` 看起来像光滑的色块。叠加倍频才能得到自然纹理：

```javascript
function fbm(x, y, octaves = 4) {
  let val = 0, amp = 1, freq = 1, sum = 0;
  for (let i = 0; i < octaves; i++) {
    val += noise(x * freq, y * freq) * amp;
    sum += amp;
    amp *= 0.5;
    freq *= 2;
  }
  return val / sum;
}
```

对于流动的有机形态，使用**域畸变**：把噪声输出回喂为噪声输入坐标。参见 `references/visual-effects.md`。

### createGraphics() 做分层 — 不是可选项

单遍扁平渲染看起来很平。用离屏缓冲做合成：

```javascript
let bgLayer, fgLayer, trailLayer;
function setup() {
  createCanvas(1920, 1080);
  bgLayer = createGraphics(width, height);
  fgLayer = createGraphics(width, height);
  trailLayer = createGraphics(width, height);
}
function draw() {
  renderBackground(bgLayer);
  renderTrails(trailLayer);   // 持久，渐隐
  renderForeground(fgLayer);  // 每帧清空
  image(bgLayer, 0, 0);
  image(trailLayer, 0, 0);
  image(fgLayer, 0, 0);
}
```

### 性能 — 尽可能向量化

p5.js 绘制调用代价高昂。对于数千个粒子：

```javascript
// 慢：独立形状
for (let p of particles) {
  ellipse(p.x, p.y, p.size);
}

// 快：用 beginShape() 的单一形状
beginShape(POINTS);
for (let p of particles) {
  vertex(p.x, p.y);
}
endShape();

// 最快：海量数量时用像素缓冲
loadPixels();
for (let p of particles) {
  let idx = 4 * (floor(p.y) * width + floor(p.x));
  pixels[idx] = r; pixels[idx+1] = g; pixels[idx+2] = b; pixels[idx+3] = 255;
}
updatePixels();
```

参见 `references/troubleshooting.md` § 性能。

### 多草图的实例模式

全局模式会污染 `window`。生产环境请用实例模式：

```javascript
const sketch = (p) => {
  p.setup = function() {
    p.createCanvas(800, 800);
  };
  p.draw = function() {
    p.background(0);
    p.ellipse(p.mouseX, p.mouseY, 50);
  };
};
new p5(sketch, 'canvas-container');
```

在一个页面嵌入多个草图或与框架集成时必须使用。

### WebGL 模式陷阱

- `createCanvas(w, h, WEBGL)` — 原点在中心，不是左上角
- Y 轴方向相反（WEBGL 中正 Y 向上，P2D 中向下）
- 用 `translate(-width/2, -height/2)` 获得类似 P2D 的坐标
- 每个变换周围都用 `push()`/`pop()`——矩阵栈会静默溢出
- `texture()` 要在 `rect()`/`plane()` 之前调用——不是之后
- 自定义着色器：`createShader(vert, frag)` — 要在多个浏览器测试

### 导出 — 快捷键约定

每个草图都应在 `keyPressed()` 中包含这些：

```javascript
function keyPressed() {
  if (key === 's' || key === 'S') saveCanvas('output', 'png');
  if (key === 'g' || key === 'G') saveGif('output', 5);
  if (key === 'r' || key === 'R') { randomSeed(millis()); noiseSeed(millis()); }
  if (key === ' ') CONFIG.paused = !CONFIG.paused;
}
```

### 无头视频导出 — 使用 noLoop()

通过 Puppeteer 做无头渲染时，草图**必须**在 setup 中使用 `noLoop()`。否则，p5 的绘制循环会自由运行，而截图很慢——草图跑在前面，导致丢帧/重复帧。

```javascript
function setup() {
  createCanvas(1920, 1080);
  pixelDensity(1);
  noLoop();                    // 由捕获脚本控制帧推进
  window._p5Ready = true;      // 向捕获脚本发出就绪信号
}
```

附带的 `scripts/export-frames.js` 会检测 `_p5Ready`，并在每次捕获时调用一次 `redraw()`，实现精确的 1:1 帧对应。参见 `references/export-pipeline.md` § 确定性捕获。

对于多场景视频，使用逐片段架构：每个场景一个 HTML，独立渲染，用 `ffmpeg -f concat` 拼接。参见 `references/export-pipeline.md` § 逐片段架构。

### Agent 工作流

构建 p5.js 草图时：

1. **编写 HTML 文件** — 单个自包含文件，所有代码内联
2. **在浏览器中打开** — `open sketch.html`（macOS）或 `xdg-open sketch.html`（Linux）
3. **本地资源**（字体、图像）需要服务器：在项目目录运行 `python3 -m http.server 8080`，然后打开 `http://localhost:8080/sketch.html`
4. **导出 PNG/GIF** — 加上如上所示的 `keyPressed()` 快捷键，告诉用户按哪个键
5. **无头导出** — `node scripts/export-frames.js sketch.html --frames 300` 做自动化帧捕获（草图必须用 `noLoop()` + `_p5Ready`）
6. **MP4 渲染** — `bash scripts/render.sh sketch.html output.mp4 --duration 30`
7. **迭代优化** — 编辑 HTML 文件，用户刷新浏览器即可看到变化
8. **按需加载参考** — 实现过程中用 `skill_view(name="p5js", file_path="references/...")` 按需加载特定参考文件

## 性能目标

| 指标 | 目标 |
|--------|--------|
| 帧率（交互式） | 持续 60fps |
| 帧率（动画导出） | 至少 30fps |
| 粒子数（P2D 形状） | 60fps 下 5,000-10,000 |
| 粒子数（像素缓冲） | 60fps 下 50,000-100,000 |
| 画布分辨率 | 最高 3840x2160（导出）、1920x1080（交互式） |
| 文件大小（HTML） | < 100KB（不含 CDN 库） |
| 加载时间 | 首帧 < 2 秒 |

## 参考资料

| 文件 | 内容 |
|------|----------|
| `references/core-api.md` | 画布设置、坐标系、绘制循环、`push()`/`pop()`、离屏缓冲、构图模式、`pixelDensity()`、响应式设计 |
| `references/shapes-and-geometry.md` | 2D 图元、`beginShape()`/`endShape()`、贝塞尔/Catmull-Rom 曲线、`vertex()` 体系、自定义形状、`p5.Vector`、有符号距离场、SVG 路径转换 |
| `references/visual-effects.md` | 噪声（柏林、分形、域畸变、旋度）、流场、粒子系统（物理、群聚、拖尾）、像素操作、纹理生成（点画、排线、半色调）、反馈回路、反应-扩散 |
| `references/animation.md` | 基于帧的动画、缓动函数、`lerp()`/`map()`、弹簧物理、状态机、时间轴序列、基于 `millis()` 的定时、转场模式 |
| `references/typography.md` | `text()`、`loadFont()`、`textToPoints()`、动态字体、文字遮罩、字体度量、响应式文字尺寸 |
| `references/color-systems.md` | `colorMode()`、HSB/HSL/RGB、`lerpColor()`、`paletteLerp()`、程序化调色板、色彩和声、`blendMode()`、渐变渲染、精选调色板库 |
| `references/webgl-and-3d.md` | WEBGL 渲染器、3D 图元、相机、光照、材质、自定义几何体、GLSL 着色器（`createShader()`、`createFilterShader()`）、帧缓冲、后期处理 |
| `references/interaction.md` | 鼠标事件、键盘状态、触摸输入、DOM 元素、`createSlider()`/`createButton()`、音频输入（p5.sound FFT/振幅）、滚动驱动动画、响应式事件 |
| `references/export-pipeline.md` | `saveCanvas()`、`saveGif()`、`saveFrames()`、确定性无头捕获、ffmpeg 帧转视频、CCapture.js、SVG 导出、逐片段架构、平台导出（fxhash）、视频陷阱 |
| `references/troubleshooting.md` | 性能剖析、逐像素预算、常见错误、浏览器兼容性、WebGL 调试、字体加载问题、像素密度陷阱、内存泄漏、CORS |
| `templates/viewer.html` | 交互式观看器模板：种子导航（上一个/下一个/随机/跳转）、参数滑块、下载 PNG、响应式画布。可探索的生成艺术请从此起步 |

---

## 创作发散（仅在用户请求实验性/创意/独特输出时使用）

如果用户要求创意、实验性、惊喜或非常规的输出，选择最契合的策略，并在生成代码前先推演其步骤。

- **概念融合** — 当用户点名要结合两样东西，或想要混合美学时
- **SCAMPER** — 当用户想在某个已知生成艺术模式上做变奏时
- **距离联想** — 当用户给出单一概念并希望探索时（"做点和时间有关的东西"）

### 概念融合
1. 命名两个截然不同的视觉系统（例如：粒子物理 + 手写字）
2. 映射对应关系（粒子 = 墨滴，力 = 笔压，场 = 字形）
3. 选择性融合——保留那些产生有趣涌现视觉的映射
4. 把融合编码为一个统一系统，而非两个并排的系统

### SCAMPER 变换
取一个已知的生成模式（流场、粒子系统、L 系统、元胞自动机），系统地变换它：
- **替代（Substitute）**：用文字字符替换圆圈，用渐变替换线条
- **组合（Combine）**：合并两个模式（流场 + Voronoi）
- **改造（Adapt）**：把 2D 模式应用到 3D 投影
- **修改（Modify）**：夸张尺度，扭曲坐标空间
- **另作他用（Purpose）**：把物理仿真用于字体排版，把排序算法用于颜色
- **消除（Eliminate）**：去掉网格，去掉颜色，去掉对称
- **反转（Reverse）**：反向运行仿真，反转参数空间

### 距离联想
1. 锚定用户的概念（例如"孤独"）
2. 在三种距离上生成联想：
   - 近（显而易见）：空房间，单个身影，寂静
   - 中（有趣）：一群鱼里游错方向的那一条，一部没有通知的手机，地铁车厢之间的缝隙
   - 远（抽象）：质数，渐近曲线，凌晨三点的颜色
3. 发展中等距离的联想——它们既足够具体可被可视化，又足够出人意料而有趣
