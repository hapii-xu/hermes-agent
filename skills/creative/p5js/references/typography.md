# 字体排版（Typography）

## 加载字体

### 系统字体

```javascript
textFont('Helvetica');
textFont('Georgia');
textFont('monospace');
```

### 自定义字体（OTF/TTF/WOFF2）

```javascript
let myFont;

function preload() {
  myFont = loadFont('path/to/font.otf');
  // 需要本地服务器或开启了 CORS 的 URL
}

function setup() {
  textFont(myFont);
}
```

### 通过 CSS 使用 Google Fonts

```html
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap" rel="stylesheet">
<script>
function setup() {
  textFont('Inter');
}
</script>
```

Google Fonts 无需 `loadFont()` 即可使用，但只适用于 `text()`——不适用于 `textToPoints()`。要做粒子文字，必须用 `loadFont()` 加载 OTF/TTF 文件。

## 文本渲染

### 基础文本

```javascript
textSize(32);
textAlign(CENTER, CENTER);
text('Hello World', width/2, height/2);
```

### 文本属性

```javascript
textSize(48);                    // 像素大小
textAlign(LEFT, TOP);            // 水平：LEFT、CENTER、RIGHT
                                 // 垂直：TOP、CENTER、BOTTOM、BASELINE
textLeading(40);                 // 行距（用于多行文本）
textStyle(BOLD);                 // NORMAL、BOLD、ITALIC、BOLDITALIC
textWrap(WORD);                  // WORD 或 CHAR（用于带最大宽度的 text()）
```

### 文本度量

```javascript
let w = textWidth('Hello');      // 字符串的像素宽度
let a = textAscent();            // 基线以上的高度
let d = textDescent();           // 基线以下的高度
let totalH = a + d;              // 完整行高
```

### 文本边界框

```javascript
let bounds = myFont.textBounds('Hello', x, y, size);
// bounds = { x, y, w, h }
// 可用于定位、碰撞检测、背景矩形
```

### 多行文本

```javascript
// 给定最大宽度——自动换行
textWrap(WORD);
text('Long text that wraps within the given width', x, y, maxWidth);

// 同时给定最大宽度和高度——裁剪
text('Very long text', x, y, maxWidth, maxHeight);
```

## textToPoints()——把文字变成粒子

把文字轮廓转换为点数组。需要加载字体（通过 `loadFont()` 加载 OTF/TTF）。

```javascript
let font;
let points;

function preload() {
  font = loadFont('font.otf');  // 必须是 loadFont，不能用 CSS
}

function setup() {
  createCanvas(1200, 600);
  points = font.textToPoints('HELLO', 100, 400, 200, {
    sampleFactor: 0.1,  // 越小点越多（典型范围 0.1-0.5）
    simplifyThreshold: 0
  });
}

function draw() {
  background(0);
  for (let pt of points) {
    let n = noise(pt.x * 0.01, pt.y * 0.01, frameCount * 0.01);
    fill(255, n * 255);
    noStroke();
    ellipse(pt.x + random(-2, 2), pt.y + random(-2, 2), 3);
  }
}
```

### 文字粒子类

