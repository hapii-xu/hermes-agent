# Replicator COMP 参考

`replicatorCOMP` 由一张数据表驱动，把模板算子克隆 N 次。这是 TD 中数据驱动网络的根本模式：按钮网格、场景清单、动态 UI、按通道的参数面板。

关于视觉实例化（按像素/按渲染的副本），见 `geometry-comp.md`。Replicator 构建的是网络节点；实例化构建的是渲染副本。属于不同层级。

---

## 概念

```
[模板算子]                  [数据 tableDAT]
       │                              │
       └─────→ replicatorCOMP ←───────┘
                     │
                     ▼
        [N 个克隆]，每行数据一个
        每个克隆获得逐行参数
```

编辑一次模板 → 所有克隆继承。编辑表格 → 克隆动态增删。可为每行推送参数覆盖。

---

## 最小设置

```python
# 1. 制作模板（要克隆的东西）
template = root.create(buttonCOMP, 'btn_template')
template.par.w = 80; template.par.h = 80
template.par.text = 'X'
template.par.bgcolorr = 0.2

# 2. 制作数据表（每个克隆一行）
data = root.create(tableDAT, 'scene_data')
data.appendRow(['name', 'color_r', 'color_g', 'color_b'])
data.appendRow(['Sunset', 1.0, 0.4, 0.0])
data.appendRow(['Midnight', 0.0, 0.1, 0.4])
data.appendRow(['Storm', 0.3, 0.3, 0.5])
data.appendRow(['Forest', 0.0, 0.5, 0.2])

# 3. Replicator —— 指向模板 + 数据
rep = root.create(replicatorCOMP, 'scene_buttons')
rep.par.template = template.path
rep.par.opfromdat = data.path
rep.par.namefromdatname = 'name'        # 用 'name' 列作克隆名
rep.par.incrementalnumbering = False
```

cook 后，replicator 会创建 4 个名为 `Sunset`、`Midnight`、`Storm`、`Forest` 的子 COMP（每个非表头行一个），都从 `btn_template` 克隆而来。

---

## 逐行参数覆盖

replicator 的停靠 `replicator1_callbacks` DAT 允许你自定义每个克隆：

```python
def onReplicate(comp, allOps, newOps, template, master):
    """每次复制循环调用一次。newOps 是刚创建的克隆列表。"""
    data = op('scene_data')
    for i, clone in enumerate(newOps):
        row = i + 1                 # +1 跳过表头
        clone.par.text = data[row, 'name'].val
        clone.par.bgcolorr = float(data[row, 'color_r'].val)
        clone.par.bgcolorg = float(data[row, 'color_g'].val)
        clone.par.bgcolorb = float(data[row, 'color_b'].val)
    return
```

或使用引用 `digits`（每个克隆的索引，作为内置表达式令牌在克隆子树内可用）的参数表达式：

```python
# 在模板内，设置类似如下的参数表达式：
# par.value0.expr = "op('../scene_data')[me.digits + 1, 'value']"
```

`me.digits` 解析为当前克隆的行索引。对静态引用模式这是最干净的方式 —— 无需回调。

---

## 布局：按钮网格

把 replicator 放进一个带自动布局的 `containerCOMP`：

```python
panel = root.create(containerCOMP, 'scene_panel')
panel.par.w = 400; panel.par.h = 100
panel.par.align = 'lefttoright'

# 把 replicator 移进去
rep.parent = panel.path           # 或直接把 rep 作为 panel 的子级创建
```

每个克隆都是 replicator 的子级（replicator 本身是面板的子级）。面板会自动排列一切。

要做二维网格，把容器的 `par.align` 设为 `'fillresize'`，并在回调中按行/列索引覆盖每个克隆的 `par.x` / `par.y`。

---

## 不重建地更新

当数据表变化时，replicator 会重新生成克隆。默认它会销毁并重建一切。要保留状态，设置：

```python
rep.par.recreatemissing = True       # 仅增删变化行
rep.par.recreateallonchange = False
```

此模式对实时编辑场景（设计者调整表格、网络持续运行）至关重要。

对增量数据摄入（例如轮询 API 的 `webDAT`），让 `datExecuteDAT` 监视响应、解析、写入数据表，replicator 会自我更新。

---

## 常见模式

### 场景清单（数据 → 按钮 + 逻辑）

