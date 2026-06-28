# 视觉特效

## 噪声

### 柏林噪声基础

```javascript
noiseSeed(42);
noiseDetail(4, 0.5);  // 倍频数，衰减

// 1D 噪声——平滑起伏
let y = noise(x * 0.01);  // 返回 0.0 到 1.0

// 2D 噪声——地形/纹理
let v = noise(x * 0.005, y * 0.005);

// 3D 噪声——动画化的 2D 场（z = 时间）
let v = noise(x * 0.005, y * 0.005, frameCount * 0.005);
```

缩放因子（0.005 等）至关重要：
- `0.001`——非常平滑，大尺度特征
- `0.005`——平滑，中等特征
- `0.01`——标准生成艺术尺度
- `0.05`——细致，小特征
- `0.1`——近随机，颗粒感

### 分形布朗运动（fBM）

叠加多倍频噪声以获得自然纹理。每个倍频在更小尺度上添加细节。

```javascript
function fbm(x, y, octaves = 6, lacunarity = 2.0, gain = 0.5) {
  let value = 0;
  let amplitude = 1.0;
  let frequency = 1.0;
  let maxValue = 0;
  for (let i = 0; i < octaves; i++) {
    value += noise(x * frequency, y * frequency) * amplitude;
    maxValue += amplitude;
    amplitude *= gain;
    frequency *= lacunarity;
  }
  return value / maxValue;
}
```

### 域畸变

把噪声输出回喂为输入坐标，获得流动的有机扭曲。

```javascript
function domainWarp(x, y, scale, strength, time) {
  // 第一次畸变通道
  let qx = fbm(x + 0.0, y + 0.0);
  let qy = fbm(x + 5.2, y + 1.3);

  // 第二次畸变通道（回喂）
  let rx = fbm(x + strength * qx + 1.7, y + strength * qy + 9.2, 4, 2, 0.5);
  let ry = fbm(x + strength * qx + 8.3, y + strength * qy + 2.8, 4, 2, 0.5);

  return fbm(x + strength * rx + time, y + strength * ry + time);
}
```

### 旋度噪声

无散度的噪声场。跟随旋度噪声的粒子永不汇聚或发散——它们以平滑、漩涡式的模式流动。

```javascript
function curlNoise(x, y, scale, time) {
  let eps = 0.001;
  // 通过有限差分求偏导
  let dndx = (noise(x * scale + eps, y * scale, time) -
              noise(x * scale - eps, y * scale, time)) / (2 * eps);
  let dndy = (noise(x * scale, y * scale + eps, time) -
              noise(x * scale, y * scale - eps, time)) / (2 * eps);
  // 旋度 = 与梯度垂直
  return createVector(dndy, -dndx);
}
```

## 流场

一个由向量组成的网格，用来引导粒子。这是生成艺术的基础技法。

```javascript
class FlowField {
  constructor(resolution, noiseScale) {
    this.resolution = resolution;
    this.cols = ceil(width / resolution);
    this.rows = ceil(height / resolution);
    this.field = new Array(this.cols * this.rows);
    this.noiseScale = noiseScale;
  }

  update(time) {
    for (let i = 0; i < this.cols; i++) {
      for (let j = 0; j < this.rows; j++) {
        let angle = noise(i * this.noiseScale, j * this.noiseScale, time) * TWO_PI * 2;
        this.field[i + j * this.cols] = p5.Vector.fromAngle(angle);
      }
    }
  }

  lookup(x, y) {
    let col = constrain(floor(x / this.resolution), 0, this.cols - 1);
    let row = constrain(floor(y / this.resolution), 0, this.rows - 1);
    return this.field[col + row * this.cols].copy();
  }
}
```

### 流场粒子