```javascript
class TextParticle {
  constructor(target) {
    this.target = createVector(target.x, target.y);
    this.pos = createVector(random(width), random(height));
    this.vel = createVector(0, 0);
    this.acc = createVector(0, 0);
    this.maxSpeed = 10;
    this.maxForce = 0.5;
  }

  arrive() {
    let desired = p5.Vector.sub(this.target, this.pos);
    let d = desired.mag();
    let speed = d < 100 ? map(d, 0, 100, 0, this.maxSpeed) : this.maxSpeed;
    desired.setMag(speed);
    let steer = p5.Vector.sub(desired, this.vel);
    steer.limit(this.maxForce);
    this.acc.add(steer);
  }

  flee(target, radius) {
    let d = this.pos.dist(target);
    if (d < radius) {
      let desired = p5.Vector.sub(this.pos, target);
      desired.setMag(this.maxSpeed);
      let steer = p5.Vector.sub(desired, this.vel);
      steer.limit(this.maxForce * 2);
      this.acc.add(steer);
    }
  }

  update() {
    this.vel.add(this.acc);
    this.vel.limit(this.maxSpeed);
    this.pos.add(this.vel);
    this.acc.mult(0);
  }

  display() {
    fill(255);
    noStroke();
    ellipse(this.pos.x, this.pos.y, 3);
  }
}

// 用法：粒子组成文字，遇鼠标后散开
let textParticles = [];
for (let pt of points) {
  textParticles.push(new TextParticle(pt));
}

function draw() {
  background(0);
  for (let p of textParticles) {
    p.arrive();
    p.flee(createVector(mouseX, mouseY), 80);
    p.update();
    p.display();
  }
}
```

## 动态字体（Kinetic Typography）

### 波浪文字

```javascript
function waveText(str, x, y, size, amplitude, frequency) {
  textSize(size);
  textAlign(LEFT, BASELINE);
  let xOff = 0;
  for (let i = 0; i < str.length; i++) {
    let yOff = sin(frameCount * 0.05 + i * frequency) * amplitude;
    text(str[i], x + xOff, y + yOff);
    xOff += textWidth(str[i]);
  }
}
```

### 打字机效果

```javascript
class Typewriter {
  constructor(str, x, y, speed = 50) {
    this.str = str;
    this.x = x;
    this.y = y;
    this.speed = speed;  // 每个字符的毫秒数
    this.startTime = millis();
    this.cursor = true;
  }

  display() {
    let elapsed = millis() - this.startTime;
    let chars = min(floor(elapsed / this.speed), this.str.length);
    let visible = this.str.substring(0, chars);

    textAlign(LEFT, TOP);
    text(visible, this.x, this.y);

    // 闪烁的光标
    if (chars < this.str.length && floor(millis() / 500) % 2 === 0) {
      let cursorX = this.x + textWidth(visible);
      line(cursorX, this.y, cursorX, this.y + textAscent() + textDescent());
    }
  }

  isDone() { return millis() - this.startTime >= this.str.length * this.speed; }
}
```

### 逐字符动画

```javascript
function animatedText(str, x, y, size, delay = 50) {
  textSize(size);
  textAlign(LEFT, BASELINE);
  let xOff = 0;

  for (let i = 0; i < str.length; i++) {
    let charStart = i * delay;
    let t = constrain((millis() - charStart) / 500, 0, 1);
    let et = easeOutElastic(t);

    push();
    translate(x + xOff, y);
    scale(et);
    let alpha = t * 255;
    fill(255, alpha);
    text(str[i], 0, 0);
    pop();

    xOff += textWidth(str[i]);
  }
}
```

## 文字作为遮罩

```javascript
let textBuffer;

function setup() {
  createCanvas(800, 800);
  textBuffer = createGraphics(width, height);
  textBuffer.background(0);
  textBuffer.fill(255);
  textBuffer.textSize(200);
  textBuffer.textAlign(CENTER, CENTER);
  textBuffer.text('MASK', width/2, height/2);
}

function draw() {
  // 绘制内容
  background(0);
  // ... 渲染一些彩色的东西

  // 应用文字遮罩（仅在文字为白色的地方显示内容）
  loadPixels();
  textBuffer.loadPixels();
  for (let i = 0; i < pixels.length; i += 4) {
    let maskVal = textBuffer.pixels[i];  // 白色 = 显示，黑色 = 隐藏
    pixels[i + 3] = maskVal;  // 根据遮罩设置 alpha
  }
  updatePixels();
}
```

## 响应式文字大小

```javascript
function responsiveTextSize(baseSize, baseWidth = 1920) {
  return baseSize * (width / baseWidth);
}

// 用法
textSize(responsiveTextSize(48));
text('Scales with canvas', width/2, height/2);
```