```python
# 每个场景的数据：名称、文件路径、音轨、BPM
scene_data.appendRow(['name', 'file', 'audio', 'bpm'])
scene_data.appendRow(['Intro', '/scenes/intro.tox', '/audio/intro.wav', 110])
scene_data.appendRow(['Main', '/scenes/main.tox', '/audio/main.wav', 128])

# Replicator 每个场景克隆一个 buttonCOMP
# 每个按钮的 onClick 回调加载对应的 tox + 预备音频
```

### 动态参数面板

对一组音频频段，每个频段生成一条推子条：

```python
# 数据：频段名（sub、low、mid、hi-mid、high、air）
# 模板：带标签 + sliderCOMP 的 containerCOMP
# Replicator 克隆 N 条
# 每个推子的值在 /audio_eq/{band_name}/fader 处读取
```

### 程序化视觉网络

从配置文件构建多通道视觉网络：

```python
# 数据：要串联的 TOPs，按“场景”
# 模板：带占位子级的 baseCOMP
# Replicator 每个场景构建一个 baseCOMP；每个场景含自定义链
# 通过面板驱动的 switchTOP.par.index 在场景间切换
```

### 按通道的 CHOP 显示

分别可视化多通道 CHOP 的每个通道：

```python
# 数据表：每个通道一行（通过 choptodatDAT 自动提取）
# 模板：显示一个通道的小型 chopVis COMP
# Replicator 生成 N 个垂直堆叠的可视化器
```

---

## Replicator vs. 纯 Python 循环

| 方式 | 何时使用 |
|---|---|
| **replicatorCOMP** | 克隆集合会变化（实时增删行）。视觉编辑器预期。模式可跨项目复用。 |
| **Python 循环**（在 `td_execute_python` 中） | 一次性生成。静态集合。逻辑更简单、无模板开销。编写更快。 |

如果只构建一次网络，优先用 `td_execute_python` 的 Python 循环。当数据是实时变化时，replicator 才显出价值。

---

## 陷阱

1. **表头行** —— `tableDAT` 行从 0 开始。若有表头，第一行数据索引为 1。回调中差一错误很常见。
2. **`namefromdatname` 列缺失** —— replicator 会静默使用 `digits`（数字后缀）命名。按钮最终被命名为 `1`、`2`、`3` 而非有意义的名字。请显式设置 `par.namefromdatname`。
3. **模板在网络中真实存在** —— 模板算子本身是一个真实网络节点。不要直接把东西连到它的下游；应连到克隆（或在中间用 `nullCOMP`）。
4. **变更即重建会清空状态** —— 克隆内的切换、滑块位置、未缓存数据在每次重新生成时都会丢失。用 `recreatemissing` 保留。
5. **`onReplicate` 在编辑时不触发** —— 仅在克隆集合变化时触发。编辑已有行内的值不会重新触发。逐单元格实时更新请用 `parameterExecuteDAT` 或表达式。
6. **克隆上的自定义参数** —— 在模板中添加的页会传播。在 `onReplicate` 中添加的页在下一次重新生成后不保留。始终在模板上添加自定义页，而非克隆。
7. **cook 风暴** —— 快速添加很多行会触发大量克隆事件。用 Python 批量添加，最后调用一次 `data.cook(force=True)`。
8. **在 replicator 子级之外使用 `me.digits`** —— `me.digits` 只在 replicator 后代的算子内解析。不要在无关网络中引用它。
9. **跨克隆引用** —— 从克隆内用相对路径引用同级克隆有效（`op('../OtherClone/x')`），但改名后会断。优先用经由数据表的绝对路径。

---

## 快速配方

| 目标 | 设置 |
|---|---|
| 8 按钮场景选择器 | `tableDAT`（8 行）+ `buttonCOMP` 模板 + `replicatorCOMP` |
| 按频段 EQ 条面板 | `tableDAT`（频段名）+ 容器模板（标签 + 滑块）+ replicator |
| 数据驱动的视觉场景 | `tableDAT`（场景配置）+ `baseCOMP` 模板（视觉链）+ replicator |
| 实时更新的克隆集 | 同上 + `par.recreatemissing = True` |
| 逐行彩色 UI | 带颜色列的数据表，`onReplicate` 回调设置每克隆颜色 |
| 来自 API 响应的列表 | `webDAT` → `datExecuteDAT` 解析 JSON → 写入数据表 → replicator 更新 |
