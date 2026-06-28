# 导出流水线

## PNG 导出

### 草图内（键盘快捷键）

```javascript
function keyPressed() {
  if (key === 's' || key === 'S') {
    saveCanvas('output', 'png');
    // 立即下载 output.png
  }
}
```

### 定时导出（静态生成）

```javascript
function setup() {
  createCanvas(3840, 2160);
  pixelDensity(1);
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
  noLoop();
}

function draw() {
  // ... 渲染一切 ...
  saveCanvas('output-seed-' + CONFIG.seed, 'png');
}
```

### 高分辨率导出

要超过屏幕尺寸的分辨率，使用 `pixelDensity()` 或一个大的离屏缓冲：

```javascript
function exportHighRes(scale) {
  let buffer = createGraphics(width * scale, height * scale);
  buffer.scale(scale);
  // 以更高分辨率把一切重新渲染到缓冲
  renderScene(buffer);
  buffer.save('highres-output.png');
}
```

### 批量种子导出

```javascript
function exportBatch(startSeed, count) {
  for (let i = 0; i < count; i++) {
    CONFIG.seed = startSeed + i;
    randomSeed(CONFIG.seed);
    noiseSeed(CONFIG.seed);
    // 渲染
    background(0);
    renderScene();
    saveCanvas('seed-' + nf(CONFIG.seed, 5), 'png');
  }
}
```

## GIF 导出

### saveGif()

```javascript
function keyPressed() {
  if (key === 'g' || key === 'G') {
    saveGif('output', 5);
    // 捕获 5 秒动画
    // 选项：saveGif(filename, duration, options)
  }
}

// 带选项
saveGif('output', 5, {
  delay: 0,        // 开始捕获前的延迟（秒）
  units: 'seconds' // 或 'frames'
});
```

限制：
- GIF 最多 256 色——渐变上会有抖动伪影
- 大画布会产生巨大文件
- GIF 用较小画布（640x360），PNG/MP4 用更高分辨率
- 帧率是近似的

### 最佳 GIF 设置

```javascript
// GIF 输出时用更小的画布和更低的帧率
function setup() {
  createCanvas(640, 360);
  frameRate(15);  // GIF 标准
  pixelDensity(1);
}
```

## 帧序列导出

### saveFrames()

```javascript
function keyPressed() {
  if (key === 'f') {
    saveFrames('frame', 'png', 10, 30);
    // 10 秒，30 fps → 300 个 PNG 文件
    // 作为独立文件下载（浏览器可能阻止批量下载）
  }
}
```

### 手动帧导出（更可控）

```javascript
let recording = false;
let frameNum = 0;
const TOTAL_FRAMES = 300;

function keyPressed() {
  if (key === 'r') recording = !recording;
}

function draw() {
  // ... 渲染帧 ...

  if (recording) {
    saveCanvas('frame-' + nf(frameNum, 4), 'png');
    frameNum++;
    if (frameNum >= TOTAL_FRAMES) {
      recording = false;
      noLoop();
      console.log('Recording complete: ' + frameNum + ' frames');
    }
  }
}
```

### 确定性捕获（视频的关键）

`noLoop()` + `redraw()` 模式是帧精确无头捕获**必需**的。否则，p5 的绘制循环在 Chrome 中自由运行，而 Puppeteer 截图很慢——草图跑在前面，导致重复/丢帧。

```javascript
function setup() {
  createCanvas(1920, 1080);
  pixelDensity(1);
  noLoop();                    // 停止自动绘制循环
  window._p5Ready = true;      // 向捕获脚本发信号
}

function draw() {
  // 这只在捕获脚本调用 redraw() 时运行
  // 每次 redraw() 时 frameCount 恰好递增一次
}
```

附带的 `scripts/export-frames.js` 会检测 `window._p5Ready`，并自动切换到确定性模式。若没有它，则回退到定时捕获（不够精确）。

### ffmpeg：帧转 MP4

```bash
# 基础编码
ffmpeg -framerate 30 -i frame-%04d.png -c:v libx264 -pix_fmt yuv420p output.mp4

# 高质量
ffmpeg -framerate 30 -i frame-%04d.png \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
  output.mp4

# 带音频
ffmpeg -framerate 30 -i frame-%04d.png -i audio.mp3 \
  -c:v libx264 -c:a aac -shortest \
  output.mp4

# 社交媒体循环（循环 3 次）
ffmpeg -stream_loop 2 -i output.mp4 -c copy output-looped.mp4
```

