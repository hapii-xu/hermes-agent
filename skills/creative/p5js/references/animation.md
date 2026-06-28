# 动画

## 基于帧的动画

### 绘制循环

```javascript
function draw() {
  // 默认约每秒调用 60 次
  // frameCount — 整数，从 1 开始
  // deltaTime — 距上一帧的毫秒数（用于与帧率无关的运动）
  // millis() — 自草图启动以来的毫秒数
}
```

### 基于时间 vs 基于帧

```javascript
// 基于帧（速度随帧率变化）
x += speed;

// 基于时间（无论帧率如何，速度一致）
x += speed * (deltaTime / 16.67);  // 归一化到 60fps
```

### 归一化时间

```javascript
// 在 N 秒内从 0 进展到 1
let duration = 5000;  // 5 秒，单位毫秒
let t = constrain(millis() / duration, 0, 1);

// 循环进展（0 → 1 → 0 → 1...）
let period = 3000;  // 3 秒一个循环
let t = (millis() % period) / period;

// 乒乓（0 → 1 → 0 → 1...）
let raw = (millis() % (period * 2)) / period;
let t = raw <= 1 ? raw : 2 - raw;
```

## 缓动函数

### 内置 lerp

```javascript
// 线性插值——平滑但机械
let x = lerp(startX, endX, t);

// 用 map 处理非 0-1 区间
let y = map(t, 0, 1, startY, endY);
```

### 常见缓动曲线

```javascript
// 缓入（慢启动）
function easeInQuad(t) { return t * t; }
function easeInCubic(t) { return t * t * t; }
function easeInExpo(t) { return t === 0 ? 0 : pow(2, 10 * (t - 1)); }

// 缓出（慢收尾）
function easeOutQuad(t) { return 1 - (1 - t) * (1 - t); }
function easeOutCubic(t) { return 1 - pow(1 - t, 3); }
function easeOutExpo(t) { return t === 1 ? 1 : 1 - pow(2, -10 * t); }

// 缓入缓出（两端都慢）
function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2;
}
function easeInOutQuint(t) {
  return t < 0.5 ? 16 * t * t * t * t * t : 1 - pow(-2 * t + 2, 5) / 2;
}

// 弹性（弹簧过冲）
function easeOutElastic(t) {
  if (t === 0 || t === 1) return t;
  return pow(2, -10 * t) * sin((t * 10 - 0.75) * (2 * PI / 3)) + 1;
}

// 弹跳
function easeOutBounce(t) {
  if (t < 1/2.75) return 7.5625 * t * t;
  else if (t < 2/2.75) { t -= 1.5/2.75; return 7.5625 * t * t + 0.75; }
  else if (t < 2.5/2.75) { t -= 2.25/2.75; return 7.5625 * t * t + 0.9375; }
  else { t -= 2.625/2.75; return 7.5625 * t * t + 0.984375; }
}

// 平滑阶跃（厄米插值——极佳的默认选择）
function smoothstep(t) { return t * t * (3 - 2 * t); }

// 更平滑阶跃（Ken Perlin）
function smootherstep(t) { return t * t * t * (t * (t * 6 - 15) + 10); }
```

### 应用缓动

```javascript
// 在 duration 毫秒内把 startVal 动画到 endVal
function easedValue(startVal, endVal, startTime, duration, easeFn) {
  let t = constrain((millis() - startTime) / duration, 0, 1);
  return lerp(startVal, endVal, easeFn(t));
}

// 用法
let x = easedValue(100, 700, animStartTime, 2000, easeOutCubic);
```

## 弹簧物理

比缓动更自然——响应力、会过冲、会稳定下来。

```javascript
class Spring {
  constructor(value, target, stiffness = 0.1, damping = 0.7) {
    this.value = value;
    this.target = target;
    this.velocity = 0;
    this.stiffness = stiffness;
    this.damping = damping;
  }

  update() {
    let force = (this.target - this.value) * this.stiffness;
    this.velocity += force;
    this.velocity *= this.damping;
    this.value += this.velocity;
    return this.value;
  }

  setTarget(t) { this.target = t; }
  isSettled(threshold = 0.01) {
    return abs(this.velocity) < threshold && abs(this.value - this.target) < threshold;
  }
}

// 用法
let springX = new Spring(0, 0, 0.08, 0.85);
function draw() {
  springX.setTarget(mouseX);
  let x = springX.update();
  ellipse(x, height/2, 50);
}
```

