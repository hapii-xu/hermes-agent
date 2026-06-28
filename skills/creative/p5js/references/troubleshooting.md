# 故障排除

## 性能

### 第零步 — 禁用 FES

友好错误系统（FES）会带来巨大开销——高达 10 倍减速。每个生产草图都要禁用：

```javascript
// 在任何 p5 代码之前
p5.disableFriendlyErrors = true;

// 或使用 p5.min.js 而非 p5.js——FES 已从压缩构建中剥离
```

### 第一步 — pixelDensity(1)

Retina/HiDPI 显示器默认使用 2 倍或 3 倍密度，让像素数增加 4-9 倍：

```javascript
function setup() {
  pixelDensity(1);        // 强制 1:1——始终先做这个
  createCanvas(1920, 1080);
}
```

### 在热点循环中使用 Math.*

p5 的 `sin()`、`cos()`、`random()`、`min()`、`max()`、`abs()` 是带开销的包装函数。在热点循环（每帧数千次迭代）中，使用原生 `Math.*`：

```javascript
// 慢——p5 包装
for (let p of particles) {
  let a = sin(p.angle);
  let d = dist(p.x, p.y, mx, my);
}

// 快——原生 Math
for (let p of particles) {
  let a = Math.sin(p.angle);
  let dx = p.x - mx, dy = p.y - my;
  let dSq = dx * dx + dy * dy;  // 完全省掉 sqrt
}
```

距离比较时用 `magSq()` 而非 `mag()`——避免昂贵的 `sqrt()`。

### 诊断

打开 Chrome DevTools > Performance 面板 > 在草图运行时录制。

常见瓶颈：
1. **FES 已启用**——每次 p5 函数调用都有 10 倍开销
2. **pixelDensity > 1**——4 倍像素数，4 倍慢
3. **绘制调用过多**——每帧数千次 `ellipse()`、`rect()`
4. **大画布 + 像素操作**——在 4K 画布上 `loadPixels()`/`updatePixels()`
5. **未优化的粒子系统**——检查所有对所有距离（O(n^2)）
6. **内存泄漏**——每帧创建对象却不清理
7. **着色器编译**——在 `draw()` 而非 `setup()` 中调用 `createShader()`
8. **draw() 中的 console.log()**——每帧一次 DOM 写入，毁掉性能
9. **draw() 中操作 DOM**——布局抖动（比画布操作慢 400-500 倍）

### 解决方案

**减少绘制调用：**
```javascript
// 差：10000 个独立圆
for (let p of particles) {
  ellipse(p.x, p.y, p.size);
}

// 好：用顶点的单一形状
beginShape(POINTS);
for (let p of particles) {
  vertex(p.x, p.y);
}
endShape();

// 最佳：直接像素操作
loadPixels();
for (let p of particles) {
  let idx = 4 * (floor(p.y) * width + floor(p.x));
  pixels[idx] = p.r;
  pixels[idx+1] = p.g;
  pixels[idx+2] = p.b;
  pixels[idx+3] = 255;
}
updatePixels();
```

**邻居查询的空间哈希：**
```javascript
class SpatialHash {
  constructor(cellSize) {
    this.cellSize = cellSize;
    this.cells = new Map();
  }

  clear() { this.cells.clear(); }

  _key(x, y) {
    return `${floor(x / this.cellSize)},${floor(y / this.cellSize)}`;
  }

  insert(obj) {
    let key = this._key(obj.pos.x, obj.pos.y);
    if (!this.cells.has(key)) this.cells.set(key, []);
    this.cells.get(key).push(obj);
  }

  query(x, y, radius) {
    let results = [];
    let minCX = floor((x - radius) / this.cellSize);
    let maxCX = floor((x + radius) / this.cellSize);
    let minCY = floor((y - radius) / this.cellSize);
    let maxCY = floor((y + radius) / this.cellSize);

    for (let cx = minCX; cx <= maxCX; cx++) {
      for (let cy = minCY; cy <= maxCY; cy++) {
        let key = `${cx},${cy}`;
        let cell = this.cells.get(key);
        if (cell) {
          for (let obj of cell) {
            if (dist(x, y, obj.pos.x, obj.pos.y) <= radius) {
              results.push(obj);
            }
          }
        }
      }
    }
    return results;
  }
}
```

**对象池：**
```javascript
class ParticlePool {
  constructor(maxSize) {
    this.pool = [];
    this.active = [];
    for (let i = 0; i < maxSize; i++) {
      this.pool.push(new Particle(0, 0));
    }
  }

  spawn(x, y) {
    let p = this.pool.pop();
    if (p) {
      p.reset(x, y);
      this.active.push(p);
    }
  }

  update() {
    for (let i = this.active.length - 1; i >= 0; i--) {
      this.active[i].update();
      if (this.active[i].isDead()) {
        this.pool.push(this.active.splice(i, 1)[0]);
      }
    }
  }
}
```

**节流重操作：**
```javascript
// 每 N 帧才更新流场
if (frameCount % 5 === 0) {
  flowField.update(frameCount * 0.001);
}
```

