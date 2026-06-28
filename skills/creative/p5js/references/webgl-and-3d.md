# WebGL 与 3D

## WebGL 模式设置

```javascript
function setup() {
  createCanvas(1920, 1080, WEBGL);
  // 原点在中心，不是左上角
  // Y 轴朝上（与 2D 模式相反）
  // Z 轴朝向观看者
}
```

### 坐标转换（WEBGL 转为类 P2D）

```javascript
function draw() {
  translate(-width/2, -height/2);  // 把原点移到左上角
  // 此后坐标与 P2D 一致
}
```

## 3D 图元

```javascript
box(w, h, d);             // 长方体
sphere(radius, detailX, detailY);
cylinder(radius, height, detailX, detailY);
cone(radius, height, detailX, detailY);
torus(radius, tubeRadius, detailX, detailY);
plane(width, height);     // 平面矩形
ellipsoid(rx, ry, rz);    // 拉伸的球体
```

### 3D 变换

```javascript
push();
  translate(x, y, z);
  rotateX(angleX);
  rotateY(angleY);
  rotateZ(angleZ);
  scale(s);
  box(100);
pop();
```

## 相机

### 默认相机

```javascript
camera(
  eyeX, eyeY, eyeZ,       // 相机位置
  centerX, centerY, centerZ, // 注视目标
  upX, upY, upZ             // 朝上方向
);

// 默认：camera(0, 0, (height/2)/tan(PI/6), 0, 0, 0, 0, 1, 0)
```

### 轨道控制

```javascript
function draw() {
  orbitControl();  // 鼠标拖拽旋转，滚动缩放
  box(200);
}
```

### createCamera

```javascript
let cam;

function setup() {
  createCanvas(800, 800, WEBGL);
  cam = createCamera();
  cam.setPosition(300, -200, 500);
  cam.lookAt(0, 0, 0);
}

// 相机方法
cam.setPosition(x, y, z);
cam.lookAt(x, y, z);
cam.move(dx, dy, dz);      // 相对相机朝向移动
cam.pan(angle);              // 水平旋转
cam.tilt(angle);             // 垂直旋转
cam.roll(angle);             // z 轴旋转
cam.slerp(otherCam, t);     // 相机之间的平滑插值
```

### 透视与正交

```javascript
// 透视（默认）
perspective(fov, aspect, near, far);
// fov：视野弧度（默认 PI/3）
// aspect：宽/高
// near/far：裁剪平面

// 正交（无深度透视收缩）
ortho(-width/2, width/2, -height/2, height/2, 0, 2000);
```

## 光照

```javascript
// 环境光（均匀、无方向）
ambientLight(50, 50, 50);     // 暗淡补光

// 平行光（平行光线，如太阳）
directionalLight(255, 255, 255, 0, -1, 0);  // 颜色 + 方向

// 点光源（从位置辐射）
pointLight(255, 200, 150, 200, -300, 400);   // 颜色 + 位置

// 聚光灯（从位置朝目标的锥形）
spotLight(255, 255, 255,       // 颜色
          0, -300, 300,         // 位置
          0, 1, -1,             // 方向
          PI / 4, 5);           // 角度，聚光度

// 基于图像的光照
imageLight(myHDRI);

// 无光照（平面着色）
noLights();

// 快速默认光照
lights();
```

### 三点照明设置

```javascript
function setupLighting() {
  ambientLight(30, 30, 40);                    // 暗淡蓝色补光

  // 主光（主要，暖色）
  directionalLight(255, 240, 220, -1, -1, -1);

  // 补光（更柔、更冷，在对侧）
  directionalLight(80, 100, 140, 1, -0.5, -1);

  // 轮廓光（在主体后方，用于勾勒边缘）
  pointLight(200, 200, 255, 0, -200, -400);
}
```

## 材质

```javascript
// 法线材质（调试——颜色来自表面法线）
normalMaterial();

// 环境材质（只响应 ambientLight）
ambientMaterial(200, 100, 100);

// 自发光材质（自发光，无阴影）
emissiveMaterial(255, 0, 100);

// 镜面材质（闪亮反射）
specularMaterial(255);
shininess(50);                // 1-200（越高高光越紧）
metalness(100);               // 0-200（金属反射）

// fill 也有效（不响应光照）
fill(255, 0, 0);
```

### 纹理

```javascript
let img;
function preload() { img = loadImage('texture.jpg'); }

function draw() {
  texture(img);
  textureMode(NORMAL);  // UV 坐标 0-1
  // textureMode(IMAGE); // UV 坐标用像素
  textureWrap(REPEAT);  // 或 CLAMP、MIRROR
  box(200);
}
```

## 自定义几何体

### buildGeometry

```javascript
let myShape;

function setup() {
  createCanvas(800, 800, WEBGL);
  myShape = buildGeometry(() => {
    for (let i = 0; i < 50; i++) {
      push();
      translate(random(-200, 200), random(-200, 200), random(-200, 200));
      sphere(10);
      pop();
    }
  });
}

function draw() {
  model(myShape);  // 高效渲染一次性构建的几何体
}
```

### beginGeometry / endGeometry

```javascript
beginGeometry();
  // 在这里绘制形状
  box(50);
  translate(100, 0, 0);
  sphere(30);
let geo = endGeometry();

model(geo);  // 复用
```

