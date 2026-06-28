# 核心 API 参考

## 画布设置

### createCanvas()

```javascript
// 2D（默认渲染器）
createCanvas(1920, 1080);

// WebGL（3D、着色器）
createCanvas(1920, 1080, WEBGL);

// 响应式
createCanvas(windowWidth, windowHeight);
```

### 像素密度

高 DPI 显示器默认以 2 倍渲染。这会让内存占用翻倍、性能减半。

```javascript
// 强制 1x，保证一致的导出和性能
pixelDensity(1);

// 匹配显示器（默认）——视网膜屏上锐利但代价高
pixelDensity(displayDensity());

// 务必在 createCanvas() 之前调用
function setup() {
  pixelDensity(1);        // 先做这个
  createCanvas(1920, 1080); // 再做这个
}
```

导出时，始终 `pixelDensity(1)` 并使用精确的目标分辨率。永远不要依赖设备缩放做最终输出。

### 响应式调整尺寸

```javascript
function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  // 以新尺寸重建离屏缓冲
  bgLayer = createGraphics(width, height);
  // 重新初始化任何依赖尺寸的状态
}
```

## 坐标系

### P2D（默认）
- 原点：左上角 (0, 0)
- X 向右递增
- Y 向下递增
- 角度：默认弧度，用 `angleMode(DEGREES)` 切换

### WEBGL
- 原点：画布中心
- X 向右递增，Y 向**上**递增，Z 朝向观看者
- 在 WEBGL 中获得类似 P2D 的坐标：`translate(-width/2, -height/2)`

## 绘制循环

```javascript
function preload() {
  // 在 setup 之前加载资源——字体、图像、JSON、CSV
  // 阻塞执行，直到所有加载完成
  font = loadFont('font.otf');
  img = loadImage('texture.png');
  data = loadJSON('data.json');
}

function setup() {
  // 只运行一次。创建画布、初始化状态。
  createCanvas(1920, 1080);
  colorMode(HSB, 360, 100, 100, 100);
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
}

function draw() {
  // 每帧运行（默认 60fps）。
  // 在 setup() 中调用 frameRate(30) 可更改。
  // 对静态草图调用 noLoop()（只渲染一次）。
}
```

### 帧控制

```javascript
frameRate(30);           // 设置目标 FPS
noLoop();                // 停止绘制循环（静态作品）
loop();                  // 重启绘制循环
redraw();                // 调用 draw() 一次（手动刷新）
frameCount              // 自开始以来的帧数（整数）
deltaTime               // 距上一帧的毫秒数（浮点）
millis()                // 自草图启动以来的毫秒数
```

## 变换栈

每个变换都是累积的。用 `push()`/`pop()` 来隔离。

```javascript
push();
  translate(width / 2, height / 2);
  rotate(angle);
  scale(1.5);
  // 在变换后的位置绘制
  ellipse(0, 0, 100, 100);
pop();
// 回到原始坐标系
```

### 变换函数

| 函数 | 效果 |
|----------|--------|
| `translate(x, y)` | 移动原点 |
| `rotate(angle)` | 绕原点旋转（弧度） |
| `scale(s)` / `scale(sx, sy)` | 从原点缩放 |
| `shearX(angle)` | 沿 X 轴倾斜 |
| `shearY(angle)` | 沿 Y 轴倾斜 |
| `applyMatrix(a, b, c, d, e, f)` | 任意 2D 仿射变换 |
| `resetMatrix()` | 清除所有变换 |

### 构图模式：绕中心旋转

```javascript
push();
  translate(cx, cy);       // 把原点移到中心
  rotate(angle);           // 绕该中心旋转
  translate(-cx, -cy);     // 把原点移回
  // 用原始坐标绘制，但绕 (cx, cy) 旋转
  rect(cx - 50, cy - 50, 100, 100);
pop();
```

## 离屏缓冲（createGraphics）