### 帧率目标

| 场景 | 目标 | 可接受 |
|---------|--------|------------|
| 交互式草图 | 60fps | 30fps |
| 氛围动画 | 30fps | 20fps |
| 导出/录制 | 30fps 渲染 | 任意（离线） |
| 移动端 | 30fps | 20fps |

### 逐像素渲染预算

像素级操作（`loadPixels()` 循环）是最昂贵的常见模式。预算取决于画布尺寸和每像素的计算量。

| 画布 | 像素数 | 简单噪声（1 次调用） | fBM（4 倍频） | 域畸变（3 层 fBM） |
|--------|--------|----------------------|----------------|--------------------------|
| 540x540 | 291K | ~5ms | ~20ms | ~80ms |
| 1080x1080 | 1.17M | ~20ms | ~80ms | ~300ms+ |
| 1920x1080 | 2.07M | ~35ms | ~140ms | ~500ms+ |
| 3840x2160 | 8.3M | ~140ms | ~560ms | 会崩溃 |

**经验法则：**
- 1080x1080 下每像素 1 次 `noise()` 调用 = ~20ms/帧（30fps 下可接受）
- 1080x1080 下每像素 4 倍频 fBM = ~80ms/帧（临界）
- 1080x1080 下多层域畸变 = 300ms+（实时太慢，`noLoop()` 导出没问题）
- **无头 Chrome 做像素操作比桌面 Chrome 慢 2-5 倍**

**解决方案：以更低分辨率渲染，填充块：**
```javascript
let step = 3;  // 渲染 1/9 像素，填充 3x3 块
loadPixels();
for (let y = 0; y < H; y += step) {
  for (let x = 0; x < W; x += step) {
    let v = expensiveNoise(x, y);
    for (let dy = 0; dy < step && y+dy < H; dy++)
      for (let dx = 0; dx < step && x+dx < W; dx++) {
        let i = 4 * ((y+dy) * W + (x+dx));
        pixels[i] = v; pixels[i+1] = v; pixels[i+2] = v; pixels[i+3] = 255;
      }
  }
}
updatePixels();
```

step=2 带来 4 倍加速。step=3 带来 9 倍。1080p 下可见，但视频可接受（运动能掩盖）。

## 常见错误

### 1. 忘记重置混合模式

```javascript
blendMode(ADD);
image(glowLayer, 0, 0);
// 错误：此后一切都被 ADD 混合
blendMode(BLEND);  // 务必重置
```

### 2. 在 draw() 中创建对象

```javascript
// 差：每帧创建新的字体对象
function draw() {
  let f = loadFont('font.otf');  // 永远不要在 draw() 中加载
}

// 好：在 preload 中加载，在 draw 中使用
let f;
function preload() { f = loadFont('font.otf'); }
```

### 3. 变换时未用 push()/pop()

```javascript
// 差：变换累积
translate(100, 0);
rotate(0.1);
ellipse(0, 0, 50);
// 此后的一切也都被平移和旋转

// 好：隔离的变换
push();
translate(100, 0);
rotate(0.1);
ellipse(0, 0, 50);
pop();
```

### 4. 用整数坐标获得锐利线条

```javascript
// 模糊：子像素渲染
line(10.5, 20.3, 100.7, 80.2);

// 锐利：1px 线用整数 + 0.5
line(10.5, 20.5, 100.5, 80.5);  // 在像素边界上
```

### 5. 像素密度混淆

```javascript
// 错误：假设像素数组与画布尺寸一致
loadPixels();
let idx = 4 * (y * width + x);  // pixelDensity > 1 时错误

// 正确：考虑像素密度
let d = pixelDensity();
loadPixels();
let idx = 4 * ((y * d) * (width * d) + (x * d));

// 最简单：一开始就设 pixelDensity(1)
```

### 6. 色彩模式混淆

```javascript
// HSB 模式下，fill(255) 不是白色
colorMode(HSB, 360, 100, 100);
fill(255);  // 这是 hue=255, sat=100, bri=100 = 鲜艳紫色

// HSB 中的白色：
fill(0, 0, 100);  // 任意色相，0 饱和度，100 亮度

// HSB 中的黑色：
fill(0, 0, 0);
```

### 7. WebGL 的原点在中心

```javascript
// WEBGL 模式下，(0,0) 是中心，不是左上角
function draw() {
  // 这画在中心，不是角落
  rect(0, 0, 100, 100);

  // 要左上角行为：
  translate(-width/2, -height/2);
  rect(0, 0, 100, 100);  // 现在在左上角
}
```

### 8. createGraphics 的清理

```javascript
// 差：内存泄漏——缓冲从不释放
function draw() {
  let temp = createGraphics(width, height);  // 每帧一个新缓冲！
  // ...
}

// 好：创建一次，复用
let temp;
function setup() {
  temp = createGraphics(width, height);
}
function draw() {
  temp.clear();
  // ... 复用 temp
}

// 若必须创建/销毁：
temp.remove();  // 显式释放
```

### 9. noise() 返回 0-1，不是 -1 到 1