```javascript
class FlowParticle {
  constructor(x, y) {
    this.pos = createVector(x, y);
    this.vel = createVector(0, 0);
    this.acc = createVector(0, 0);
    this.prev = this.pos.copy();
    this.maxSpeed = 2;
    this.life = 1.0;
  }

  follow(field) {
    let force = field.lookup(this.pos.x, this.pos.y);
    force.mult(0.5);  // 力的大小
    this.acc.add(force);
  }

  update() {
    this.prev = this.pos.copy();
    this.vel.add(this.acc);
    this.vel.limit(this.maxSpeed);
    this.pos.add(this.vel);
    this.acc.mult(0);
    this.life -= 0.001;
  }

  edges() {
    if (this.pos.x > width) this.pos.x = 0;
    if (this.pos.x < 0) this.pos.x = width;
    if (this.pos.y > height) this.pos.y = 0;
    if (this.pos.y < 0) this.pos.y = height;
    this.prev = this.pos.copy();  // 防止环绕线
  }

  display(buffer) {
    buffer.stroke(255, this.life * 30);
    buffer.strokeWeight(0.5);
    buffer.line(this.prev.x, this.prev.y, this.pos.x, this.pos.y);
  }
}
```

## 粒子系统

### 基础物理粒子

```javascript
class Particle {
  constructor(x, y) {
    this.pos = createVector(x, y);
    this.vel = p5.Vector.random2D().mult(random(1, 3));
    this.acc = createVector(0, 0);
    this.life = 255;
    this.decay = random(1, 5);
    this.size = random(3, 8);
  }

  applyForce(f) { this.acc.add(f); }

  update() {
    this.vel.add(this.acc);
    this.pos.add(this.vel);
    this.acc.mult(0);
    this.life -= this.decay;
  }

  display() {
    noStroke();
    fill(255, this.life);
    ellipse(this.pos.x, this.pos.y, this.size);
  }

  isDead() { return this.life <= 0; }
}
```

### 吸引子驱动的粒子

```javascript
class Attractor {
  constructor(x, y, strength) {
    this.pos = createVector(x, y);
    this.strength = strength;
  }

  attract(particle) {
    let force = p5.Vector.sub(this.pos, particle.pos);
    let d = constrain(force.mag(), 5, 200);
    force.normalize();
    force.mult(this.strength / (d * d));
    particle.applyForce(force);
  }
}
```

### Boid 群聚

```javascript
class Boid {
  constructor(x, y) {
    this.pos = createVector(x, y);
    this.vel = p5.Vector.random2D().mult(random(2, 4));
    this.acc = createVector(0, 0);
    this.maxForce = 0.2;
    this.maxSpeed = 4;
    this.perceptionRadius = 50;
  }

  flock(boids) {
    let alignment = createVector(0, 0);
    let cohesion = createVector(0, 0);
    let separation = createVector(0, 0);
    let total = 0;

    for (let other of boids) {
      let d = this.pos.dist(other.pos);
      if (other !== this && d < this.perceptionRadius) {
        alignment.add(other.vel);
        cohesion.add(other.pos);
        let diff = p5.Vector.sub(this.pos, other.pos);
        diff.div(d * d);
        separation.add(diff);
        total++;
      }
    }
    if (total > 0) {
      alignment.div(total).setMag(this.maxSpeed).sub(this.vel).limit(this.maxForce);
      cohesion.div(total).sub(this.pos).setMag(this.maxSpeed).sub(this.vel).limit(this.maxForce);
      separation.div(total).setMag(this.maxSpeed).sub(this.vel).limit(this.maxForce);
    }

    this.acc.add(alignment.mult(1.0));
    this.acc.add(cohesion.mult(1.0));
    this.acc.add(separation.mult(1.5));
  }

  update() {
    this.vel.add(this.acc);
    this.vel.limit(this.maxSpeed);
    this.pos.add(this.vel);
    this.acc.mult(0);
  }
}
```

## 像素操作

### 读写像素

