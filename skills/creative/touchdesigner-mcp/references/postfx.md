# 后期特效参考

用于现场视觉作品的泛光（Bloom）、CRT 扫描线、色差（chromatic aberration）以及 feedback 辉光模式。

---

## 泛光（Bloom）

### 内置 Bloom TOP

TD 的 `bloomTOP` 是最快的方案——GPU 加速、无需着色器。

```python
bloom = root.create(bloomTOP, 'bloom1')
bloom.par.threshold = 0.6     # 亮度阈值（0-1）
bloom.par.size = 0.03         # 扩散半径（0-1）
bloom.par.strength = 1.5      # 泛光强度
bloom.par.blendmode = 'add'   # 'add' 或 'screen'
```

**音频反应式泛光：**
```python
bloom.par.strength.mode = ParMode.EXPRESSION
bloom.par.strength.expr = "op('audio_env')['envelope'][0] * 3.0 + 0.5"
```

### GLSL 泛光（更多控制）

用于带色彩着色的多通道泛光：

```glsl
// bloom_pixel.glsl — pass1：阈值 + 着色
out vec4 fragColor;
uniform float uThreshold;
uniform vec3 uBloomColor;

void main() {
    vec4 col = texture(sTD2DInputs[0], vUV.st);
    float luma = dot(col.rgb, vec3(0.299, 0.587, 0.114));
    float bloom = max(0.0, luma - uThreshold);
    fragColor = TDOutputSwizzle(vec4(col.rgb * bloom * uBloomColor, col.a));
}
```

然后用 `blurTOP` 模糊（size 约 0.02-0.05），再通过 `addTOP` 或处于 Add 模式的 `compositeTOP` 叠加回源画面。

---

## CRT / 扫描线

纯 GLSL 实现——创建一个 `glslTOP` 并粘贴到其 `_pixel` DAT 中。

```glsl
// crt_pixel.glsl
out vec4 fragColor;
uniform float uTime;
uniform float uScanlineIntensity;  // 0.0 - 1.0，默认 0.4
uniform float uCurvature;          // 0.0 - 0.15，默认 0.05
uniform float uVignette;           // 0.0 - 1.0，默认 0.8

vec2 curveUV(vec2 uv, float amount) {
    uv = uv * 2.0 - 1.0;
    vec2 offset = abs(uv.yx) / vec2(6.0, 4.0);
    uv = uv + uv * offset * offset * amount;
    return uv * 0.5 + 0.5;
}

void main() {
    vec2 res = uTDOutputInfo.res.zw;
    vec2 uv = vUV.st;

    // CRT 桶形畸变
    uv = curveUV(uv, uCurvature * 10.0);

    // 剔除弯曲屏幕外的像素
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        fragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }

    vec4 col = texture(sTD2DInputs[0], uv);

    // 扫描线
    float scanline = sin(uv.y * res.y * 3.14159) * 0.5 + 0.5;
    col.rgb *= mix(1.0, scanline, uScanlineIntensity);

    // 水平噪声闪烁
    float flicker = TDSimplexNoise(vec2(uv.y * 100.0, uTime * 8.0)) * 0.03;
    col.rgb += flicker;

    // 暗角
    vec2 vig = uv * (1.0 - uv.yx);
    float v = pow(vig.x * vig.y * 15.0, uVignette);
    col.rgb *= v;

    fragColor = TDOutputSwizzle(col);
}
```

---

## 色差（Chromatic Aberration）

将 RGB 通道分离并沿屏幕轴向偏移。

```glsl
out vec4 fragColor;
uniform float uAmount;   // 0.001 - 0.02，默认 0.006

void main() {
    vec2 uv = vUV.st;
    vec2 dir = uv - 0.5;

    float r = texture(sTD2DInputs[0], uv + dir * uAmount).r;
    float g = texture(sTD2DInputs[0], uv).g;
    float b = texture(sTD2DInputs[0], uv - dir * uAmount).b;
    float a = texture(sTD2DInputs[0], uv).a;

    fragColor = TDOutputSwizzle(vec4(r, g, b, a));
}
```

**音频反应式变体** — 在节拍时增强色差：
```glsl
uniform float uBeat;
void main() {
    vec2 uv = vUV.st;
    vec2 dir = uv - 0.5;
    float amount = uAmount + uBeat * 0.04;
    float r = texture(sTD2DInputs[0], uv + dir * amount * 1.2).r;
    float g = texture(sTD2DInputs[0], uv).g;
    float b = texture(sTD2DInputs[0], uv - dir * amount * 0.8).b;
    fragColor = TDOutputSwizzle(vec4(r, g, b, 1.0));
}
```

---

## Feedback 辉光

用于辉光效果的温暖持久拖尾。

```glsl
out vec4 fragColor;
uniform float uDecay;     // 0.92 - 0.98 用于慢速拖尾
uniform vec3 uGlowColor;  // 为累积的 feedback 着色

void main() {
    vec2 uv = vUV.st;
    vec4 prev = texture(sTD2DInputs[0], uv);  // feedback 输入
    vec4 curr = texture(sTD2DInputs[1], uv);  // 当前帧

    vec3 glow = prev.rgb * uDecay * uGlowColor;
    vec3 result = max(glow, curr.rgb);

    fragColor = TDOutputSwizzle(vec4(result, 1.0));
}
```

**提示：**
- `uDecay = 0.95` → 中等长度拖尾
- `uDecay = 0.98` → 长彗星尾
- 将 `glslTOP` 格式设为 `rgba16float` 以获得平滑渐变

---

## 完整后期特效堆栈

推荐顺序：

```
[场景 / 合成画面]
        ↓
   bloomTOP          ← 亮度阈值泛光
        ↓
   glslTOP (chrom)   ← 色差
        ↓
   glslTOP (crt)     ← 扫描线 + 桶形畸变 + 暗角
        ↓
   null_out          ← 最终输出
```

**性能提示：** 每个 glslTOP 是一次完整的 GPU pass。对于 1920×1080 @ 60fps，这个堆栈完全能实时。对于 4K，考虑先用 `resolutionTOP` 对 bloom 的输入做降采样。
