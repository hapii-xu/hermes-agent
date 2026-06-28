# TouchDesigner 算子参考

## 算子家族概览

TouchDesigner 有 6 个算子家族。每个家族处理一种特定数据类型，并在 UI 中以颜色区分。算子只能连接到同家族的其他算子（跨家族需用转换器作为桥梁）。

## TOPs —— 纹理算子（紫色）

在 GPU 上进行 2D 图像/纹理处理。视觉输出的主力。

### 生成器（凭空生成图像）

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Noise TOP | `noiseTop` | `type`（0-6）、`monochrome`、`seed`、`period`、`harmonics`、`exponent`、`amp`、`offset`、`resolutionw/h` | 程序化噪声纹理 —— Perlin、Simplex、Sparse 等。生成艺术的基础。 |
| Constant TOP | `constantTop` | `colorr/g/b/a`、`resolutionw/h` | 纯色。用作背景或混合输入。 |
| Text TOP | `textTop` | `text`、`fontsizex`、`fontfile`、`alignx/y`、`colorr/g/b` | 把文字渲染为纹理。支持多行、自动换行。 |
| Ramp TOP | `rampTop` | `type`（0=水平、1=垂直、2=径向、3=环形）、`phase`、`period` | 渐变纹理，用于遮罩、颜色映射。 |
| Circle TOP | `circleTop` | `radiusx/y`、`centerx/y`、`width` | 圆、环、椭圆。 |
| Rectangle TOP | `rectangleTop` | `sizex/y`、`centerx/y`、`softness` | 矩形，可选软边。 |
| GLSL TOP | `glslTop` | `dat`（指向着色器 DAT）、`resolutionw/h`、`outputformat`、自定义 uniform | 自定义片元着色器。最强大的自定义视觉 TOP。 |
| GLSL Multi TOP | `glslmultiTop` | `dat`、`numinputs`、`numoutputs`、`numcomputepasses` | 带 compute 着色器的多遍 GLSL。进阶。 |
| Render TOP | `renderTop` | `camera`、`geometry`、`lights`、`resolutionw/h` | 渲染 3D 场景（SOPs + MATs + Camera/Light COMPs）。 |

### 滤镜（修改单个输入）

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Level TOP | `levelTop` | `opacity`、`brightness1/2`、`gamma1/2`、`contrast`、`invert`、`blacklevel/whitelevel` | 亮度、对比度、伽马、色阶。必备的调色工具。 |
| Blur TOP | `blurTop` | `sizex/y`、`type`（0=高斯、1=方框、2=Bartlett） | 高斯/方框模糊。 |
| Transform TOP | `transformTop` | `tx/ty`、`sx/sy`、`rz`、`pivotx/y`、`extend`（0=保留、1=零、2=重复、3=镜像） | 平移、缩放、旋转纹理。 |
| HSV Adjust TOP | `hsvadjustTop` | `hueoffset`、`saturationmult`、`valuemult` | HSV 色彩调整。 |
| Lookup TOP | `lookupTop` | （输入：纹理 + 查找表） | 通过查找表纹理做颜色重映射。 |
| Edge TOP | `edgeTop` | `type`（0=Sobel、1=Frei-Chen） | 边缘检测。 |
| Displace TOP | `displaceTop` | `scalex/y` | 用第二个输入作为位移图做像素位移。 |
| Flip TOP | `flipTop` | `flipx`、`flipy`、`flop`（对角线） | 镜像/翻转纹理。 |
| Crop TOP | `cropTop` | `cropleft/right/top/bottom` | 裁剪纹理区域。 |
| Resolution TOP | `resolutionTop` | `resolutionw/h`、`outputresolution` | 调整纹理尺寸。 |
| Null TOP | `nullTop` | （无重要参数） | 透传。用于组织、引用、反馈延迟。 |
| Cache TOP | `cacheTop` | `length`、`step` | 存储 N 帧历史。用于拖尾、时间效果。 |