### 2D 弹簧

```javascript
class Spring2D {
  constructor(x, y) {
    this.pos = createVector(x, y);
    this.target = createVector(x, y);
    this.vel = createVector(0, 0);
    this.stiffness = 0.08;
    this.damping = 0.85;
  }

  update() {
    let force = p5.Vector.sub(this.target, this.pos).mult(this.stiffness);
    this.vel.add(force).mult(this.damping);
    this.pos.add(this.vel);
    return this.pos;
  }
}
```

## 状态机

用于复杂的多阶段动画。

```javascript
const STATES = { IDLE: 0, ENTER: 1, ACTIVE: 2, EXIT: 3 };
let state = STATES.IDLE;
let stateStart = 0;

function setState(newState) {
  state = newState;
  stateStart = millis();
}

function stateTime() {
  return millis() - stateStart;
}

function draw() {
  switch (state) {
    case STATES.IDLE:
      // 等待中...
      break;
    case STATES.ENTER:
      let t = constrain(stateTime() / 1000, 0, 1);
      let alpha = easeOutCubic(t) * 255;
      // 淡入...
      if (t >= 1) setState(STATES.ACTIVE);
      break;
    case STATES.ACTIVE:
      // 主动画...
      break;
    case STATES.EXIT:
      let t2 = constrain(stateTime() / 500, 0, 1);
      // 淡出...
      if (t2 >= 1) setState(STATES.IDLE);
      break;
  }
}
```

## 时间轴编排

用于定时的多场景动画（动态图形、片头序列）。

```javascript
class Timeline {
  constructor() {
    this.events = [];
  }

  at(timeMs, duration, fn) {
    this.events.push({ start: timeMs, end: timeMs + duration, fn });
    return this;
  }

  update() {
    let now = millis();
    for (let e of this.events) {
      if (now >= e.start && now < e.end) {
        let t = (now - e.start) / (e.end - e.start);
        e.fn(t);
      }
    }
  }
}

// 用法
let timeline = new Timeline();
timeline
  .at(0, 2000, (t) => {
    // 场景 1：片头淡入（0-2 秒）
    let alpha = easeOutCubic(t) * 255;
    fill(255, alpha);
    textSize(48);
    text("Hello", width/2, height/2);
  })
  .at(2000, 1000, (t) => {
    // 场景 2：片头淡出（2-3 秒）
    let alpha = (1 - easeInCubic(t)) * 255;
    fill(255, alpha);
    textSize(48);
    text("Hello", width/2, height/2);
  })
  .at(3000, 5000, (t) => {
    // 场景 3：主要内容（3-8 秒）
    renderMainContent(t);
  });

function draw() {
  background(0);
  timeline.update();
}
```

## 噪声驱动的运动

比确定性动画更有机。

```javascript
// 平滑游荡的位置
let x = map(noise(frameCount * 0.005, 0), 0, 1, 0, width);
let y = map(noise(0, frameCount * 0.005), 0, 1, 0, height);

// 噪声驱动的旋转
let angle = noise(frameCount * 0.01) * TWO_PI;

// 噪声驱动的缩放（呼吸效果）
let s = map(noise(frameCount * 0.02), 0, 1, 0.8, 1.2);

// 噪声驱动的色相偏移
let hue = map(noise(frameCount * 0.003), 0, 1, 0, 360);
```

## 转场模式

### 淡入/淡出

```javascript
function fadeIn(t) { return constrain(t, 0, 1); }
function fadeOut(t) { return constrain(1 - t, 0, 1); }
```

### 滑入

```javascript
function slideIn(t, direction = 'left') {
  let et = easeOutCubic(t);
  switch (direction) {
    case 'left': return lerp(-width, 0, et);
    case 'right': return lerp(width, 0, et);
    case 'up': return lerp(-height, 0, et);
    case 'down': return lerp(height, 0, et);
  }
}
```

### 缩放揭示

