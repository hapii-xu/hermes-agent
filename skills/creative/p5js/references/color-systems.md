# 色彩系统

## 色彩模式

### HSB（生成艺术推荐）

```javascript
colorMode(HSB, 360, 100, 100, 100);
// 色相：0-360（色轮位置）
// 饱和度：0-100（灰到鲜艳）
// 亮度：0-100（黑到满）
// Alpha：0-100

fill(200, 80, 90);        // 蓝色，鲜艳，明亮
fill(200, 80, 90, 50);    // 50% 透明
```

HSB 的优势：
- 旋转色相：`(baseHue + offset) % 360`
- 降低饱和度：减小 S
- 变暗：减小 B
- 单色变体：固定 H，调整 S 和 B
- 互补色：`(hue + 180) % 360`
- 邻近色：`hue +/- 30`

### HSL

```javascript
colorMode(HSL, 360, 100, 100, 100);
// 亮度 50 = 纯色，0 = 黑，100 = 白
// 对浅色（L > 50）和深色（L < 50）更直观
```

### RGB

```javascript
colorMode(RGB, 255, 255, 255, 255);  // 默认
// 直接通道控制，对程序化调色板不够直观
```

## 颜色对象

```javascript
let c = color(200, 80, 90);    // 创建颜色对象
fill(c);

// 提取分量
let h = hue(c);
let s = saturation(c);
let b = brightness(c);
let r = red(c);
let g = green(c);
let bl = blue(c);
let a = alpha(c);

// 十六进制颜色随处可用
fill('#e8d5b7');
fill('#e8d5b7cc');  // 带 alpha

// 通过 setter 修改
c.setAlpha(128);
c.setRed(200);
```

## 颜色插值

### lerpColor

```javascript
let c1 = color(0, 80, 100);    // 红
let c2 = color(200, 80, 100);  // 蓝
let mixed = lerpColor(c1, c2, 0.5);  // 中点混合
// 在当前 colorMode 下工作
```

### paletteLerp（p5.js 1.11+）

一次在多个颜色之间插值。

```javascript
let colors = [
  color('#2E0854'),
  color('#850E35'),
  color('#EE6C4D'),
  color('#F5E663')
];
let c = paletteLerp(colors, t);  // t = 0..1，在所有颜色间插值
```

### 手动多停靠点渐变

```javascript
function multiLerp(colors, t) {
  t = constrain(t, 0, 1);
  let segment = t * (colors.length - 1);
  let idx = floor(segment);
  let frac = segment - idx;
  idx = min(idx, colors.length - 2);
  return lerpColor(colors[idx], colors[idx + 1], frac);
}
```

## 渐变渲染

### 线性渐变

```javascript
function linearGradient(x1, y1, x2, y2, c1, c2) {
  let steps = dist(x1, y1, x2, y2);
  for (let i = 0; i <= steps; i++) {
    let t = i / steps;
    let c = lerpColor(c1, c2, t);
    stroke(c);
    let x = lerp(x1, x2, t);
    let y = lerp(y1, y2, t);
    // 在每个点绘制垂直线
    let dx = -(y2 - y1) / steps * 1000;
    let dy = (x2 - x1) / steps * 1000;
    line(x - dx, y - dy, x + dx, y + dy);
  }
}
```

### 径向渐变

```javascript
function radialGradient(cx, cy, r, innerColor, outerColor) {
  noStroke();
  for (let i = r; i > 0; i--) {
    let t = 1 - i / r;
    fill(lerpColor(innerColor, outerColor, t));
    ellipse(cx, cy, i * 2);
  }
}
```

### 噪声渐变

```javascript
function noiseGradient(colors, noiseScale, time) {
  loadPixels();
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let n = noise(x * noiseScale, y * noiseScale, time);
      let c = multiLerp(colors, n);
      let idx = 4 * (y * width + x);
      pixels[idx] = red(c);
      pixels[idx+1] = green(c);
      pixels[idx+2] = blue(c);
      pixels[idx+3] = 255;
    }
  }
  updatePixels();
}
```

## 程序化调色板生成

### 互补色

```javascript
function complementary(baseHue) {
  return [baseHue, (baseHue + 180) % 360];
}
```

### 邻近色

```javascript
function analogous(baseHue, spread = 30) {
  return [
    (baseHue - spread + 360) % 360,
    baseHue,
    (baseHue + spread) % 360
  ];
}
```

### 三元色

```javascript
function triadic(baseHue) {
  return [baseHue, (baseHue + 120) % 360, (baseHue + 240) % 360];
}
```

### 分裂互补

```javascript
function splitComplementary(baseHue) {
  return [baseHue, (baseHue + 150) % 360, (baseHue + 210) % 360];
}
```

### 四元色（矩形）

```javascript
function tetradic(baseHue) {
  return [baseHue, (baseHue + 60) % 360, (baseHue + 180) % 360, (baseHue + 240) % 360];
}
```

### 单色变体

```javascript
function monoVariations(hue, count = 5) {
  let colors = [];
  for (let i = 0; i < count; i++) {
    let s = map(i, 0, count - 1, 20, 90);
    let b = map(i, 0, count - 1, 95, 40);
    colors.push(color(hue, s, b));
  }
  return colors;
}
```

## 精选调色板库

### 暖色调色板

```javascript
const SUNSET = ['#2E0854', '#850E35', '#EE6C4D', '#F5E663'];
const EMBER  = ['#1a0000', '#4a0000', '#8b2500', '#cd5c00', '#ffd700'];
const PEACH  = ['#fff5eb', '#ffdab9', '#ff9a76', '#ff6b6b', '#c94c4c'];
const COPPER = ['#1c1108', '#3d2b1f', '#7b4b2a', '#b87333', '#daa06d'];
```

