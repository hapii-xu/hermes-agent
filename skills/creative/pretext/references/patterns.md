# Pretext 模式

针对最常见 pretext demo 形状的可复制粘贴片段。每个模式都是自包含的——在从 `https://esm.sh/@chenglou/pretext@0.0.6` 导入之后，直接丢进 HTML 的 `<script type="module">` 即可。

## 1. 围绕障碍物流动（变宽列）

pretext 的招牌动作。逐行询问「这里走廊有多宽？」，让 pretext 据此换行。

```js
const prepared = prepareWithSegments(TEXT, FONT);
const LINE_H = 24;

function drawFlow(ctx, obstacle /* {x,y,r} */, COL_X, COL_W, H) {
  let cursor = { segmentIndex: 0, graphemeIndex: 0 };
  let y = 72;
  while (y < H - 40) {
    const dy = y - obstacle.y;
    const inBand = Math.abs(dy) < obstacle.r;
    let x = COL_X, w = COL_W;
    if (inBand) {
      const half = Math.sqrt(obstacle.r ** 2 - dy ** 2);
      const leftW  = Math.max(0, (obstacle.x - half) - COL_X);
      const rightW = Math.max(0, (COL_X + COL_W) - (obstacle.x + half));
      if (leftW >= rightW) { x = COL_X;                 w = leftW  - 12; }
      else                 { x = obstacle.x + half + 12; w = rightW - 12; }
      if (w < 40) { y += LINE_H; continue; } // 宁可跳过，也不要挤压
    }
    const range = layoutNextLineRange(prepared, cursor, w);
    if (!range) break;
    const line = materializeLineRange(prepared, range);
    ctx.fillText(line.text, x, y);
    cursor = range.end;
    y += LINE_H;
  }
}
```

**障碍物变体：** 圆形（如上）、矩形（在该行片段上用 `Math.max(0, …)`）、多障碍物（排序片段并输出更宽的剩余车道）、动画障碍物（每帧重算——pretext 足够快）。

## 2. 文字即几何的游戏（带碰撞的单词砖块）

用 `layoutWithLines` 得到稳定的行矩形，然后把每个单词当作一个轴对齐的盒子来做物理。

```js
const prepared = prepareWithSegments(WORDS.join(" "), FONT);
const { lines } = layoutWithLines(prepared, FIELD_W, 28);

// 构建砖块矩形：按空格拆分每一行，逐词测量。
const bricks = [];
let y = 50;
for (const line of lines) {
  let x = 10;
  for (const word of line.text.split(" ")) {
    const wPx = ctx.measureText(word).width; // 或者按词用 walkLineRanges
    bricks.push({ x, y, w: wPx, h: 24, text: word, hp: 1 });
    x += wPx + ctx.measureText(" ").width;
  }
  y += 28;
}
```

碰撞：标准 AABB 对球。当 `hp` 掉到 0 时，砖块被「吃掉」。美学上：用 hp 淡化砖块不透明度，撞击时从字母拖出粒子轨迹。

## 3. 炸裂 / 爆炸式排版

用 `walkLineRanges` + 手动字形素遍历得到每个字形的 `(x, y)`，然后生成粒子。

```js
const prepared = prepareWithSegments(TEXT, FONT);
const particles = [];
let y = 100;
walkLineRanges(prepared, COL_W, (line) => {
  // 实例化以得到逐字形素位置
  const range = materializeLineRange(prepared, line);
  const seg = new Intl.Segmenter(undefined, { granularity: "grapheme" });
  let x = COL_X;
  for (const { segment } of seg.segment(range.text)) {
    const w = ctx.measureText(segment).width;
    particles.push({ ch: segment, x, y, vx: 0, vy: 0, homeX: x, homeY: y });
    x += w;
  }
  y += LINE_H;
});

// 点击时，把粒子从点击点向外踢；再缓动回 (homeX, homeY)。
canvas.addEventListener("click", (e) => {
  for (const p of particles) {
    const dx = p.x - e.clientX, dy = p.y - e.clientY;
    const d = Math.hypot(dx, dy) || 1;
    const force = 400 / (d * 0.2 + 1);
    p.vx += (dx / d) * force;
    p.vy += (dy / d) * force;
  }
});

function tick(dt) {
  for (const p of particles) {
    p.vx *= 0.92; p.vy *= 0.92;
    p.vx += (p.homeX - p.x) * 0.06;
    p.vy += (p.homeY - p.y) * 0.06;
    p.x += p.vx * dt; p.y += p.vy * dt;
  }
}
```