```javascript
loadPixels();
for (let y = 0; y < height; y++) {
  for (let x = 0; x < width; x++) {
    let idx = 4 * (y * width + x);
    let r = pixels[idx];
    let g = pixels[idx + 1];
    let b = pixels[idx + 2];
    let a = pixels[idx + 3];

    // 修改
    pixels[idx] = 255 - r;       // 红色反转
    pixels[idx + 1] = 255 - g;   // 绿色反转
    pixels[idx + 2] = 255 - b;   // 蓝色反转
  }
}
updatePixels();
```

### 像素级噪声纹理

```javascript
loadPixels();
for (let i = 0; i < pixels.length; i += 4) {
  let x = (i / 4) % width;
  let y = floor((i / 4) / width);
  let n = noise(x * 0.01, y * 0.01, frameCount * 0.02);
  let c = n * 255;
  pixels[i] = c;
  pixels[i + 1] = c;
  pixels[i + 2] = c;
  pixels[i + 3] = 255;
}
updatePixels();
```

### 内置滤镜

```javascript
filter(BLUR, 3);        // 高斯模糊（半径）
filter(THRESHOLD, 0.5); // 黑/白阈值
filter(INVERT);          // 颜色反转
filter(POSTERIZE, 4);    // 减少色阶
filter(GRAY);            // 去饱和
filter(ERODE);           // 收缩亮区
filter(DILATE);          // 扩展亮区
filter(OPAQUE);          // 移除透明度
```

## 纹理生成

### 点画 / 点彩派

```javascript
function stipple(buffer, density, minSize, maxSize) {
  buffer.loadPixels();
  for (let i = 0; i < density; i++) {
    let x = floor(random(width));
    let y = floor(random(height));
    let idx = 4 * (y * width + x);
    let brightness = (buffer.pixels[idx] + buffer.pixels[idx+1] + buffer.pixels[idx+2]) / 3;
    let size = map(brightness, 0, 255, maxSize, minSize);
    if (random() < map(brightness, 0, 255, 0.8, 0.1)) {
      noStroke();
      fill(buffer.pixels[idx], buffer.pixels[idx+1], buffer.pixels[idx+2]);
      ellipse(x, y, size);
    }
  }
}
```

### 半色调

```javascript
function halftone(sourceBuffer, dotSpacing, maxDotSize) {
  sourceBuffer.loadPixels();
  background(255);
  fill(0);
  noStroke();
  for (let y = 0; y < height; y += dotSpacing) {
    for (let x = 0; x < width; x += dotSpacing) {
      let idx = 4 * (y * width + x);
      let brightness = (sourceBuffer.pixels[idx] + sourceBuffer.pixels[idx+1] + sourceBuffer.pixels[idx+2]) / 3;
      let dotSize = map(brightness, 0, 255, maxDotSize, 0);
      ellipse(x + dotSpacing/2, y + dotSpacing/2, dotSize);
    }
  }
}
```

### 交叉排线

```javascript
function crossHatch(x, y, w, h, value, spacing) {
  // value：0（深）到 1（浅）
  let numLayers = floor(map(value, 0, 1, 4, 0));
  let angles = [PI/4, -PI/4, 0, PI/2];

  for (let layer = 0; layer < numLayers; layer++) {
    push();
    translate(x + w/2, y + h/2);
    rotate(angles[layer]);
    let s = spacing + layer * 2;
    for (let i = -max(w, h); i < max(w, h); i += s) {
      line(i, -max(w, h), i, max(w, h));
    }
    pop();
  }
}
```

## 反馈回路

### 帧反馈（回声/拖尾）

```javascript
let feedback;

function setup() {
  createCanvas(800, 800);
  feedback = createGraphics(width, height);
}

function draw() {
  // 复制当前反馈，略微缩放和旋转
  let temp = feedback.get();

  feedback.push();
  feedback.translate(width/2, height/2);
  feedback.scale(1.005);  // 缓慢缩放
  feedback.rotate(0.002); // 缓慢旋转
  feedback.translate(-width/2, -height/2);
  feedback.tint(255, 245);  // 轻微渐隐
  feedback.image(temp, 0, 0);
  feedback.pop();

  // 把新内容绘到反馈上
  feedback.noStroke();
  feedback.fill(255);
  feedback.ellipse(mouseX, mouseY, 20);

  // 显示
  image(feedback, 0, 0);
}
```

