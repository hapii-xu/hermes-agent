# 布局合成器参考

用于构建模块化多面板网格的模式——适用于 HUD 界面、数据看板以及多源视觉合成。

## 布局方案

| 方案 | 最适用于 | 说明 |
|----------|----------|-------|
| `layoutTOP` | 固定网格、快速搭建 | GPU、简单平铺 |
| Container COMP + `overTOP` | 完全控制、混合尺寸面板 | 设置更多、非常灵活 |
| GLSL 合成器 | 程序化 / BSP 风格 | 最强大、更复杂 |

---

## layoutTOP

内置的网格合成器——构建均匀瓦片网格的最快路径。

```python
layout = root.create(layoutTOP, 'layout1')
layout.par.resolutionw = 1920
layout.par.resolutionh = 1080
layout.par.cols = 3
layout.par.rows = 2
layout.par.gap = 4
```

连接输入（最多 cols×rows 个）：
```python
layout.inputConnectors[0].connect(op('panel_radar'))
layout.inputConnectors[1].connect(op('panel_wave'))
layout.inputConnectors[2].connect(op('panel_data'))
```

**变宽列：** 不直接支持。对于非均匀网格请使用 overTOP 方案。

---

## Container COMP 网格

将每个元素构建为独立的 `containerCOMP`。用 `overTOP` 合成：

```python
def create_panel(root, name, width, height, x=0, y=0):
    panel = root.create(containerCOMP, name)
    panel.par.w = width
    panel.par.h = height
    panel.viewer = True
    return panel

# 用 overTOP 链合成
over1 = root.create(overTOP, 'over1')
over1.inputConnectors[0].connect(panel_radar)
over1.inputConnectors[1].connect(panel_wave)
over1.par.topx2 = 0
over1.par.topy2 = 512
```

**提示：** 如果面板尺寸不同，在每个 `overTOP` 输入前使用 `resolutionTOP`。

---

## 面板分隔线（GLSL）

```glsl
out vec4 fragColor;
uniform vec2 uGridDivisions;   // 例如 vec2(3, 2) 表示 3 列、2 行
uniform float uLineWidth;      // 像素
uniform vec4 uLineColor;       // 例如 vec4(0.0, 1.0, 0.8, 0.6) 表示青色

void main() {
    vec2 res = uTDOutputInfo.res.zw;
    vec2 uv = vUV.st;
    vec4 bg = texture(sTD2DInputs[0], uv);

    float lineW = uLineWidth / res.x;
    float lineH = uLineWidth / res.y;

    float vDiv = 0.0;
    for (float i = 1.0; i < uGridDivisions.x; i++) {
        float x = i / uGridDivisions.x;
        vDiv = max(vDiv, step(abs(uv.x - x), lineW));
    }

    float hDiv = 0.0;
    for (float i = 1.0; i < uGridDivisions.y; i++) {
        float y = i / uGridDivisions.y;
        hDiv = max(hDiv, step(abs(uv.y - y), lineH));
    }

    float line = max(vDiv, hDiv);
    vec4 result = mix(bg, uLineColor, line * uLineColor.a);
    fragColor = TDOutputSwizzle(result);
}
```

---

## 元件库模式

每个视觉元件都作为可复用的 `.tox` 存放在自己的 `baseCOMP` 中：

### 标准接口
```
inputs:
  - in_audio   (CHOP)  — 音频包络 / 节拍数据
  - in_data    (CHOP)  — 可选数据流
  - in_control (CHOP)  — 强度、颜色、速度等参数

outputs:
  - out_top    (TOP)   — 渲染好的元件
```

### 网络结构
```
/project1/
  audio_bus/          ← 所有音频分析（见 audio-reactive.md）
  elements/
    elem_radar/       ← 含 out_top 的 baseCOMP
    elem_wave/
    elem_data/
  compositor/
    layout1           ← layoutTOP 或 overTOP 链
    dividers1         ← GLSL 分隔线
    postfx/           ← bloom → chrom → CRT 堆栈（见 postfx.md）
      null_out        ← 最终输出
  output/
    windowCOMP        ← 全屏输出
```

**关键原则：** 元件之间互不感知。由合成器负责组装。音频总线被所有元件引用，但独立存在。