离屏缓冲是可以单独绘制并合成的独立画布。适用于：
- **分层合成** — 背景、中景、前景
- **持久拖尾** — 绘到缓冲，用半透明矩形渐隐，永不清空
- **遮罩** — 把遮罩绘到缓冲，用 `image()` 或像素操作应用
- **后期处理** — 把场景渲染到缓冲，应用特效，绘到主画布

```javascript
let layer;

function setup() {
  createCanvas(1920, 1080);
  layer = createGraphics(width, height);
}

function draw() {
  // 绘到离屏缓冲
  layer.background(0, 10);  // 半透明清屏 = 拖尾
  layer.fill(255);
  layer.ellipse(mouseX, mouseY, 20);

  // 合成到主画布
  image(layer, 0, 0);
}
```

### 拖尾效果模式

```javascript
let trailBuffer;

function setup() {
  createCanvas(1920, 1080);
  trailBuffer = createGraphics(width, height);
  trailBuffer.background(0);
}

function draw() {
  // 渐隐上一帧（alpha 越低拖尾越长）
  trailBuffer.noStroke();
  trailBuffer.fill(0, 0, 0, 15);  // RGBA——15/255 alpha
  trailBuffer.rect(0, 0, width, height);

  // 绘制新内容
  trailBuffer.fill(255);
  trailBuffer.ellipse(mouseX, mouseY, 10);

  // 显示
  image(trailBuffer, 0, 0);
}
```

### 多层合成

```javascript
let bgLayer, contentLayer, fxLayer;

function setup() {
  createCanvas(1920, 1080);
  bgLayer = createGraphics(width, height);
  contentLayer = createGraphics(width, height);
  fxLayer = createGraphics(width, height);
}

function draw() {
  // 背景——绘制一次或缓慢演化
  renderBackground(bgLayer);

  // 内容——主要视觉元素
  contentLayer.clear();
  renderContent(contentLayer);

  // 特效——叠加、暗角、颗粒
  fxLayer.clear();
  renderEffects(fxLayer);

  // 用混合模式合成
  image(bgLayer, 0, 0);
  blendMode(ADD);
  image(contentLayer, 0, 0);
  blendMode(MULTIPLY);
  image(fxLayer, 0, 0);
  blendMode(BLEND);  // 重置
}
```

## 构图模式

### 网格布局

```javascript
let cols = 10, rows = 10;
let cellW = width / cols;
let cellH = height / rows;
for (let i = 0; i < cols; i++) {
  for (let j = 0; j < rows; j++) {
    let cx = cellW * (i + 0.5);
    let cy = cellH * (j + 0.5);
    // 在单元尺寸 (cellW, cellH) 内的 (cx, cy) 处绘制元素
  }
}
```

### 放射状布局

```javascript
let n = 12;
for (let i = 0; i < n; i++) {
  let angle = TWO_PI * i / n;
  let r = 300;
  let x = width/2 + cos(angle) * r;
  let y = height/2 + sin(angle) * r;
  // 在 (x, y) 处绘制元素
}
```

### 黄金比例螺旋

```javascript
let phi = (1 + sqrt(5)) / 2;
let n = 500;
for (let i = 0; i < n; i++) {
  let angle = i * TWO_PI / (phi * phi);
  let r = sqrt(i) * 10;
  let x = width/2 + cos(angle) * r;
  let y = height/2 + sin(angle) * r;
  let size = map(i, 0, n, 8, 2);
  ellipse(x, y, size);
}
```

### 留白感知构图

```javascript
const MARGIN = 80;  // 距边缘的像素
const drawW = width - 2 * MARGIN;
const drawH = height - 2 * MARGIN;

// 把归一化 [0,1] 坐标映射到可绘制区域
function mapX(t) { return MARGIN + t * drawW; }
function mapY(t) { return MARGIN + t * drawH; }
```

## 随机数与噪声

### 带种子的随机