### 手动几何体（p5.Geometry）

```javascript
let geo = new p5.Geometry(detailX, detailY, function() {
  for (let i = 0; i <= detailX; i++) {
    for (let j = 0; j <= detailY; j++) {
      let u = i / detailX;
      let v = j / detailY;
      let x = cos(u * TWO_PI) * (100 + 30 * cos(v * TWO_PI));
      let y = sin(u * TWO_PI) * (100 + 30 * cos(v * TWO_PI));
      let z = 30 * sin(v * TWO_PI);
      this.vertices.push(createVector(x, y, z));
      this.uvs.push(u, v);
    }
  }
  this.computeFaces();
  this.computeNormals();
});
```

## GLSL 着色器

### createShader（顶点 + 片段）

```javascript
let myShader;

function setup() {
  createCanvas(800, 800, WEBGL);

  let vert = `
    precision mediump float;
    attribute vec3 aPosition;
    attribute vec2 aTexCoord;
    varying vec2 vTexCoord;
    uniform mat4 uModelViewMatrix;
    uniform mat4 uProjectionMatrix;
    void main() {
      vTexCoord = aTexCoord;
      vec4 pos = uProjectionMatrix * uModelViewMatrix * vec4(aPosition, 1.0);
      gl_Position = pos;
    }
  `;

  let frag = `
    precision mediump float;
    varying vec2 vTexCoord;
    uniform float uTime;
    uniform vec2 uResolution;

    void main() {
      vec2 uv = vTexCoord;
      vec3 col = 0.5 + 0.5 * cos(uTime + uv.xyx + vec3(0, 2, 4));
      gl_FragColor = vec4(col, 1.0);
    }
  `;

  myShader = createShader(vert, frag);
}

function draw() {
  shader(myShader);
  myShader.setUniform('uTime', millis() / 1000.0);
  myShader.setUniform('uResolution', [width, height]);
  rect(0, 0, width, height);
  resetShader();
}
```

### createFilterShader（后期处理）

更简单——只需要片段着色器。会自动把画布作为纹理传入。

```javascript
let blurShader;

function setup() {
  createCanvas(800, 800, WEBGL);

  blurShader = createFilterShader(`
    precision mediump float;
    varying vec2 vTexCoord;
    uniform sampler2D tex0;
    uniform vec2 texelSize;

    void main() {
      vec4 sum = vec4(0.0);
      for (int x = -2; x <= 2; x++) {
        for (int y = -2; y <= 2; y++) {
          sum += texture2D(tex0, vTexCoord + vec2(float(x), float(y)) * texelSize);
        }
      }
      gl_FragColor = sum / 25.0;
    }
  `);
}

function draw() {
  // 正常绘制场景
  background(0);
  fill(255, 0, 0);
  sphere(100);

  // 应用后期处理滤镜
  filter(blurShader);
}
```

### 常见着色器 uniform

```javascript
myShader.setUniform('uTime', millis() / 1000.0);
myShader.setUniform('uResolution', [width, height]);
myShader.setUniform('uMouse', [mouseX / width, mouseY / height]);
myShader.setUniform('uTexture', myGraphics);  // 把 p5.Graphics 作为纹理传入
myShader.setUniform('uValue', 0.5);           // 浮点
myShader.setUniform('uColor', [1.0, 0.0, 0.5, 1.0]); // vec4
```

### 着色器配方

**色差：**
```glsl
vec4 r = texture2D(tex0, vTexCoord + vec2(0.005, 0.0));
vec4 g = texture2D(tex0, vTexCoord);
vec4 b = texture2D(tex0, vTexCoord - vec2(0.005, 0.0));
gl_FragColor = vec4(r.r, g.g, b.b, 1.0);
```

**暗角：**
```glsl
float d = distance(vTexCoord, vec2(0.5));
float v = smoothstep(0.7, 0.4, d);
gl_FragColor = texture2D(tex0, vTexCoord) * v;
```

**扫描线：**
```glsl
float scanline = sin(vTexCoord.y * uResolution.y * 3.14159) * 0.04;
vec4 col = texture2D(tex0, vTexCoord);
gl_FragColor = col - scanline;
```

## 帧缓冲

```javascript
let fbo;

function setup() {
  createCanvas(800, 800, WEBGL);
  fbo = createFramebuffer();
}

function draw() {
  // 渲染到帧缓冲
  fbo.begin();
  clear();
  rotateY(frameCount * 0.01);
  box(200);
  fbo.end();

  // 把帧缓冲作为纹理使用
  texture(fbo.color);
  plane(width, height);
}
```

### 多通道渲染

```javascript
let sceneBuffer, blurBuffer;

function setup() {
  createCanvas(800, 800, WEBGL);
  sceneBuffer = createFramebuffer();
  blurBuffer = createFramebuffer();
}

function draw() {
  // 通道 1：渲染场景
  sceneBuffer.begin();
  clear();
  lights();
  rotateY(frameCount * 0.01);
  box(200);
  sceneBuffer.end();

  // 通道 2：模糊
  blurBuffer.begin();
  shader(blurShader);
  blurShader.setUniform('uTexture', sceneBuffer.color);
  rect(0, 0, width, height);
  resetShader();
  blurBuffer.end();

  // 最终：合成
  texture(blurBuffer.color);
  plane(width, height);
}
```