### 合成器（合并多个输入）

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Composite TOP | `compositeTop` | `operand`（0-31：Over、Add、Multiply、Screen 等） | 用标准合成模式混合两张纹理。 |
| Over TOP | `overTop` | （简单 alpha 合成） | 带 alpha 的图层叠加。比 Composite 更简单。 |
| Add TOP | `addTop` | （相加混合） | 相加混合。非常适合发光、光效。 |
| Multiply TOP | `multiplyTop` | （相乘混合） | 相乘混合。适合遮罩、压暗。 |
| Switch TOP | `switchTop` | `index`（从 0 开始） | 按索引在多个输入间切换。 |
| Cross TOP | `crossTop` | `cross`（0.0-1.0） | 在两个输入间交叉淡入淡出。 |

### I/O（输入/输出）

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Movie File In TOP | `moviefileinTop` | `file`、`speed`、`trim`、`index` | 加载视频文件、图像序列。 |
| Movie File Out TOP | `moviefileoutTop` | `file`、`type`（编码器）、`record`（开关） | 录制/导出视频文件。 |
| NDI In TOP | `ndiinTop` | `sourcename` | 接收 NDI 视频流。 |
| NDI Out TOP | `ndioutTop` | `sourcename` | 发送 NDI 视频流。 |
| Syphon Spout In/Out TOP | `syphonspoutinTop` / `syphonspoutoutTop` | `servername` | 跨应用纹理共享。 |
| Video Device In TOP | `videodeviceinTop` | `device` | 摄像头/采集卡输入。 |
| Feedback TOP | `feedbackTop` | `top`（要反馈的 TOP 路径） | 单帧延迟反馈。递归效果必备。 |

### 转换器

| 算子 | 类型名 | 方向 | 用途 |
|----------|-----------|-----------|-----|
| CHOP to TOP | `choptopTop` | CHOP -> TOP | 把通道数据可视化为纹理（波形、频谱显示）。 |
| TOP to CHOP | `topchopChop` | TOP -> CHOP | 把纹理像素采样为通道数据。 |

## CHOPs —— 通道算子（绿色）

时变的数值数据：音频、动画曲线、传感器数据、控制信号。

### 生成器

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Constant CHOP | `constantChop` | `name0/value0`、`name1/value1`... | 静态命名通道。参数的控制面板。 |
| LFO CHOP | `lfoChop` | `frequency`、`type`（0=正弦、1=三角、2=方波、3=斜坡、4=脉冲）、`amp`、`offset`、`phase` | 低频振荡器。动画驱动源。 |
| Noise CHOP | `noiseChop` | `type`、`roughness`、`period`、`amp`、`seed`、`channels` | 平滑随机运动。有机动画。 |
| Pattern CHOP | `patternChop` | `type`（0=正弦、1=三角、...）、`length`、`cycles` | 生成波形图案。 |
| Timer CHOP | `timerChop` | `length`、`play`、`cue`、`cycles` | 带提示点的倒计时/正计时器。 |
| Count CHOP | `countChop` | `threshold`、`limittype`、`limitmin/max` | 带回绕/钳制的事件计数器。 |

### 音频

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Audio File In CHOP | `audiofileinChop` | `file`、`volume`、`play`、`speed`、`trim` | 播放音频文件。 |
| Audio Device In CHOP | `audiodeviceinChop` | `device`、`channels` | 实时麦克风/线路输入。 |
| Audio Spectrum CHOP | `audiospectrumChop` | `size`（FFT 尺寸）、`outputformat`（0=功率、1=幅度） | FFT 频率分析。 |
| Audio Band EQ CHOP | `audiobandeqChop` | `bands`、每频段 `gaindb` | 频段隔离。 |
| Audio Device Out CHOP | `audiodeviceoutChop` | `device` | 音频播放输出。 |

### 数学/逻辑

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Math CHOP | `mathChop` | `preoff`、`gain`、`postoff`、`chanop`（0=关、1=加、2=减、3=乘...） | 通道上的数学运算。瑞士军刀。 |
| Logic CHOP | `logicChop` | `preop`（0=关、1=AND、2=OR、3=XOR、4=NAND）、`convert` | 通道上的布尔逻辑。 |
| Filter CHOP | `filterChop` | `type`（0=低通、1=带通、2=高通、3=陷波）、`cutofffreq`、`filterwidth` | 平滑、阻尼、滤波信号。 |
| Lag CHOP | `lagChop` | `lag1/2`、`overshoot1/2` | 带过冲的平滑过渡。 |
| Limit CHOP | `limitChop` | `type`（0=钳制、1=循环、2=ZigZag）、`min/max` | 钳制或回绕通道值。 |
| Speed CHOP | `speedChop` | （无重要参数） | 积分值（速度到位移、加速度到速度）。 |
| Trigger CHOP | `triggerChop` | `attack`、`peak`、`decay`、`sustain`、`release` | 从触发事件生成 ADSR 包络。 |
| Select CHOP | `selectChop` | `chop`（路径）、`channames` | 从另一个 CHOP 引用通道。 |
| Merge CHOP | `mergeChop` | `align`（0=扩展、1=裁到第一个、2=裁到最短） | 合并多个 CHOP 的通道。 |
| Null CHOP | `nullChop` | （无重要参数） | 用于组织和引用的透传。 |

