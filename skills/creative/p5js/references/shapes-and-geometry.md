# 形状与几何

## 2D 基础图元

```javascript
point(x, y);
line(x1, y1, x2, y2);
rect(x, y, w, h);            // 默认：角模式
rect(x, y, w, h, r);         // 圆角
rect(x, y, w, h, tl, tr, br, bl);  // 每个角单独设置半径
square(x, y, size);
ellipse(x, y, w, h);
circle(x, y, d);             // 直径，不是半径
triangle(x1, y1, x2, y2, x3, y3);
quad(x1, y1, x2, y2, x3, y3, x4, y4);
arc(x, y, w, h, start, stop, mode);  // 模式：OPEN、CHORD、PIE
```

### 绘制模式

```javascript
rectMode(CENTER);   // x,y 为中心（默认：CORNER）
rectMode(CORNERS);  // x1,y1 到 x2,y2
ellipseMode(CORNER); // x,y 为左上角
ellipseMode(CENTER); // 默认——x,y 为中心
```

## 描边与填充

```javascript
fill(r, g, b, a);    // 也可写作 fill(gray)、fill('#hex')，或在 HSB 模式下 fill(h, s, b)
noFill();
stroke(r, g, b, a);
noStroke();
strokeWeight(2);
strokeCap(ROUND);     // ROUND、SQUARE、PROJECT
strokeJoin(ROUND);    // ROUND、MITER、BEVEL
```

## 使用顶点构造自定义形状

### 基础顶点形状

```javascript
beginShape();
  vertex(100, 100);
  vertex(200, 50);
  vertex(300, 100);
  vertex(250, 200);
  vertex(150, 200);
endShape(CLOSE);  // CLOSE 会把最后一个顶点连回第一个
```

### 形状模式

```javascript
beginShape();          // 默认：连接所有顶点的多边形
beginShape(POINTS);    // 独立的点
beginShape(LINES);     // 两两顶点配对成线
beginShape(TRIANGLES); // 三个顶点一组构成三角形
beginShape(TRIANGLE_FAN);
beginShape(TRIANGLE_STRIP);
beginShape(QUADS);     // 四个一组
beginShape(QUAD_STRIP);
```

### 轮廓（在形状里挖洞）

```javascript
beginShape();
  // 外部形状
  vertex(100, 100);
  vertex(300, 100);
  vertex(300, 300);
  vertex(100, 300);
  // 内部洞
  beginContour();
    vertex(150, 150);
    vertex(150, 250);
    vertex(250, 250);
    vertex(250, 150);
  endContour();
endShape(CLOSE);
```

## 贝塞尔曲线（Bezier）

### 三次贝塞尔

```javascript
bezier(x1, y1, cx1, cy1, cx2, cy2, x2, y2);
// x1,y1 = 起点
// cx1,cy1 = 第一个控制点
// cx2,cy2 = 第二个控制点
// x2,y2 = 终点
```

### 在自定义形状中使用贝塞尔

```javascript
beginShape();
  vertex(100, 200);
  bezierVertex(150, 50, 250, 50, 300, 200);
  // 控制点1、控制点2、终点
endShape();
```

### 二次贝塞尔

```javascript
beginShape();
  vertex(100, 200);
  quadraticVertex(200, 50, 300, 200);
  // 单个控制点 + 终点
endShape();
```

### 沿贝塞尔插值

```javascript
let x = bezierPoint(x1, cx1, cx2, x2, t);  // t = 0..1
let y = bezierPoint(y1, cy1, cy2, y2, t);
let tx = bezierTangent(x1, cx1, cx2, x2, t); // 切线
```

## Catmull-Rom 样条

```javascript
curve(cpx1, cpy1, x1, y1, x2, y2, cpx2, cpy2);
// cpx1,cpy1 = 起点之前的控制点
// x1,y1 = 起点（可见）
// x2,y2 = 终点（可见）
// cpx2,cpy2 = 终点之后的控制点

curveVertex(x, y);  // 在 beginShape() 内使用——生成经过所有点的平滑曲线
curveTightness(0);  // 0 = Catmull-Rom，1 = 直线，-1 = 松散
```

### 经过各点的平滑曲线

```javascript
let points = [/* 由 {x, y} 组成的数组 */];
beginShape();
  curveVertex(points[0].x, points[0].y); // 重复第一个点以设置切线
  for (let p of points) {
    curveVertex(p.x, p.y);
  }
  curveVertex(points[points.length-1].x, points[points.length-1].y); // 重复最后一个点
endShape();
```

## p5.Vector

在物理、粒子系统和几何计算中不可或缺。

