# 交互

## 鼠标事件

### 持续状态

```javascript
mouseX, mouseY          // 当前位置（相对于画布）
pmouseX, pmouseY        // 上一帧的位置
mouseIsPressed          // 布尔值
mouseButton             // LEFT、RIGHT、CENTER（按下期间）
movedX, movedY          // 相对上一帧的增量
winMouseX, winMouseY    // 相对于窗口（而非画布）
```

### 事件回调

```javascript
function mousePressed() {
  // 按下时触发一次
  // mouseButton 指示是哪个按键
}

function mouseReleased() {
  // 释放时触发一次
}

function mouseClicked() {
  // 按下+释放后触发（同一元素）
}

function doubleClicked() {
  // 双击时触发
}

function mouseMoved() {
  // 鼠标移动时触发（无按键按下）
}

function mouseDragged() {
  // 鼠标移动且按下按键时触发
}

function mouseWheel(event) {
  // event.delta：正值 = 向下滚动，负值 = 向上滚动
  zoom += event.delta * -0.01;
  return false;  // 阻止页面滚动
}
```

### 鼠标交互模式

**点击生成粒子：**
```javascript
function mousePressed() {
  particles.push(new Particle(mouseX, mouseY));
}
```

**带弹簧的鼠标跟随：**
```javascript
let springX, springY;
function setup() {
  springX = new Spring(width/2, width/2);
  springY = new Spring(height/2, height/2);
}
function draw() {
  springX.setTarget(mouseX);
  springY.setTarget(mouseY);
  let x = springX.update();
  let y = springY.update();
  ellipse(x, y, 50);
}
```

**拖拽交互：**
```javascript
let dragging = false;
let dragObj = null;
let offsetX, offsetY;

function mousePressed() {
  for (let obj of objects) {
    if (dist(mouseX, mouseY, obj.x, obj.y) < obj.radius) {
      dragging = true;
      dragObj = obj;
      offsetX = mouseX - obj.x;
      offsetY = mouseY - obj.y;
      break;
    }
  }
}

function mouseDragged() {
  if (dragging && dragObj) {
    dragObj.x = mouseX - offsetX;
    dragObj.y = mouseY - offsetY;
  }
}

function mouseReleased() {
  dragging = false;
  dragObj = null;
}
```

**鼠标排斥（粒子逃离光标）：**
```javascript
function draw() {
  let mousePos = createVector(mouseX, mouseY);
  for (let p of particles) {
    let d = p.pos.dist(mousePos);
    if (d < 150) {
      let repel = p5.Vector.sub(p.pos, mousePos);
      repel.normalize();
      repel.mult(map(d, 0, 150, 5, 0));
      p.applyForce(repel);
    }
  }
}
```

## 键盘事件

### 状态

```javascript
keyIsPressed         // 布尔值
key                  // 最后按下的键，字符串形式（'a'、'A'、' '）
keyCode              // 数字代码（LEFT_ARROW、UP_ARROW 等）
```

### 事件回调

```javascript
function keyPressed() {
  // 按下时触发一次
  if (keyCode === LEFT_ARROW) { /* ... */ }
  if (key === 's') saveCanvas('output', 'png');
  if (key === ' ') CONFIG.paused = !CONFIG.paused;
  return false;  // 阻止浏览器默认行为
}

function keyReleased() {
  // 释放时触发一次
}

function keyTyped() {
  // 仅对可打印字符触发（不包括方向键、Shift 等）
}
```

### 持续按键状态（多键同时按下）

```javascript
let keys = {};

function keyPressed() { keys[keyCode] = true; }
function keyReleased() { keys[keyCode] = false; }

function draw() {
  if (keys[LEFT_ARROW]) player.x -= 5;
  if (keys[RIGHT_ARROW]) player.x += 5;
  if (keys[UP_ARROW]) player.y -= 5;
  if (keys[DOWN_ARROW]) player.y += 5;
}
```

### 按键常量

```
LEFT_ARROW, RIGHT_ARROW, UP_ARROW, DOWN_ARROW
BACKSPACE, DELETE, ENTER, RETURN, TAB, ESCAPE
SHIFT, CONTROL, OPTION, ALT
```

## 触摸事件

```javascript
touches   // 由 { x, y, id } 组成的数组——所有当前触摸点

function touchStarted() {
  // 首次触摸时触发
  return false;  // 阻止默认行为（在移动端可阻止滚动）
}

function touchMoved() {
  // 触摸拖动时触发
  return false;
}

function touchEnded() {
  // 触摸释放时触发
}
```

### 双指捏合缩放