### 泛光 / 辉光（后期处理）

把场景降采样到一个小缓冲，模糊，再以相加方式叠加。在亮区周围创造柔和辉光。这是标准的生成艺术泛光技法。

```javascript
let scene, bloomBuf;

function setup() {
  createCanvas(1080, 1080);
  scene = createGraphics(width, height);
  bloomBuf = createGraphics(width, height);
}

function draw() {
  // 1. 把场景渲染到离屏缓冲
  scene.background(0);
  scene.fill(255, 200, 100);
  scene.noStroke();
  // ... 把亮元素绘到 scene ...

  // 2. 构建泛光：降采样 → 模糊 → 升采样
  bloomBuf.clear();
  bloomBuf.image(scene, 0, 0, width / 4, height / 4);  // 4 倍降采样
  bloomBuf.filter(BLUR, 6);  // 模糊小版本

  // 3. 合成：场景 + 相加式泛光
  background(0);
  image(scene, 0, 0);           // 基础层
  blendMode(ADD);               // 相加 = 辉光
  tint(255, 80);                // 控制泛光强度（0-255）
  image(bloomBuf, 0, 0, width, height);  // 升采样回全尺寸
  noTint();
  blendMode(BLEND);             // 务必重置混合模式
}
```

**调参：**
- 降采样比（1/4 是标准，1/8 更柔，1/2 更紧）
- 模糊半径（典型 4-8，越大辉光越宽）
- Tint alpha（40-120，控制辉光强度）
- 每 N 帧更新泛光以省性能：`if (frameCount % 2 === 0) { ... }`

**常见错误：** 在 ADD 通道后忘记 `blendMode(BLEND)`——此后绘的一切都会变成相加式。

### 拖尾缓冲亮度

通过 `createGraphics()` + 半透明渐隐矩形做拖尾累积是粒子拖尾的标准技法，但**拖尾总是比你预期的更暗**。渐隐矩形的 alpha 每帧以乘法复合。

```javascript
// 渐隐矩形 alpha 同时控制拖尾长度和亮度：
trailBuf.fill(0, 0, 0, alpha);
trailBuf.rect(0, 0, width, height);

// alpha=5  → 极长拖尾，极暗（内容约 35 帧后渐隐到 50%）
// alpha=10 → 长拖尾，暗
// alpha=20 → 中等拖尾，可见
// alpha=40 → 短拖尾，亮
// alpha=80 → 极短拖尾，清晰
```

**陷阱：** 你为长拖尾设了 alpha=5，但 alpha=30 的粒子描边却看不见，因为它们在累积到足够密度前就渐隐了。要么：
- **把描边 alpha 提到** 80-150（而非直觉上的 20-40）
- **降低渐隐 alpha**，但接受更短的拖尾
- **对描边使用相加混合**：亮粒子累积，暗粒子保持暗

```javascript
// 错误：低渐隐 + 低描边 = 看不见
trailBuf.fill(0, 0, 0, 5);     // 长拖尾
trailBuf.rect(0, 0, W, H);
trailBuf.stroke(255, 30);       // 太暗，永远累积不起来
trailBuf.line(px, py, x, y);

// 正确：低渐隐 + 高描边 = 可见的长拖尾
trailBuf.fill(0, 0, 0, 5);
trailBuf.rect(0, 0, W, H);
trailBuf.stroke(255, 100);      // 足够亮，能穿过渐隐留存
trailBuf.line(px, py, x, y);
```

### 反应-扩散（Gray-Scott）