## 4. 作为移动物体的 ASCII 掩码

「酷炫 demo」的吸金模式：把一个 ASCII logo、精灵或位图栅格化进一个 cell 缓冲，然后把被占用的 cell 转成逐行的障碍物区间。Pretext 围绕这些区间排版段落，于是文字会真正地围绕移动的 ASCII 物体打开，而不是被视觉上覆盖上去。

完整实现见本技能里的 `templates/donut-orbit.html`。把它当作示例，而不是标准场景：它展示了如何从 ASCII logo 推导区间、如何把一个线框形状投影进障碍物行、如何在一个 DOM 层里保持文字可选，以及如何把调参控件藏在 `?dev` 后面。关键结构：

```js
const CELL_W = 12, CELL_H = 15;
const cols = Math.ceil(W / CELL_W), rows = Math.ceil(H / CELL_H);
const asciiMask = new Uint8Array(cols * rows);
const obstacleRows = Array.from({ length: rows }, () => []);

function rasterizeLogo(time) {
  asciiMask.fill(0);
  for (const r of obstacleRows) r.length = 0;

  for (const block of logoBlocks(time)) {
    const r0 = Math.floor(block.y0 / CELL_H);
    const r1 = Math.ceil(block.y1 / CELL_H);
    for (let r = r0; r <= r1; r++) {
      obstacleRows[r]?.push([block.x0 - 18, block.x1 + 22]);
      // 在此处填充 asciiMask 的 cell 以便绘制。
    }
  }

  mergeRowSpans(obstacleRows);
}

function drawParagraphs(prepared) {
  let cursor = { segmentIndex: 0, graphemeIndex: 0 };
  for (let y = yStart; y < yEnd; y += LINE_H) {
    const spans = obstacleRows[Math.floor(y / CELL_H)];
    for (const [x0, x1] of freeIntervalsAround(spans)) {
      const range = layoutNextLineRange(prepared, cursor, x1 - x0);
      if (!range) return;
      ctx.fillText(materializeLineRange(prepared, range).text, x0, y);
      cursor = range.end;
    }
  }
}
```

关键之处在于 ASCII 几何不是纯装饰。那些画 logo 或可拖拽物体的、正在移动的区间，同时也雕刻出传给 `layoutNextLineRange` 的行内区间。

### 测量的区间胜过魔法内边距

当一个 logo 或位图被栅格化进 cell 时，要测量每一行实际占用的 cell，然后加一个小的光晕。不要用一个巨大的包围盒。紧贴的测量区间会让文字读起来像是在绕着字母形状流动。

```js
const rowMin = new Float32Array(rows).fill(Infinity);
const rowMax = new Float32Array(rows).fill(-Infinity);

for (const cell of visibleCells) {
  rowMin[cell.row] = Math.min(rowMin[cell.row], cell.x);
  rowMax[cell.row] = Math.max(rowMax[cell.row], cell.x + CELL_W);
}

for (let row = 0; row < rows; row++) {
  if (!Number.isFinite(rowMin[row])) continue;
  obstacleRows[row].push([rowMin[row] - halo, rowMax[row] + halo]);
}
```

对于锐利的像素艺术字母，在推入区间前平滑相邻行。1-2 行的光晕通常能防止代码/正文碰到边角，又不丢失字母轮廓。

### 变形的形状需要变形的障碍物

如果可见物体在变形（球变立方、logo 变粒子等），碰撞场也要补间。一个有说服力的 demo 会为渲染缓冲和 pretext 障碍物行使用同一个 `mix` 值。