### 视频导出陷阱

**YUV420 会吃掉暗部数值。** H.264 在 YUV420 色彩空间中编码，会对暗的 RGB 数值取整。低于 RGB(8,8,8) 的内容可能变成纯黑。细微的暗部细节（暗淡的粒子拖尾、微弱的噪声纹理）在编码视频中会消失，尽管它们在 PNG 帧里清晰可见。

**修复：** 确保任何可见内容的最小亮度约为 10。编码几帧后对比 MP4 帧与源 PNG 来测试。

```bash
# 从 MP4 提取一帧用于对比
ffmpeg -i output.mp4 -vf "select=eq(n\,100)" -vframes 1 check.png
```

**静态帧在视频中看起来像出错。** 如果算法只产出单一静态图像（如预计算的吸引子热图），在视频中会读起来像卡顿/故障。即使是静态内容也要加动画：
- 渐进揭示（从中心扩展、横扫）
- 缓慢参数漂移（旋转颜色映射、偏移噪声）
- 类相机运动（缓慢缩放、轻微平移）
- 叠加动画粒子或颗粒

**场景转场是必须的。** 视觉差异大的场景之间硬切令人不适。用淡入淡出包络：

```javascript
const FADE_FRAMES = 15;  // 30fps 下半秒
let fade = 1;
if (localFrame < FADE_FRAMES) fade = localFrame / FADE_FRAMES;
if (localFrame > SCENE_FRAMES - FADE_FRAMES) fade = (SCENE_FRAMES - localFrame) / FADE_FRAMES;
fade = fade * fade * (3 - 2 * fade);  // smoothstep
// 应用：把所有 alpha/亮度乘以 fade
```

### 逐片段架构（多场景视频）

对于多场景视频，把每个场景渲染为独立的 HTML 文件 + MP4 片段，再用 ffmpeg 拼接。这样可以单独重渲染某个场景而不动其余部分。

**目录结构：**
```
project/
├── capture-scene.js          # 共享：node capture-scene.js <html> <outdir> <frames>
├── render-all.sh             # 渲染全部 + 拼接
├── scenes/
│   ├── 00-intro.html         # 每个场景自包含
│   ├── 01-particles.html
│   ├── 02-noise.html
│   └── 03-outro.html
└── clips/
    ├── 00-intro.mp4          # 每个片段独立渲染
    ├── 01-particles.mp4
    ├── 02-noise.mp4
    ├── 03-outro.mp4
    └── concat.txt
```

**用 ffmpeg concat 拼接片段：**
```bash
# concat.txt（顺序决定最终序列）
file '00-intro.mp4'
file '01-particles.mp4'
file '02-noise.mp4'
file '03-outro.mp4'

# 无损拼接（所有片段必须具有相同的编解码器/分辨率/帧率）
ffmpeg -f concat -safe 0 -i concat.txt -c copy final.mp4
```

**重渲染单个场景：**
```bash
node capture-scene.js scenes/01-particles.html clips/01-particles 150
ffmpeg -y -framerate 30 -i clips/01-particles/frame-%04d.png \
  -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p clips/01-particles.mp4
# 然后重新拼接
ffmpeg -y -f concat -safe 0 -i clips/concat.txt -c copy final.mp4
```

**不重渲染即可重排：** 只需更改 concat.txt 中的顺序再重新拼接。无需重渲染任何帧。

**每个场景 HTML 必须：**
- 在 setup 中调用 `noLoop()` 并设置 `window._p5Ready = true`
- 使用基于 `frameCount` 的定时（而非 `millis()`）以保证确定性输出
- 自行处理淡入/淡出包络
- 完全自包含（场景之间无共享状态）

### ffmpeg：帧转 GIF（质量更佳）

```bash
# 先生成调色板以获得最佳颜色
ffmpeg -i frame-%04d.png -vf "fps=15,palettegen=max_colors=256" palette.png

# 用调色板渲染 GIF
ffmpeg -i frame-%04d.png -i palette.png \
  -lavfi "fps=15 [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3" \
  output.gif
```

## 无头导出（Puppeteer）

用于自动化、服务器端或 CI 渲染。使用无头 Chrome 浏览器运行草图。

### export-frames.js（Node.js 脚本）

完整实现见 `scripts/export-frames.js`。基本模式：