### 输入设备

| 算子 | 类型名 | 用途 |
|----------|-----------|-----|
| Mouse In CHOP | `mouseinChop` | 鼠标位置、按键、滚轮。 |
| Keyboard In CHOP | `keyboardinChop` | 键盘按键状态。 |
| MIDI In CHOP | `midiinChop` | MIDI 音符/CC 输入。 |
| OSC In CHOP | `oscinChop` | OSC 消息输入（网络）。 |

## SOPs —— 曲面算子（蓝色）

3D 几何体：点、多边形、NURBS、网格。

### 生成器

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Grid SOP | `gridSop` | `rows`、`cols`、`sizex/y`、`type`（0=多边形、1=网格、2=NURBS） | 平面网格。位移、实例化的基础。 |
| Sphere SOP | `sphereSop` | `type`、`rows`、`cols`、`radius` | 球体几何。 |
| Box SOP | `boxSop` | `sizex/y/z` | 盒子几何。 |
| Torus SOP | `torusSop` | `radiusx/y`、`rows`、`cols` | 圆环（甜甜圈）形状。 |
| Circle SOP | `circleSop` | `type`、`radius`、`divs` | 圆/环几何。 |
| Line SOP | `lineSop` | `dist`、`points` | 线段。 |
| Text SOP | `textSop` | `text`、`fontsizex`、`fontfile`、`extrude` | 3D 文字几何。 |

### 修改器

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Transform SOP | `transformSop` | `tx/ty/tz`、`rx/ry/rz`、`sx/sy/sz` | 变换几何体（平移、旋转、缩放）。 |
| Noise SOP | `noiseSop` | `type`、`amp`、`period`、`roughness` | 用噪声变形几何体。 |
| Sort SOP | `sortSop` | `ptsort`、`primsort` | 重排点/图元。 |
| Facet SOP | `facetSop` | `unique`、`consolidate`、`computenormals` | 法线、合并、唯一化点。 |
| Merge SOP | `mergeSop` | （无重要参数） | 合并多个几何体输入。 |
| Null SOP | `nullSop` | （无重要参数） | 透传。 |

## DATs —— 数据算子（白色）

文本、表格、脚本、网络数据。

### 核心

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Table DAT | `tableDat` | （直接编辑内容） | 类电子表格的数据表。 |
| Text DAT | `textDat` | （直接编辑内容） | 任意文本内容。着色器代码、配置、脚本。 |
| Script DAT | `scriptDat` | `language`（0=Python、1=C++） | 自定义回调和 DAT 处理。 |
| CHOP Execute DAT | `chopexecDat` | `chop`（要监视的路径）、回调 | 在 CHOP 值变化时触发 Python。 |
| DAT Execute DAT | `datexecDat` | `dat`（要监视的路径） | 在 DAT 内容变化时触发 Python。 |
| Panel Execute DAT | `panelexecDat` | `panel` | 在 UI 面板事件时触发 Python。 |

### I/O

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Web DAT | `webDat` | `url`、`fetchmethod`（0=GET、1=POST） | HTTP 请求。API 集成。 |
| TCP/IP DAT | `tcpipDat` | `address`、`port`、`mode` | TCP 网络。 |
| OSC In DAT | `oscinDat` | `port` | 以文本消息形式接收 OSC。 |
| Serial DAT | `serialDat` | `port`、`baudrate` | 串口通信（Arduino 等）。 |
| File In DAT | `fileinDat` | `file` | 读取文本文件。 |
| File Out DAT | `fileoutDat` | `file`、`write` | 写入文本文件。 |