```javascript
class ReactionDiffusion {
  constructor(w, h) {
    this.w = w;
    this.h = h;
    this.a = new Float32Array(w * h).fill(1);
    this.b = new Float32Array(w * h).fill(0);
    this.nextA = new Float32Array(w * h);
    this.nextB = new Float32Array(w * h);
    this.dA = 1.0;
    this.dB = 0.5;
    this.feed = 0.055;
    this.kill = 0.062;
  }

  seed(cx, cy, r) {
    for (let y = cy - r; y < cy + r; y++) {
      for (let x = cx - r; x < cx + r; x++) {
        if (dist(x, y, cx, cy) < r) {
          let idx = y * this.w + x;
          this.b[idx] = 1;
        }
      }
    }
  }

  step() {
    for (let y = 1; y < this.h - 1; y++) {
      for (let x = 1; x < this.w - 1; x++) {
        let idx = y * this.w + x;
        let a = this.a[idx], b = this.b[idx];
        let lapA = this.laplacian(this.a, x, y);
        let lapB = this.laplacian(this.b, x, y);
        let abb = a * b * b;
        this.nextA[idx] = constrain(a + this.dA * lapA - abb + this.feed * (1 - a), 0, 1);
        this.nextB[idx] = constrain(b + this.dB * lapB + abb - (this.kill + this.feed) * b, 0, 1);
      }
    }
    [this.a, this.nextA] = [this.nextA, this.a];
    [this.b, this.nextB] = [this.nextB, this.b];
  }

  laplacian(arr, x, y) {
    let w = this.w;
    return arr[(y-1)*w+x] + arr[(y+1)*w+x] + arr[y*w+(x-1)] + arr[y*w+(x+1)]
           - 4 * arr[y*w+x];
  }
}
```

## 像素排序

```javascript
function pixelSort(buffer, threshold, direction = 'horizontal') {
  buffer.loadPixels();
  let px = buffer.pixels;

  if (direction === 'horizontal') {
    for (let y = 0; y < height; y++) {
      let spans = findSpans(px, y, width, threshold, true);
      for (let span of spans) {
        sortSpan(px, span.start, span.end, y, true);
      }
    }
  }
  buffer.updatePixels();
}

function findSpans(px, row, w, threshold, horizontal) {
  let spans = [];
  let start = -1;
  for (let i = 0; i < w; i++) {
    let idx = horizontal ? 4 * (row * w + i) : 4 * (i * w + row);
    let brightness = (px[idx] + px[idx+1] + px[idx+2]) / 3;
    if (brightness > threshold && start === -1) {
      start = i;
    } else if (brightness <= threshold && start !== -1) {
      spans.push({ start, end: i });
      start = -1;
    }
  }
  if (start !== -1) spans.push({ start, end: w });
  return spans;
}
```

## 高级生成技法

### L 系统（Lindenmayer 系统）

基于文法的递归生长，用于树木、植物、分形。

```javascript
class LSystem {
  constructor(axiom, rules) {
    this.axiom = axiom;
    this.rules = rules;  // { 'F': 'F[+F]F[-F]F' }
    this.sentence = axiom;
  }

  generate(iterations) {
    for (let i = 0; i < iterations; i++) {
      let next = '';
      for (let ch of this.sentence) {
        next += this.rules[ch] || ch;
      }
      this.sentence = next;
    }
  }

  draw(len, angle) {
    for (let ch of this.sentence) {
      switch (ch) {
        case 'F': line(0, 0, 0, -len); translate(0, -len); break;
        case '+': rotate(angle); break;
        case '-': rotate(-angle); break;
        case '[': push(); break;
        case ']': pop(); break;
      }
    }
  }
}

// 用法：分形植物
let lsys = new LSystem('X', {
  'X': 'F+[[X]-X]-F[-FX]+X',
  'F': 'FF'
});
lsys.generate(5);
translate(width/2, height);
lsys.draw(4, radians(25));
```

### 圆堆叠

用大小不一、互不重叠的圆填满空间。