```javascript
const puppeteer = require('puppeteer');

async function captureFrames(htmlPath, outputDir, options) {
  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  const page = await browser.newPage();

  await page.setViewport({
    width: options.width || 1920,
    height: options.height || 1080,
    deviceScaleFactor: 1
  });

  await page.goto(`file://${path.resolve(htmlPath)}`, {
    waitUntil: 'networkidle0'
  });

  // 等待草图初始化
  await page.waitForSelector('canvas');
  await page.waitForTimeout(1000);

  for (let i = 0; i < options.frames; i++) {
    const canvas = await page.$('canvas');
    await canvas.screenshot({
      path: path.join(outputDir, `frame-${String(i).padStart(4, '0')}.png`)
    });

    // 推进一帧
    await page.evaluate(() => { redraw(); });
    await page.waitForTimeout(1000 / options.fps);
  }

  await browser.close();
}
```

### render.sh（完整流水线）

完整渲染脚本见 `scripts/render.sh`。流水线：

```
1. 启动 Puppeteer → 打开草图 HTML
2. 把 N 帧捕获为 PNG 序列
3. 通过管道送给 ffmpeg → 编码 H.264 MP4
4. 可选：添加音轨
5. 清理临时帧
```

## SVG 导出

### 使用 p5.js-svg 库

```html
<script src="https://unpkg.com/p5.js-svg@1.5.1"></script>
```

```javascript
function setup() {
  createCanvas(1920, 1080, SVG);  // SVG 渲染器
  noLoop();
}

function draw() {
  // 只能用矢量操作（无像素、无混合模式）
  stroke(0);
  noFill();
  for (let i = 0; i < 100; i++) {
    let x = random(width);
    let y = random(height);
    ellipse(x, y, random(10, 50));
  }
  save('output.svg');
}
```

限制：
- 无 `loadPixels()`、`updatePixels()`、`filter()`、`blendMode()`
- 无 WebGL
- 无像素级特效
- 适合：线条艺术、几何图案、绘图仪输出

### 混合：光栅背景 + SVG 叠加

把背景特效渲染为 PNG，再用 SVG 叠加清晰的矢量元素。

## 导出格式决策指南

| 需求 | 格式 | 方法 |
|------|--------|--------|
| 单张静态图 | PNG | `saveCanvas()` 或 `keyPressed()` |
| 印刷级静态图 | PNG（高分辨率） | `pixelDensity(1)` + 大画布 |
| 短动画循环 | GIF | `saveGif()` |
| 长动画 | MP4 | 帧序列 + ffmpeg |
| 社交媒体视频 | MP4 | `scripts/render.sh` |
| 矢量/印刷 | SVG | p5.js-svg 渲染器 |
| 批量变体 | PNG 序列 | 种子循环 + `saveCanvas()` |
| 交互式部署 | HTML | 单个自包含文件 |
| 无头渲染 | PNG/MP4 | Puppeteer + ffmpeg |

## 超高分辨率的分块

对于单画布无法承载的分辨率（例如印刷用的 10000x10000）：

```javascript
function renderTiled(totalW, totalH, tileSize) {
  let cols = ceil(totalW / tileSize);
  let rows = ceil(totalH / tileSize);

  for (let ty = 0; ty < rows; ty++) {
    for (let tx = 0; tx < cols; tx++) {
      let buffer = createGraphics(tileSize, tileSize);
      buffer.push();
      buffer.translate(-tx * tileSize, -ty * tileSize);
      renderScene(buffer, totalW, totalH);
      buffer.pop();
      buffer.save(`tile-${tx}-${ty}.png`);
      buffer.remove();  // 释放内存
    }
  }
  // 用 ImageMagick 拼接：
  // montage tile-*.png -tile 4x4 -geometry +0+0 final.png
}
```

## CCapture.js — 确定性视频捕获

内置的 `saveFrames()` 有局限：帧数少、内存问题、浏览器下载阻止。CCapture.js 通过挂钩浏览器的定时函数来模拟恒定时间步长（无论实际渲染速度如何），从而解决这一切。

```html
<script src="https://cdn.jsdelivr.net/npm/ccapture.js-npmfixed/build/CCapture.all.min.js"></script>
```

### 基础设置

```javascript
let capturer;
let recording = false;