### 转换

| 算子 | 类型名 | 方向 | 用途 |
|----------|-----------|-----------|-----|
| DAT to CHOP | `dattochopChop` | DAT -> CHOP | 把表格数据转换为通道。 |
| CHOP to DAT | `choptodatDat` | CHOP -> DAT | 把通道数据转换为表格行。 |
| SOP to DAT | `soptodatDat` | SOP -> DAT | 以表格形式提取几何数据。 |

## MATs —— 材质算子（黄色）

用于在 Render TOP / Geometry COMP 中进行 3D 渲染的材质。

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Phong MAT | `phongMat` | `diff_colorr/g/b`、`spec_colorr/g/b`、`shininess`、`colormap`、`normalmap` | 经典 Phong 着色。简单、快速。 |
| PBR MAT | `pbrMat` | `basecolorr/g/b`、`metallic`、`roughness`、`normalmap`、`emitcolorr/g/b` | 基于物理的渲染。写实材质。 |
| GLSL MAT | `glslMat` | `dat`（着色器 DAT）、自定义 uniform | 用于 3D 的自定义顶点 + 片元着色器。 |
| Constant MAT | `constMat` | `colorr/g/b`、`colormap` | 平面无光照颜色/纹理。无着色。 |
| Point Sprite MAT | `pointspriteMat` | `colormap`、`scale` | 把点渲染为面向相机的精灵。适合粒子。 |
| Wireframe MAT | `wireframeMat` | `colorr/g/b`、`width` | 线框渲染。 |
| Depth MAT | `depthMat` | `near`、`far` | 把深度缓冲渲染为灰度图。 |

## COMPs —— 组件算子（灰色）

容器、3D 场景元素、UI 组件。

### 3D 场景

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Geometry COMP | `geometryComp` | `material`（路径）、`instancechop`（路径）、`instancing`（开关） | 用材质渲染几何体。实例化宿主。 |
| Camera COMP | `cameraComp` | `tx/ty/tz`、`rx/ry/rz`、`fov`、`near/far` | 用于 Render TOP 的相机。 |
| Light COMP | `lightComp` | `lighttype`（0=点、1=平行、2=聚光、3=锥形）、`dimmer`、`colorr/g/b` | 3D 场景的灯光。 |
| Ambient Light COMP | `ambientlightComp` | `dimmer`、`colorr/g/b` | 环境光。 |
| Environment Light COMP | `envlightComp` | `envmap` | 基于图像的光照（IBL）。 |

### 容器

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Container COMP | `containerComp` | `w`、`h`、`bgcolor1/2/3` | UI 容器。承载其他 COMP 以做面板布局。 |
| Base COMP | `baseComp` | （无重要参数） | 通用容器。网络套网络。 |
| Replicator COMP | `replicatorComp` | `template`、`operatorsdat` | 按表格把模板算子克隆 N 次。 |

### 工具

| 算子 | 类型名 | 关键参数 | 用途 |
|----------|-----------|---------------|-----|
| Window COMP | `windowComp` | `winw/h`、`winoffsetx/y`、`monitor`、`borders` | 用于显示/投影的输出窗口。 |
| Select COMP | `selectComp` | `rowcol`、`panel` | 从别处选择并显示内容。 |
| Engine COMP | `engineComp` | `tox`、`externaltox` | 加载外部 .tox 组件。子进程隔离。 |

## 跨家族转换器汇总

| 源 | 目标 | 算子 | 类型名 |
|------|-----|----------|-----------|
| CHOP | TOP | CHOP to TOP | `choptopTop` |
| TOP | CHOP | TOP to CHOP | `topchopChop` |
| DAT | CHOP | DAT to CHOP | `dattochopChop` |
| CHOP | DAT | CHOP to DAT | `choptodatDat` |
| SOP | CHOP | SOP to CHOP | `soptochopChop` |
| CHOP | SOP | CHOP to SOP | `choptosopSop` |
| SOP | DAT | SOP to DAT | `soptodatDat` |
| DAT | SOP | DAT to SOP | `dattosopSop` |
| SOP | TOP | （用 Render TOP + Geometry COMP） | — |
| TOP | SOP | TOP to SOP | `toptosopSop` |