### 冷色调色板

```javascript
const OCEAN   = ['#0a0e27', '#1a1b4b', '#2a4a7f', '#3d7cb8', '#87ceeb'];
const ARCTIC  = ['#0d1b2a', '#1b263b', '#415a77', '#778da9', '#e0e1dd'];
const FOREST  = ['#0b1a0b', '#1a3a1a', '#2d5a2d', '#4a8c4a', '#90c990'];
const DEEP_SEA = ['#000814', '#001d3d', '#003566', '#006d77', '#83c5be'];
```

### 中性调色板

```javascript
const GRAPHITE = ['#1a1a1a', '#333333', '#555555', '#888888', '#cccccc'];
const CREAM    = ['#f4f0e8', '#e8dcc8', '#c9b99a', '#a89070', '#7a6450'];
const SLATE    = ['#1e293b', '#334155', '#475569', '#64748b', '#94a3b8'];
```

### 鲜艳调色板

```javascript
const NEON     = ['#ff00ff', '#00ffff', '#ff0080', '#80ff00', '#0080ff'];
const RAINBOW  = ['#ff0000', '#ff8000', '#ffff00', '#00ff00', '#0000ff', '#8000ff'];
const VAPOR    = ['#ff71ce', '#01cdfe', '#05ffa1', '#b967ff', '#fffb96'];
const CYBER    = ['#0f0f0f', '#00ff41', '#ff0090', '#00d4ff', '#ffd000'];
```

### 大地色调

```javascript
const TERRA    = ['#2c1810', '#5c3a2a', '#8b6b4a', '#c4a672', '#e8d5b7'];
const MOSS     = ['#1a1f16', '#3d4a2e', '#6b7c4f', '#9aab7a', '#c8d4a9'];
const CLAY     = ['#3b2f2f', '#6b4c4c', '#9e7676', '#c9a0a0', '#e8caca'];
```

## 混合模式

```javascript
blendMode(BLEND);       // 默认——alpha 合成
blendMode(ADD);         // 相加——明亮辉光效果
blendMode(MULTIPLY);    // 变暗——阴影、纹理叠加
blendMode(SCREEN);      // 变亮——柔和辉光
blendMode(OVERLAY);     // 对比度增强——高/低端强调
blendMode(DIFFERENCE);  // 颜色相减——迷幻
blendMode(EXCLUSION);   // 较柔和的差值
blendMode(REPLACE);     // 覆盖（无 alpha 混合）
blendMode(REMOVE);      // 减去 alpha
blendMode(LIGHTEST);    // 保留较亮像素
blendMode(DARKEST);     // 保留较暗像素
blendMode(BURN);        // 加深 + 提饱和
blendMode(DODGE);       // 提亮 + 提饱和
blendMode(SOFT_LIGHT);  // 微妙叠加
blendMode(HARD_LIGHT);  // 强烈叠加

// 用完务必重置
blendMode(BLEND);
```

### 混合模式配方

| 效果 | 模式 | 用途 |
|--------|------|----------|
| 相加辉光 | `ADD` | 光束、火焰、粒子 |
| 阴影叠加 | `MULTIPLY` | 纹理、暗角 |
| 柔光混合 | `SCREEN` | 雾、薄雾、背光 |
| 高对比 | `OVERLAY` | 戏剧化合成 |
| 颜色负片 | `DIFFERENCE` | 故障、迷幻 |
| 图层合成 | `BLEND` | 标准 alpha 叠层 |

## 背景技法

### 纹理背景

```javascript
function texturedBackground(baseColor, noiseScale, noiseAmount) {
  loadPixels();
  let r = red(baseColor), g = green(baseColor), b = blue(baseColor);
  for (let i = 0; i < pixels.length; i += 4) {
    let x = (i / 4) % width;
    let y = floor((i / 4) / width);
    let n = (noise(x * noiseScale, y * noiseScale) - 0.5) * noiseAmount;
    pixels[i] = constrain(r + n, 0, 255);
    pixels[i+1] = constrain(g + n, 0, 255);
    pixels[i+2] = constrain(b + n, 0, 255);
    pixels[i+3] = 255;
  }
  updatePixels();
}
```

### 暗角

```javascript
function vignette(strength = 0.5, radius = 0.7) {
  loadPixels();
  let cx = width / 2, cy = height / 2;
  let maxDist = dist(0, 0, cx, cy);
  for (let i = 0; i < pixels.length; i += 4) {
    let x = (i / 4) % width;
    let y = floor((i / 4) / width);
    let d = dist(x, y, cx, cy) / maxDist;
    let factor = 1.0 - smoothstep(constrain((d - radius) / (1 - radius), 0, 1)) * strength;
    pixels[i] *= factor;
    pixels[i+1] *= factor;
    pixels[i+2] *= factor;
  }
  updatePixels();
}

function smoothstep(t) { return t * t * (3 - 2 * t); }
```

### 胶片颗粒

```javascript
function filmGrain(amount = 30) {
  loadPixels();
  for (let i = 0; i < pixels.length; i += 4) {
    let grain = random(-amount, amount);
    pixels[i] = constrain(pixels[i] + grain, 0, 255);
    pixels[i+1] = constrain(pixels[i+1] + grain, 0, 255);
    pixels[i+2] = constrain(pixels[i+2] + grain, 0, 255);
  }
  updatePixels();
}
```
