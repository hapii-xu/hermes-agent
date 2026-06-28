# GLSL 参考

## Uniform 变量

```
TouchDesigner          GLSL
─────────────────────────────
vec0name = 'uTime'  →  uniform float uTime;
vec0valuex = 1.0    →  uTime 的值
```

### 传递时间

```python
glsl_op.par.vec0name = 'uTime'
glsl_op.par.vec0valuex.mode = ParMode.EXPRESSION
glsl_op.par.vec0valuex.expr = 'absTime.seconds'
```

```glsl
uniform float uTime;
void main() { float t = uTime * 0.5; }
```

### 内置 Uniform（TOP）

```glsl
// 输出分辨率（始终可用）
vec2 res = uTDOutputInfo.res.zw;

// 输入纹理（仅在连入了输入时可用）
vec2 inputRes = uTD2DInfos[0].res.zw;
vec4 color = texture(sTD2DInputs[0], vUV.st);

// UV 坐标
vUV.st  // 0-1 纹理坐标
```

**重要：** `uTD2DInfos` 需要输入纹理。对于独立着色器请使用 `uTDOutputInfo`。

## 内置工具函数

```glsl
// 噪声
float TDPerlinNoise(vec2/vec3/vec4 v);
float TDSimplexNoise(vec2/vec3/vec4 v);

// 颜色转换
vec3 TDHSVToRGB(vec3 c);
vec3 TDRGBToHSV(vec3 c);

// 矩阵变换
mat4 TDTranslate(float x, float y, float z);
mat3 TDRotateX/Y/Z(float radians);
mat3 TDRotateOnAxis(float radians, vec3 axis);
mat3 TDScale(float x, float y, float z);
mat3 TDRotateToVector(vec3 forward, vec3 up);
mat3 TDCreateRotMatrix(vec3 from, vec3 to);  // 向量必须归一化

// 分辨率结构体
struct TDTexInfo {
  vec4 res;   // (1/width, 1/height, width, height)
  vec4 depth;
};

// 输出（始终使用这个——能正确处理 sRGB）
fragColor = TDOutputSwizzle(color);

// 实例化（仅 MAT）
int TDInstanceID();
```

## glslTOP

自动创建的停靠 DAT：
- `glsl1_pixel` — 像素着色器
- `glsl1_compute` — 计算着色器
- `glsl1_info` — 编译信息

### 像素着色器模板

```glsl
out vec4 fragColor;
void main() {
    vec4 color = texture(sTD2DInputs[0], vUV.st);
    fragColor = TDOutputSwizzle(color);
}
```

### 计算着色器模板

```glsl
layout (local_size_x = 8, local_size_y = 8) in;
void main() {
    vec4 color = texelFetch(sTD2DInputs[0], ivec2(gl_GlobalInvocationID.xy), 0);
    TDImageStoreOutput(0, gl_GlobalInvocationID, color);
}
```

### 更新着色器

```python
op('/project1/glsl1_pixel').text = shader_code
op('/project1/glsl1').cook(force=True)
# 检查错误：
print(op('/project1/glsl1_info').text)
```

## glslMAT

停靠 DAT：
- `glslmat1_vertex` — 顶点着色器（参数：`vdat`）
- `glslmat1_pixel` — 像素着色器（参数：`pdat`）
- `glslmat1_info` — 编译信息

注意：MAT 使用 `vdat`/`pdat`，TOP 使用 `vertexdat`/`pixeldat`。

### 顶点着色器模板

```glsl
uniform float uTime;
void main() {
    vec3 pos = TDPos();
    pos.z += sin(pos.x * 3.0 + uTime) * 0.2;
    vec4 worldSpacePos = TDDeform(pos);
    gl_Position = TDWorldToProj(worldSpacePos);
}
```

## Bayer 8x8 抖动矩阵

可复用的有序抖动函数，用于复古/印刷风格：

```glsl
float bayer8(vec2 pos) {
    int x = int(mod(pos.x, 8.0)), y = int(mod(pos.y, 8.0)), idx = x + y * 8;
    int b[64] = int[64](
        0,32,8,40,2,34,10,42,48,16,56,24,50,18,58,26,
        12,44,4,36,14,46,6,38,60,28,52,20,62,30,54,22,
        3,35,11,43,1,33,9,41,51,19,59,27,49,17,57,25,
        15,47,7,39,13,45,5,37,63,31,55,23,61,29,53,21
    );
    return float(b[idx]) / 64.0;
}
```

## glslPOP / glsladvancedPOP / glslcopyPOP

全部使用计算着色器。停靠 DAT 遵循命名约定：
- `glsl1_compute` / `glsladv1_compute`
- `glslcopy1_ptCompute` / `glslcopy1_vertCompute` / `glslcopy1_primCompute`