```javascript
randomSeed(42);
let x = random(100);        // 对种子 42 始终是同一个值
let y = random(-1, 1);      // 范围
let item = random(myArray);  // 随机元素
```

### 高斯随机

```javascript
let x = randomGaussian(0, 1);  // 均值=0，标准差=1
// 适合自然分布
```

### 柏林噪声

```javascript
noiseSeed(42);
noiseDetail(4, 0.5);  // 4 倍频，0.5 衰减

let v = noise(x * 0.01, y * 0.01);  // 返回 0.0 到 1.0
// 缩放因子（0.01）控制特征尺寸——越小越平滑
```

## 数学工具

| 函数 | 描述 |
|----------|-------------|
| `map(v, lo1, hi1, lo2, hi2)` | 在区间之间重映射值 |
| `constrain(v, lo, hi)` | 钳制到区间 |
| `lerp(a, b, t)` | 线性插值 |
| `norm(v, lo, hi)` | 归一化到 0-1 |
| `dist(x1, y1, x2, y2)` | 欧氏距离 |
| `mag(x, y)` | 向量长度 |
| `abs()`, `ceil()`, `floor()`, `round()` | 标准数学 |
| `sq(n)`, `sqrt(n)`, `pow(b, e)` | 幂运算 |
| `sin()`, `cos()`, `tan()`, `atan2()` | 三角函数（弧度） |
| `degrees(r)`, `radians(d)` | 角度转换 |
| `fract(n)` | 小数部分 |

## p5.js 2.0 的变化

p5.js 2.0（2025 年 4 月发布，当前：2.2）引入了破坏性变更。p5.js 编辑器默认使用 1.x 直至 2026 年 8 月。仅在你需要 2.x 特性时才使用。

### async setup() 取代 preload()

```javascript
// p5.js 1.x
let img;
function preload() { img = loadImage('cat.jpg'); }
function setup() { createCanvas(800, 800); }

// p5.js 2.x
let img;
async function setup() {
  createCanvas(800, 800);
  img = await loadImage('cat.jpg');
}
```

### 新色彩模式

```javascript
colorMode(OKLCH);  // 感知均匀——渐变更佳
// L：0-1（亮度），C：0-0.4（色度），H：0-360（色相）
fill(0.7, 0.15, 200);  // 中等明亮的饱和蓝

colorMode(OKLAB);  // 感知均匀，无色相角
colorMode(HWB);    // 色相-白度-黑度
```

### splineVertex() 取代 curveVertex()

不再需要把首/末控制点加倍：

```javascript
// p5.js 1.x——必须重复首尾
beginShape();
curveVertex(pts[0].x, pts[0].y);  // 加倍
for (let p of pts) curveVertex(p.x, p.y);
curveVertex(pts[pts.length-1].x, pts[pts.length-1].y);  // 加倍
endShape();

// p5.js 2.x——干净
beginShape();
for (let p of pts) splineVertex(p.x, p.y);
endShape();
```

### 着色器 .modify() API

无需编写完整 GLSL 即可修改内置着色器：

```javascript
let myShader = baseMaterialShader().modify({
  vertexDeclarations: 'uniform float uTime;',
  'vec4 getWorldPosition': `(vec4 pos) {
    pos.y += sin(pos.x * 0.1 + uTime) * 20.0;
    return pos;
  }`
});
```

### 可变字体

```javascript
textWeight(700);  // 无需加载多个文件即可动态调节字重
```

### textToContours() 与 textToModel()

```javascript
let contours = font.textToContours('HELLO', 0, 0, 200);
// 返回轮廓数组的数组（闭合路径）

let geo = font.textToModel('HELLO', 0, 0, 200);
// 返回 p5.Geometry，用于 3D 挤出文字
```

### p5.js 2.x 的 CDN

```html
<script src="https://cdn.jsdelivr.net/npm/p5@2/lib/p5.min.js"></script>
```
