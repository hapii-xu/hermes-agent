# p5.js Skill

使用 [p5.js](https://p5js.org/) 打造交互式与生成式视觉艺术的生产流水线。

## 它能做什么

根据文本提示创建浏览器端的视觉艺术。agent 负责整条流水线：创意构思、代码生成、预览、导出和迭代打磨。输出是一个可在任意浏览器中运行的自包含 HTML 文件——无需构建步骤、无需服务器，除了 CDN script 标签外没有任何依赖。

输出的是真正的交互艺术，而不是教程练习。生成系统、粒子物理、噪声场、着色器（shader）效果、动态字体排版——配合经过精心设计的配色方案、分层构图和视觉层次。

## 模式

| 模式 | 输入 | 输出 |
|------|-------|--------|
| **生成艺术（Generative art）** | 种子 / 参数 | 程序化视觉构图 |
| **数据可视化** | 数据集 / API | 交互式图表、自定义数据展示 |
| **交互体验** | 无（由用户驱动） | 鼠标/键盘/触摸驱动的 sketch |
| **动画 / 动效图形** | 时间轴 / 分镜脚本 | 定时序列、动态字体排版 |
| **3D 场景** | 概念描述 | WebGL 几何体、光照、着色器 |
| **图像处理** | 图像文件 | 像素操作、滤镜、点画派 |
| **声音响应（Audio-reactive）** | 音频文件 / 麦克风 | 由声音驱动的生成式视觉效果 |

## 导出格式

| 格式 | 方式 |
|--------|--------|
| **HTML** | 自包含文件，可在任意浏览器中打开 |
| **PNG** | `saveCanvas()`——按 's' 捕获 |
| **GIF** | `saveGif()`——按 'g' 捕获 |
| **MP4** | 通过 `scripts/render.sh` 的帧序列 + ffmpeg |
| **SVG** | p5.js-svg 渲染器，用于矢量输出 |

## 前置条件

一个现代浏览器。基础使用有它就够了。

用于无头导出：Node.js、Puppeteer、ffmpeg。

```bash
bash skills/creative/p5js/scripts/setup.sh
```

## 文件结构

```
├── SKILL.md                      # 模式、工作流、创意方向、关键说明
├── README.md                     # 本文件
├── references/
│   ├── core-api.md              # 画布、绘制循环、变换、离屏缓冲、数学
│   ├── shapes-and-geometry.md   # 基础图元、顶点、曲线、向量、SDF、裁剪
│   ├── visual-effects.md        # 噪声、流场、粒子、像素、纹理、反馈
│   ├── animation.md             # 缓动、弹簧、状态机、时间轴、过渡
│   ├── typography.md            # 字体、textToPoints、动态文字、文字遮罩
│   ├── color-systems.md         # HSB/RGB、配色、渐变、混合模式、精选颜色
│   ├── webgl-and-3d.md          # 3D 图元、相机、光照、着色器、帧缓冲
│   ├── interaction.md           # 鼠标、键盘、触摸、DOM、音频、滚动
│   ├── export-pipeline.md       # PNG、GIF、MP4、SVG、无头渲染、分块、批量导出
│   └── troubleshooting.md       # 性能、常见错误、浏览器问题、调试
└── scripts/
    ├── setup.sh                 # 依赖验证
    ├── serve.sh                 # 本地开发服务器（用于加载本地资源）
    ├── render.sh                # 无头渲染流水线（HTML → 帧 → MP4）
    └── export-frames.js         # Puppeteer 帧捕获（Node.js）
```