```javascript
let v = createVector(x, y);

// 算术运算（就地修改）
v.add(other);        // 向量加法
v.sub(other);        // 减法
v.mult(scalar);      // 缩放
v.div(scalar);       // 反向缩放
v.normalize();       // 单位向量（长度为 1）
v.limit(max);        // 限制最大长度
v.setMag(len);       // 设置精确长度

// 查询（非破坏性）
v.mag();             // 长度（模）
v.magSq();           // 长度的平方（更快，无 sqrt）
v.heading();         // 弧度制的角度
v.dist(other);       // 到另一个向量的距离
v.dot(other);        // 点积
v.cross(other);      // 叉积（3D）
v.angleBetween(other); // 两向量之间的夹角

// 静态方法（返回新向量）
p5.Vector.add(a, b);      // a + b → 新向量
p5.Vector.sub(a, b);      // a - b → 新向量
p5.Vector.fromAngle(a);   // 给定角度的单位向量
p5.Vector.random2D();     // 随机单位向量
p5.Vector.lerp(a, b, t);  // 插值

// 复制
let copy = v.copy();
```

## 有符号距离场（2D SDF）

SDF 返回某点到形状最近边缘的距离。内部为负，外部为正。常用于平滑形状、辉光效果和布尔运算。

```javascript
// 圆形 SDF
function sdCircle(px, py, cx, cy, r) {
  return dist(px, py, cx, cy) - r;
}

// 矩形 SDF
function sdBox(px, py, cx, cy, hw, hh) {
  let dx = abs(px - cx) - hw;
  let dy = abs(py - cy) - hh;
  return sqrt(max(dx, 0) ** 2 + max(dy, 0) ** 2) + min(max(dx, dy), 0);
}

// 线段 SDF
function sdSegment(px, py, ax, ay, bx, by) {
  let pa = createVector(px - ax, py - ay);
  let ba = createVector(bx - ax, by - ay);
  let t = constrain(pa.dot(ba) / ba.dot(ba), 0, 1);
  let closest = p5.Vector.add(createVector(ax, ay), p5.Vector.mult(ba, t));
  return dist(px, py, closest.x, closest.y);
}

// 平滑布尔并集
function opSmoothUnion(d1, d2, k) {
  let h = constrain(0.5 + 0.5 * (d2 - d1) / k, 0, 1);
  return lerp(d2, d1, h) - k * h * (1 - h);
}

// 把 SDF 渲染成辉光
let d = sdCircle(x, y, width/2, height/2, 200);
let glow = exp(-abs(d) * 0.02);  // 指数衰减
fill(glow * 255);
```

## 常用几何模式

### 正多边形

```javascript
function regularPolygon(cx, cy, r, sides) {
  beginShape();
  for (let i = 0; i < sides; i++) {
    let a = TWO_PI * i / sides - HALF_PI;
    vertex(cx + cos(a) * r, cy + sin(a) * r);
  }
  endShape(CLOSE);
}
```

### 星形

```javascript
function star(cx, cy, r1, r2, npoints) {
  beginShape();
  let angle = TWO_PI / npoints;
  let halfAngle = angle / 2;
  for (let a = -HALF_PI; a < TWO_PI - HALF_PI; a += angle) {
    vertex(cx + cos(a) * r2, cy + sin(a) * r2);
    vertex(cx + cos(a + halfAngle) * r1, cy + sin(a + halfAngle) * r1);
  }
  endShape(CLOSE);
}
```

### 圆头线段（胶囊形）

```javascript
function capsule(x1, y1, x2, y2, weight) {
  strokeWeight(weight);
  strokeCap(ROUND);
  line(x1, y1, x2, y2);
}
```

### 柔体 / 不规则团块（Blob）

```javascript
function blob(cx, cy, baseR, noiseScale, noiseOffset, detail = 64) {
  beginShape();
  for (let i = 0; i < detail; i++) {
    let a = TWO_PI * i / detail;
    let r = baseR + noise(cos(a) * noiseScale + noiseOffset,
                          sin(a) * noiseScale + noiseOffset) * baseR * 0.4;
    vertex(cx + cos(a) * r, cy + sin(a) * r);
  }
  endShape(CLOSE);
}
```

## 裁剪与遮罩

```javascript
// 裁剪形状——其后绘制的所有内容都会被裁剪形状遮罩
beginClip();
  circle(width/2, height/2, 400);
endClip();
// 只有圆内的内容可见
image(myImage, 0, 0);

// 或函数式写法
clip(() => {
  circle(width/2, height/2, 400);
});

// 擦除模式——打洞
erase();
  circle(mouseX, mouseY, 100);  // 此区域会变透明
noErase();
```
