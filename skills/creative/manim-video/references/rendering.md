# 渲染参考

## 前置条件

```bash
manim --version       # Manim CE
pdflatex --version    # LaTeX
ffmpeg -version       # ffmpeg
```

## CLI 参考

```bash
manim -ql script.py Scene1 Scene2    # 草稿（480p 15fps）
manim -qm script.py Scene1           # 中等（720p 30fps）
manim -qh script.py Scene1           # 成品（1080p 60fps）
manim -ql --format=png -s script.py Scene1  # 预览静帧（最后一帧）
manim -ql --format=gif script.py Scene1     # GIF 输出
```

## 质量预设

| 标志 | 分辨率 | FPS | 用途 |
|------|-----------|-----|----------|
| `-ql` | 854x480 | 15 | 草稿迭代（布局、节奏） |
| `-qm` | 1280x720 | 30 | 预览（文字密集场景用这个） |
| `-qh` | 1920x1080 | 60 | 成品 |

**文字渲染质量：** `-ql`（480p15）的文字字距和可读性明显很差。对于有大量文字的场景，在 `-qm` 下预览静帧以发现 480p 下看不见的问题。`-ql` 仅用于测试布局和动画节奏。

## 输出结构

```
media/videos/script/480p15/Scene1_Intro.mp4
media/images/script/Scene1_Intro.png  （来自 -s 标志）
```

## 用 ffmpeg 拼接

```bash
cat > concat.txt << 'EOF'
file 'media/videos/script/480p15/Scene1_Intro.mp4'
file 'media/videos/script/480p15/Scene2_Core.mp4'
EOF
ffmpeg -y -f concat -safe 0 -i concat.txt -c copy final.mp4
```

## 添加配音

```bash
# 混入旁白
ffmpeg -y -i final.mp4 -i narration.mp3 -c:v copy -c:a aac -b:a 192k -shortest final_narrated.mp4

# 先把每个场景的音频拼接起来
cat > audio_concat.txt << 'EOF'
file 'audio/scene1.mp3'
file 'audio/scene2.mp3'
EOF
ffmpeg -y -f concat -safe 0 -i audio_concat.txt -c copy full_narration.mp3
```

## 添加背景音乐

```bash
ffmpeg -y -i final.mp4 -i music.mp3 \
  -filter_complex "[1:a]volume=0.15[bg];[0:a][bg]amix=inputs=2:duration=shortest" \
  -c:v copy final_with_music.mp4
```

## GIF 导出

```bash
ffmpeg -y -i scene.mp4 \
  -vf "fps=15,scale=640:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  output.gif
```

## 宽高比

```bash
manim -ql --resolution 1080,1920 script.py Scene  # 9:16 竖屏
manim -ql --resolution 1080,1080 script.py Scene  # 1:1 正方形
```

## 渲染工作流

1. 在 `-ql` 下草稿渲染所有场景
2. 在关键时刻预览静帧（`-s`）
3. 修复并只重新渲染出问题的场景
4. 用 ffmpeg 拼接
5. 审查拼接后的输出
6. 在 `-qh` 下成品渲染
7. 重新拼接 + 加音频

## manim.cfg —— 项目配置

在项目目录下创建 `manim.cfg` 以设定项目级默认值：

```ini
[CLI]
quality = low_quality
preview = True
media_dir = ./media

[renderer]
background_color = #0D1117

[tex]
tex_template_file = custom_template.tex
```

这样就省去了在每个场景里重复写 CLI 标志和 `self.camera.background_color`。

## Sections —— 章节标记

在一个场景内标记章节，以便输出更有组织：

```python
class LongVideo(Scene):
    def construct(self):
        self.next_section("Introduction")
        # ... 引言内容 ...

        self.next_section("Main Concept")
        # ... 主要内容 ...

        self.next_section("Conclusion")
        # ... 收尾 ...
```

渲染单独的章节：`manim --save_sections script.py LongVideo`
这会为每个章节输出单独的视频文件 —— 适用于长视频中只想重新渲染某一部分的情况。

## manim-voiceover 插件（推荐用于带旁白的视频）

官方的 `manim-voiceover` 插件把 TTS 直接集成进场景代码，自动把动画时长同步到配音长度。这比上面手动的 ffmpeg 混流方式干净得多。

### 安装

```bash
pip install "manim-voiceover[elevenlabs]"
# 或者用免费/本地 TTS：
pip install "manim-voiceover[gtts]"    # Google TTS（免费，质量较低）
pip install "manim-voiceover[azure]"   # Azure 认知服务
```

### 用法

```python
from manim import *
from manim_voiceover import VoiceoverScene
from manim_voiceover.services.elevenlabs import ElevenLabsService

class NarratedScene(VoiceoverScene):
    def construct(self):
        self.set_speech_service(ElevenLabsService(
            voice_name="Alice",
            model_id="eleven_multilingual_v2"
        ))

        # 配音自动控制场景时长
        with self.voiceover(text="Here is a circle being drawn.") as tracker:
            self.play(Create(Circle()), run_time=tracker.duration)

        with self.voiceover(text="Now let's transform it into a square.") as tracker:
            self.play(Transform(circle, Square()), run_time=tracker.duration)
```

### 关键特性

- `tracker.duration` —— 配音总时长（秒）
- `tracker.time_until_bookmark("mark1")` —— 把特定动画同步到特定词
- 自动生成字幕 `.srt` 文件
- 在本地缓存音频 —— 重新渲染不会重新生成 TTS
- 支持：ElevenLabs、Azure、Google TTS、pyttsx3（离线）以及自定义服务

### 用书签做精确同步

```python
with self.voiceover(text='This is a <bookmark mark="circle"/>circle.') as tracker:
    self.wait_until_bookmark("circle")
    self.play(Create(Circle()), run_time=tracker.time_until_bookmark("circle", limit=1))
```

这是任何带旁白的视频推荐的做法。上面的手动 ffmpeg 混流工作流仍然适用于添加背景音乐或后期音频混音。