```javascript
let prevDist = 0;
let zoomLevel = 1;

function touchMoved() {
  if (touches.length === 2) {
    let d = dist(touches[0].x, touches[0].y, touches[1].x, touches[1].y);
    if (prevDist > 0) {
      zoomLevel *= d / prevDist;
    }
    prevDist = d;
  }
  return false;
}

function touchEnded() {
  prevDist = 0;
}
```

## DOM 元素

### 创建控件

```javascript
function setup() {
  createCanvas(800, 800);

  // 滑块
  let slider = createSlider(0, 255, 100, 1);  // 最小值、最大值、默认值、步长
  slider.position(10, height + 10);
  slider.input(() => { CONFIG.value = slider.value(); });

  // 按钮
  let btn = createButton('Reset');
  btn.position(10, height + 40);
  btn.mousePressed(() => { resetSketch(); });

  // 复选框
  let check = createCheckbox('Show grid', false);
  check.position(10, height + 70);
  check.changed(() => { CONFIG.showGrid = check.checked(); });

  // 下拉选择
  let sel = createSelect();
  sel.position(10, height + 100);
  sel.option('Mode A');
  sel.option('Mode B');
  sel.changed(() => { CONFIG.mode = sel.value(); });

  // 取色器
  let picker = createColorPicker('#ff0000');
  picker.position(10, height + 130);
  picker.input(() => { CONFIG.color = picker.value(); });

  // 文本输入框
  let inp = createInput('Hello');
  inp.position(10, height + 160);
  inp.input(() => { CONFIG.text = inp.value(); });
}
```

### 给 DOM 元素添加样式

```javascript
let slider = createSlider(0, 100, 50);
slider.position(10, 10);
slider.style('width', '200px');
slider.class('my-slider');
slider.parent('controls-div');  // 挂载到指定 DOM 元素
```

## 音频输入（p5.sound）

需要 `p5.sound.min.js` 插件。

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/addons/p5.sound.min.js"></script>
```

### 麦克风输入

```javascript
let mic, fft, amplitude;

function setup() {
  createCanvas(800, 800);
  userStartAudio();  // 必需——需要用户手势才能启用音频

  mic = new p5.AudioIn();
  mic.start();

  fft = new p5.FFT(0.8, 256);  // 平滑系数、频段数
  fft.setInput(mic);

  amplitude = new p5.Amplitude();
  amplitude.setInput(mic);
}

function draw() {
  let level = amplitude.getLevel();    // 0.0 到 1.0（整体音量）
  let spectrum = fft.analyze();         // 由 256 个频率值组成的数组（0-255）
  let waveform = fft.waveform();        // 由 256 个时域采样值组成的数组（-1 到 1）

  // 获取各频段能量
  let bass = fft.getEnergy('bass');          // 20-140 Hz
  let lowMid = fft.getEnergy('lowMid');      // 140-400 Hz
  let mid = fft.getEnergy('mid');            // 400-2600 Hz
  let highMid = fft.getEnergy('highMid');    // 2600-5200 Hz
  let treble = fft.getEnergy('treble');      // 5200-14000 Hz
  // 每个返回值都在 0-255 之间
}
```

### 音频文件播放

```javascript
let song, fft;

function preload() {
  song = loadSound('track.mp3');
}

function setup() {
  createCanvas(800, 800);
  fft = new p5.FFT(0.8, 512);
  fft.setInput(song);
}

function mousePressed() {
  if (song.isPlaying()) {
    song.pause();
  } else {
    song.play();
  }
}
```

### 节拍检测（简单版）

```javascript
let prevBass = 0;
let beatThreshold = 30;
let beatCooldown = 0;

function detectBeat() {
  let bass = fft.getEnergy('bass');
  let isBeat = bass - prevBass > beatThreshold && beatCooldown <= 0;
  prevBass = bass;
  if (isBeat) beatCooldown = 10;  // 帧数
  beatCooldown--;
  return isBeat;
}
```

## 滚动驱动的动画

```javascript
let scrollProgress = 0;

function setup() {
  let canvas = createCanvas(windowWidth, windowHeight);
  canvas.style('position', 'fixed');
  // 让页面可滚动
  document.body.style.height = '500vh';
}

window.addEventListener('scroll', () => {
  let maxScroll = document.body.scrollHeight - window.innerHeight;
  scrollProgress = window.scrollY / maxScroll;
});

function draw() {
  background(0);
  // 用 scrollProgress（0 到 1）驱动动画
  let x = lerp(0, width, scrollProgress);
  ellipse(x, height/2, 50);
}
```

## 响应式事件

```javascript
function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  // 重建缓冲
  bgLayer = createGraphics(width, height);
  // 重新计算布局
  recalculateLayout();
}

// 可见性变化（切换标签页）
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    noLoop();  // 标签页不可见时暂停
  } else {
    loop();
  }
});
```