```javascript
function scaleReveal(t) {
  let et = easeOutElastic(constrain(t, 0, 1));
  push();
  translate(width/2, height/2);
  scale(et);
  translate(-width/2, -height/2);
  // 绘制内容...
  pop();
}
```

### 错峰入场

```javascript
// N 个元素依次出现
let staggerDelay = 100;  // 每个元素之间的毫秒间隔
for (let i = 0; i < elements.length; i++) {
  let itemStart = baseTime + i * staggerDelay;
  let t = constrain((millis() - itemStart) / 500, 0, 1);
  let alpha = easeOutCubic(t) * 255;
  let yOffset = lerp(30, 0, easeOutCubic(t));
  // 用 alpha 和 yOffset 绘制元素
}
```

## 录制确定性动画

要做帧精确的导出，使用帧计数而非 millis()：

```javascript
const TOTAL_FRAMES = 300;  // 30fps 下 10 秒
const FPS = 30;

function draw() {
  let t = frameCount / TOTAL_FRAMES;  // 整段时长内从 0 到 1
  if (t > 1) { noLoop(); return; }

  // 所有动画定时都使用 t——确定性
  renderFrame(t);

  // 导出
  if (CONFIG.recording) {
    saveCanvas('frame-' + nf(frameCount, 4), 'png');
  }
}
```

## 场景淡入淡出包络（视频）

多场景视频中的每个场景都需要淡入和淡出。视觉差异大的生成场景之间硬切会让人不适。

```javascript
const SCENE_FRAMES = 150;  // 30fps 下 5 秒
const FADE = 15;           // 半秒淡入淡出

function draw() {
  let lf = frameCount - 1;  // 从 0 开始的本地帧
  let t = lf / SCENE_FRAMES; // 归一化进度 0..1

  // 淡入淡出包络：开头渐升，结尾渐降
  let fade = 1;
  if (lf < FADE) fade = lf / FADE;
  if (lf > SCENE_FRAMES - FADE) fade = (SCENE_FRAMES - lf) / FADE;
  fade = fade * fade * (3 - 2 * fade);  // smoothstep，更有机

  // 把淡入淡出应用到所有视觉输出
  // 方式 1：把 alpha 值乘以 fade
  fill(r, g, b, alpha * fade);

  // 方式 2：对整张合成图像做 tint
  tint(255, fade * 255);
  image(sceneBuffer, 0, 0);
  noTint();

  // 方式 3：乘以像素亮度（用于像素级场景）
  pixels[i] = r * fade;
}
```

## 为静态算法添加动画

某些生成算法产出单一静态结果（吸引子、圆堆叠、Voronoi）。在视频中，静态内容看起来像卡住/出 bug。添加运动的技法：

### 渐进揭示

从中心向外扩展一个遮罩，揭示预先计算的结果：

```javascript
let revealRadius = easeOutCubic(min(t * 1.5, 1)) * (width * 0.8);
// 在渲染循环中，跳过距中心超过 revealRadius 的像素
let dx = x - width/2, dy = y - height/2;
if (sqrt(dx*dx + dy*dy) > revealRadius) continue;
// 软边缘：
let edgeFade = constrain((revealRadius - dist) / 40, 0, 1);
```

### 参数扫描

缓慢改变一个参数，展示算法的演化：

```javascript
// 带漂移参数的吸引子
let a = -1.7 + sin(t * 0.5) * 0.2;  // 在基准值附近振荡
let b = 1.3 + cos(t * 0.3) * 0.15;
```

### 缓慢相机运动

对最终图像施加微妙的缩放或旋转：

```javascript
push();
translate(width/2, height/2);
scale(1 + t * 0.05);       // 场景时长内缓慢 5% 缩放
rotate(t * 0.1);            // 轻微旋转
translate(-width/2, -height/2);
image(precomputedResult, 0, 0);
pop();
```

### 叠加动态元素

在静态内容之上添加粒子、颗粒或细微噪声：

```javascript
// 静态背景
image(staticResult, 0, 0);
// 动态叠加
for (let p of ambientParticles) {
  p.update();
  p.display();  // 缓慢移动的微粒增添生气
}
```