function setup() {
  createCanvas(1920, 1080);
  pixelDensity(1);

  capturer = new CCapture({
    format: 'webm',       // 'webm'、'gif'、'png'、'jpg'
    framerate: 30,
    quality: 99,           // webm/jpg 为 0-100
    // timeLimit: 10,      // N 秒后自动停止
    // motionBlurFrames: 4 // 超采样的运动模糊
  });
}

function draw() {
  // ... 渲染帧 ...

  if (recording) {
    capturer.capture(document.querySelector('canvas'));
  }
}

function keyPressed() {
  if (key === 'c') {
    if (!recording) {
      capturer.start();
      recording = true;
      console.log('Recording started');
    } else {
      capturer.stop();
      capturer.save();  // 触发下载
      recording = false;
      console.log('Recording saved');
    }
  }
}
```

### 格式对比

| 格式 | 质量 | 大小 | 浏览器支持 |
|--------|---------|------|-----------------|
| **WebM** | 高 | 中 | 仅 Chrome |
| **GIF** | 256 色 | 大 | 全部（通过 gif.js worker） |
| **PNG 序列** | 无损 | 很大（TAR） | 全部 |
| **JPEG 序列** | 有损 | 大（TAR） | 全部 |

### 重要：定时钩子

CCapture.js 会覆盖 `Date.now()`、`setTimeout`、`requestAnimationFrame` 和 `performance.now()`。这意味着：
- `millis()` 返回模拟时间（录制时完美）
- `deltaTime` 是常量（1000/framerate）
- 即使每帧耗时 500 毫秒的复杂草图也能以平滑的 30fps 录制
- **注意**：音频同步会失效（音频按真实时间播放，而非模拟时间）

## 程序化导出（canvas API）

用于超越 `saveCanvas()` 的自定义导出工作流：

```javascript
// Canvas 转 Blob（用于上传、处理）
document.querySelector('canvas').toBlob((blob) => {
  // 上传到服务器、处理等
  let url = URL.createObjectURL(blob);
  console.log('Blob URL:', url);
}, 'image/png');

// Canvas 转 Data URL（用于内联嵌入）
let dataUrl = document.querySelector('canvas').toDataURL('image/png');
// 用在 <img src="..."> 中或作为 base64 发送
```

## SVG 导出（p5.js-svg）

```html
<script src="https://unpkg.com/p5.js-svg@1.6.0"></script>
```

```javascript
function setup() {
  createCanvas(1920, 1080, SVG);  // SVG 渲染器
  noLoop();
}

function draw() {
  // 只有矢量操作可用（无像素操作、无 blendMode）
  stroke(0);
  noFill();
  for (let i = 0; i < 100; i++) {
    ellipse(random(width), random(height), random(10, 50));
  }
  save('output.svg');
}
```

**关键 SVG 注意事项：**
- **动画草图必须在 `draw()` 中调用 `clear()`**——SVG DOM 会累积子元素，导致内存膨胀
- SVG 渲染器**未实现** `blendMode()`
- `filter()`、`loadPixels()`、`updatePixels()` 不可用
- 需要 **p5.js 1.11.x**——与 p5.js 2.x 不兼容
- 完美适合：线条艺术、几何图案、绘图仪输出

## 平台导出

### fxhash 约定

```javascript
// 用 fxhash 的确定性 PRNG 替换 p5 的 random
const rng = $fx.rand;

// 声明特性，用于稀有度/筛选
$fx.features({
  'Palette': paletteName,
  'Complexity': complexity > 0.7 ? 'High' : 'Low',
  'Has Particles': particleCount > 0
});

// 声明链上参数
$fx.params([
  { id: 'density', name: 'Density', type: 'number',
    options: { min: 1, max: 100, step: 1 } },
  { id: 'palette', name: 'Palette', type: 'select',
    options: { options: ['Warm', 'Cool', 'Mono'] } },
  { id: 'accent', name: 'Accent Color', type: 'color' }
]);

// 读取参数
let density = $fx.getParam('density');

// 构建：npx fxhash build → upload.zip
// 开发：npx fxhash dev → localhost:3300
```

### Art Blocks / 通用平台

```javascript
// 平台提供一个哈希字符串
const hash = tokenData.hash;  // Art Blocks 约定

// 从哈希构建确定性 PRNG
function prngFromHash(hash) {
  let seed = parseInt(hash.slice(0, 16), 16);
  // xoshiro128** 或类似算法
  return function() { /* ... */ };
}

const rng = prngFromHash(hash);
```