```javascript
class PackedCircle {
  constructor(x, y, r) {
    this.x = x; this.y = y; this.r = r;
    this.growing = true;
  }

  grow() { if (this.growing) this.r += 0.5; }

  overlaps(other) {
    let d = dist(this.x, this.y, other.x, other.y);
    return d < this.r + other.r + 2;  // +2 间隙
  }

  atEdge() {
    return this.x - this.r < 0 || this.x + this.r > width ||
           this.y - this.r < 0 || this.y + this.r > height;
  }
}

let circles = [];

function packStep() {
  // 尝试放置新圆
  for (let attempts = 0; attempts < 100; attempts++) {
    let x = random(width), y = random(height);
    let valid = true;
    for (let c of circles) {
      if (dist(x, y, c.x, c.y) < c.r + 2) { valid = false; break; }
    }
    if (valid) { circles.push(new PackedCircle(x, y, 1)); break; }
  }

  // 让现有圆生长
  for (let c of circles) {
    if (!c.growing) continue;
    c.grow();
    if (c.atEdge()) { c.growing = false; continue; }
    for (let other of circles) {
      if (c !== other && c.overlaps(other)) { c.growing = false; break; }
    }
  }
}
```

### Voronoi 图（Fortune 算法近似）

```javascript
// 简单的暴力 Voronoi（适用于小点数）
function drawVoronoi(points, colors) {
  loadPixels();
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let minDist = Infinity;
      let closest = 0;
      for (let i = 0; i < points.length; i++) {
        let d = (x - points[i].x) ** 2 + (y - points[i].y) ** 2;  // magSq
        if (d < minDist) { minDist = d; closest = i; }
      }
      let idx = 4 * (y * width + x);
      let c = colors[closest % colors.length];
      pixels[idx] = red(c);
      pixels[idx+1] = green(c);
      pixels[idx+2] = blue(c);
      pixels[idx+3] = 255;
    }
  }
  updatePixels();
}
```

### 分形树

```javascript
function fractalTree(x, y, len, angle, depth, branchAngle) {
  if (depth <= 0 || len < 2) return;

  let x2 = x + Math.cos(angle) * len;
  let y2 = y + Math.sin(angle) * len;

  strokeWeight(map(depth, 0, 10, 0.5, 4));
  line(x, y, x2, y2);

  let shrink = 0.67 + noise(x * 0.01, y * 0.01) * 0.15;
  fractalTree(x2, y2, len * shrink, angle - branchAngle, depth - 1, branchAngle);
  fractalTree(x2, y2, len * shrink, angle + branchAngle, depth - 1, branchAngle);
}

// 用法
fractalTree(width/2, height, 120, -HALF_PI, 10, PI/6);
```

### 奇异吸引子

```javascript
// Clifford 吸引子
function cliffordAttractor(a, b, c, d, iterations) {
  let x = 0, y = 0;
  beginShape(POINTS);
  for (let i = 0; i < iterations; i++) {
    let nx = Math.sin(a * y) + c * Math.cos(a * x);
    let ny = Math.sin(b * x) + d * Math.cos(b * y);
    x = nx; y = ny;
    let px = map(x, -3, 3, 0, width);
    let py = map(y, -3, 3, 0, height);
    vertex(px, py);
  }
  endShape();
}

// De Jong 吸引子
function deJongAttractor(a, b, c, d, iterations) {
  let x = 0, y = 0;
  beginShape(POINTS);
  for (let i = 0; i < iterations; i++) {
    let nx = Math.sin(a * y) - Math.cos(b * x);
    let ny = Math.sin(c * x) - Math.cos(d * y);
    x = nx; y = ny;
    let px = map(x, -2.5, 2.5, 0, width);
    let py = map(y, -2.5, 2.5, 0, height);
    vertex(px, py);
  }
  endShape();
}
```

### 泊松盘采样

看起来自然的均匀分布——比纯随机更适合放置元素。