```javascript
let n = noise(x);  // 0.0 到 1.0（偏向 0.5）

// 要 -1 到 1 范围：
let n = noise(x) * 2 - 1;

// 要特定范围：
let n = map(noise(x), 0, 1, -100, 100);
```

### 10. draw() 中的 saveCanvas() 每帧都保存

```javascript
// 差：每一帧都保存一个 PNG
function draw() {
  // ... 渲染 ...
  saveCanvas('output', 'png');  // 别这样做
}

// 好：通过键盘保存一次
function keyPressed() {
  if (key === 's') saveCanvas('output', 'png');
}

// 好：渲染完静态作品后保存一次
function draw() {
  // ... 渲染 ...
  saveCanvas('output', 'png');
  noLoop();  // 保存后停止
}
```

### 11. draw() 中的 console.log()

```javascript
// 差：每帧写入 DOM 控制台——巨大开销
function draw() {
  console.log(particles.length);  // 每秒 60 次 DOM 写入
}

// 好：周期性或条件性记录
function draw() {
  if (frameCount % 60 === 0) console.log('FPS:', frameRate().toFixed(1));
}
```

### 12. draw() 中操作 DOM

```javascript
// 差：布局抖动——比画布操作慢 400-500 倍
function draw() {
  document.getElementById('counter').innerText = frameCount;
  let el = document.querySelector('.info');  // 每帧 DOM 查询
}

// 好：缓存 DOM 引用，少更新
let counterEl;
function setup() { counterEl = document.getElementById('counter'); }
function draw() {
  if (frameCount % 30 === 0) counterEl.innerText = frameCount;
}
```

### 13. 生产环境未禁用 FES

```javascript
// 差：每次 p5 函数调用都有错误检查开销（慢达 10 倍）
function setup() { createCanvas(800, 800); }

// 好：在任何 p5 代码之前禁用
p5.disableFriendlyErrors = true;
function setup() { createCanvas(800, 800); }

// 同样可行：使用 p5.min.js（FES 已从压缩构建中剥离）
```

## 浏览器兼容性

### Safari 问题
- WebGL 着色器精度：始终声明 `precision mediump float;`
- `AudioContext` 需要用户手势（`userStartAudio()`）
- 某些 `blendMode()` 选项行为不同

### Firefox 问题
- `textToPoints()` 返回的点数可能略有不同
- WebGL 扩展可能与 Chrome 不同
- 颜色配置文件处理可能造成颜色偏移

### 移动端问题
- 触摸事件需要 `return false` 来阻止滚动
- `devicePixelRatio` 可能是 2 倍或 3 倍——用 `pixelDensity(1)` 提升性能
- 推荐更小的画布（720p 或更低）
- 音频需要显式用户手势才能启动

## CORS 问题

```javascript
// 从外部 URL 加载图像/字体需要 CORS 头
// 本地文件需要服务器：
// python3 -m http.server 8080

// 或对外部资源使用 CORS 代理（生产环境不推荐）
```

## 内存泄漏

### 症状
- 帧率随时间下降
- 浏览器标签页内存无限增长
- 几分钟后页面无响应

### 常见原因

```javascript
// 1. 不断增长的数组
let history = [];
function draw() {
  history.push(someData);  // 永远增长
}
// 修复：限制数组长度
if (history.length > 1000) history.shift();

// 2. 在 draw() 中创建 p5 对象
function draw() {
  let v = createVector(0, 0);  // 每帧分配
}
// 修复：复用预分配的对象

// 3. 未释放的图形缓冲
let layers = [];
function reset() {
  for (let l of layers) l.remove();  // 释放旧缓冲
  layers = [];
}

// 4. 事件监听器累积
function setup() {
  // 差：每次 setup 运行都添加新监听器
  window.addEventListener('resize', handler);
}
// 修复：用 p5 内置的 windowResized()
```

## 调试技巧

### 控制台日志

```javascript
// 只记录一次（而非每帧）
if (frameCount === 1) {
  console.log('Canvas:', width, 'x', height);
  console.log('Pixel density:', pixelDensity());
  console.log('Renderer:', drawingContext.constructor.name);
}

// 周期性记录
if (frameCount % 60 === 0) {
  console.log('FPS:', frameRate().toFixed(1));
  console.log('Particles:', particles.length);
}
```

### 可视化调试

```javascript
// 显示帧率
function draw() {
  // ... 你的草图 ...
  if (CONFIG.debug) {
    fill(255, 0, 0);
    noStroke();
    textSize(14);
    textAlign(LEFT, TOP);
    text('FPS: ' + frameRate().toFixed(1), 10, 10);
    text('Particles: ' + particles.length, 10, 28);
    text('Frame: ' + frameCount, 10, 46);
  }
}

// 用 'd' 键切换调试
function keyPressed() {
  if (key === 'd') CONFIG.debug = !CONFIG.debug;
}
```

### 隔离问题

```javascript
// 注释掉各层以找出慢的那一层
function draw() {
  renderBackground();      // 注释掉以测试
  // renderParticles();    // 这可能慢
  // renderPostEffects();  // 或这层
}
```