```js
function pushMorphedRows(aRows, bRows, mix) {
  for (let row = 0; row < rows; row++) {
    const a = aRows[row] ?? [centerX, centerX];
    const b = bRows[row] ?? [centerX, centerX];
    obstacleRows[row].push([
      a[0] + (b[0] - a[0]) * mix,
      a[1] + (b[1] - a[1]) * mix,
    ]);
  }
}
```

没有这个，艺术品可能在变形，而文字仍绕着旧形状换行，这就破坏了 pretext 效果。

### 把视觉层与碰撞分离

当视觉处理不应影响排版时，使用独立的 canvas。例如，在一个独立的 canvas 图层上用 CSS 不透明度淡出一个 ASCII 物体，但让它的障碍物行由显式的形状状态控制。淡化字形强度或缩放障碍物区间，往往看起来像物体在缩小而不是在淡出。

## 5. 共享游标的编辑式多栏

经典杂志排版：三栏，文字从第一栏末尾流进第二栏顶部，依此类推。Pretext 让这变得轻而易举，因为游标可以在 `layoutNextLineRange` 调用之间移植。

```js
const prepared = prepareWithSegments(ARTICLE, FONT);
let cursor = { segmentIndex: 0, graphemeIndex: 0 };

for (const col of [COL1, COL2, COL3]) {
  let y = col.y;
  while (y < col.y + col.h) {
    const range = layoutNextLineRange(prepared, cursor, col.w);
    if (!range) return;
    const line = materializeLineRange(prepared, range);
    ctx.fillText(line.text, col.x, y);
    cursor = range.end;
    y += LINE_H;
  }
}
```

把引言当作中间栏里的障碍物，并围绕它们使用模式 #1，就可以加入引言块。

## 6. 多行紧贴边界（最贴合的卡片）

给定一个最大宽度，找到仍能产生相同行数的**最小**容器宽度。适用于聊天气泡、引言卡片、工具提示尺寸。

```js
const prepared = prepareWithSegments(text, FONT);
const { lineCount, maxLineWidth } = measureLineStats(prepared, MAX_W);
// 卡片宽度 = maxLineWidth + 内边距；卡片高度 = lineCount * LINE_H + 内边距
```

如果要做一个*可视化*这个过程的 demo，可以在一秒内把卡片从 `MAX_W` 缩到 `maxLineWidth`——行数保持不变，但右边缘在向内拉。

## 7. 动态字体（kinetic typography）

随时间对逐行变换做动画。`layoutWithLines` 给你稳定的行；索引 `i` 驱动时序偏移。

```js
const { lines } = layoutWithLines(prepared, W - 80, 40);
function frame(t) {
  for (let i = 0; i < lines.length; i++) {
    const phase = t * 0.001 - i * 0.15;
    const y = 100 + i * 40 + Math.sin(phase) * 12;
    const opacity = 0.4 + 0.6 * Math.max(0, Math.sin(phase));
    ctx.globalAlpha = opacity;
    ctx.fillText(lines[i].text, 40, y);
  }
}
```

变体：星战爬行字幕（逐行透视倾斜）、波浪（正弦 y 偏移）、弹跳（缓入缓出到达）、故障（用 `Intl.Segmenter` 做逐字随机偏移）。

## 8. 字体栈模式

| 调性 | font 字符串 | 调色板提示 |
|------|-------------|--------------|
| 编辑式 / 严肃 | `17px/1.4 "Iowan Old Style", Georgia, serif` | 骨色 `#e8e6df` 配炭灰 `#0c0d10` |
| CRT / 终端 | `600 13px "JetBrains Mono", ui-monospace, monospace` | 琥珀 `hsl(38 60% 62%)` 配 `#07070a` |
| 人文 / 现代 | `500 17px Inter, ui-sans-serif, system-ui, sans-serif` | 灰白 `#f3efe6` 配深海军蓝 `#0b1020` |
| 展示 / 海报 | `700 64px "Playfair Display", serif` | 亮红 `#ff4130` 配奶油色 `#f0ebe0` |
| 工程 | `14px "IBM Plex Mono", monospace` | 霓虹绿 `#7cff7c` 配近黑 `#0a0a0c` |

始终显式加载 web 字体（Google Fonts link 标签或 `@font-face`），这样 canvas 测量才会与 CSS 渲染匹配。