```javascript
function poissonDiskSampling(r, k = 30) {
  let cellSize = r / Math.sqrt(2);
  let cols = Math.ceil(width / cellSize);
  let rows = Math.ceil(height / cellSize);
  let grid = new Array(cols * rows).fill(-1);
  let points = [];
  let active = [];

  function gridIndex(x, y) {
    return Math.floor(x / cellSize) + Math.floor(y / cellSize) * cols;
  }

  // 种子
  let p0 = createVector(random(width), random(height));
  points.push(p0);
  active.push(p0);
  grid[gridIndex(p0.x, p0.y)] = 0;

  while (active.length > 0) {
    let idx = Math.floor(Math.random() * active.length);
    let pos = active[idx];
    let found = false;

    for (let n = 0; n < k; n++) {
      let angle = Math.random() * TWO_PI;
      let mag = r + Math.random() * r;
      let sample = createVector(pos.x + Math.cos(angle) * mag, pos.y + Math.sin(angle) * mag);

      if (sample.x < 0 || sample.x >= width || sample.y < 0 || sample.y >= height) continue;

      let col = Math.floor(sample.x / cellSize);
      let row = Math.floor(sample.y / cellSize);
      let ok = true;

      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) {
          let nc = col + dx, nr = row + dy;
          if (nc >= 0 && nc < cols && nr >= 0 && nr < rows) {
            let gi = nc + nr * cols;
            if (grid[gi] !== -1 && points[grid[gi]].dist(sample) < r) { ok = false; }
          }
        }
      }

      if (ok) {
        points.push(sample);
        active.push(sample);
        grid[gridIndex(sample.x, sample.y)] = points.length - 1;
        found = true;
        break;
      }
    }
    if (!found) active.splice(idx, 1);
  }
  return points;
}
```

## 附加库

### p5.brush — 自然媒介

手绘、有机美学。水彩、炭笔、钢笔、马克笔。需要 **p5.js 2.x + WEBGL**。

```html
<script src="https://cdn.jsdelivr.net/npm/p5.brush@latest/dist/p5.brush.js"></script>
```

```javascript
function setup() {
  createCanvas(1200, 1200, WEBGL);
  brush.scaleBrushes(3);  // 正确尺寸所必需
  translate(-width/2, -height/2);  // WEBGL 原点在中心
  brush.pick('2B');  // 铅笔笔刷
  brush.stroke(50, 50, 50);
  brush.strokeWeight(2);
  brush.line(100, 100, 500, 500);
  brush.pick('watercolor');
  brush.fill('#4a90d9', 150);
  brush.circle(400, 400, 200);
}
```

内置笔刷：`2B`、`HB`、`2H`、`cpencil`、`pen`、`rotring`、`spray`、`marker`、`charcoal`、`hatch_brush`。
内置向量场：`hand`、`curved`、`zigzag`、`waves`、`seabed`、`spiral`、`columns`。

### p5.grain — 胶片颗粒与纹理

```html
<script src="https://cdn.jsdelivr.net/npm/p5.grain@0.7.0/p5.grain.min.js"></script>
```

```javascript
function draw() {
  // ... 渲染场景 ...
  applyMonochromaticGrain(42);   // 均匀颗粒
  // 或：applyChromaticGrain(42); // 按通道随机化
}
```

### CCapture.js — 确定性视频捕获

以固定帧率录制画布，无论实际渲染速度如何。复杂生成艺术必备。

```html
<script src="https://cdn.jsdelivr.net/npm/ccapture.js-npmfixed/build/CCapture.all.min.js"></script>
```

```javascript
let capturer;

function setup() {
  createCanvas(1920, 1080);
  capturer = new CCapture({
    format: 'webm',
    framerate: 60,
    quality: 99,
    // timeLimit: 10,    // N 秒后自动停止
    // motionBlurFrames: 4  // 超采样的运动模糊
  });
}

function startRecording() {
  capturer.start();
}

function draw() {
  // ... 渲染帧 ...
  if (capturer) capturer.capture(document.querySelector('canvas'));
}

function stopRecording() {
  capturer.stop();
  capturer.save();  // 触发下载
}
```
