# TouchDesigner Python API 参考

## td 模块

TouchDesigner 的 Python 环境会自动导入 `td` 模块。所有 TD 专属的类、函数和常量都在这里。TD 内的脚本（Script DAT、CHOP/DAT Execute 回调、扩展）拥有完整访问权限。

使用 MCP `execute_python_script` 工具时，以下全局变量会预加载：
- `op` —— `td.op()` 的快捷方式，按路径查找算子
- `ops` —— `td.ops()` 的快捷方式，按模式查找多个算子
- `me` —— 运行脚本的算子（通过 MCP 时，这是 twozero 内部执行器）
- `parent` —— `me.parent()` 的快捷方式
- `project` —— 根工程组件
- `td` —— 完整的 td 模块

## 查找算子：op() 与 ops()

### op(path) —— 查找单个算子

```python
# 绝对路径（从 MCP 始终可用）
node = op('/project1/noise1')

# 相对路径（相对于当前算子 —— 仅在 Script DAT 中）
node = op('noise1')      # 同级
node = op('../noise1')   # 父级的同级

# 未找到时返回 None（不会抛异常）
node = op('/project1/nonexistent')  # None
```

### ops(pattern) —— 查找多个算子

```python
# Glob 模式
nodes = ops('/project1/noise*')       # 所有以 "noise" 开头的节点
nodes = ops('/project1/*')            # 所有直接子级
nodes = ops('/project1/container1/*') # container1 的所有子级

# 返回算子元组（可能为空）
for n in ops('/project1/*'):
    print(n.name, n.OPType)
```

### 从节点导航

```python
node = op('/project1/noise1')

node.name        # 'noise1'
node.path        # '/project1/noise1'
node.OPType      # 'noiseTop'
node.type         # <class 'noiseTop'>
node.family       # 'TOP'

# 父级 / 子级
node.parent()              # 父 COMP
node.parent().children     # 所有同级 + 自身
node.parent().findChildren(name='noise*')  # 过滤

# 类型判断
node.isTOP   # True
node.isCHOP  # False
node.isSOP   # False
node.isDAT   # False
node.isMAT   # False
node.isCOMP  # False
```

## 参数

每个算子都通过 `.par` 属性访问参数。

### 读取参数

```python
node = op('/project1/noise1')

# 直接访问
node.par.seed.val        # 当前求值（可能是表达式结果）
node.par.seed.eval()     # 等同于 .val
node.par.seed.default    # 默认值
node.par.monochrome.val  # 布尔参数：True/False

# 列出所有参数
for p in node.pars():
    print(f"{p.name}: {p.val} (default: {p.default})")

# 按页（参数组）过滤
for p in node.pars('Noise'):  # 页名
    print(f"{p.name}: {p.val}")
```

### 设置参数

```python
# 直接赋值
node.par.seed.val = 42
node.par.monochrome.val = True
node.par.resolutionw.val = 1920
node.par.resolutionh.val = 1080

# 字符串参数
op('/project1/text1').par.text.val = 'Hello World'

# 文件路径
op('/project1/moviefilein1').par.file.val = '/path/to/video.mp4'

# 引用另一个算子（用于 "dat"、"chop"、"top" 类型参数）
op('/project1/glsl1').par.dat.val = '/project1/shader_code'
```

### 参数表达式

```python
# 动态求值的 Python 表达式
node.par.seed.expr = "me.time.frame"
node.par.tx.expr = "math.sin(me.time.seconds * 2)"

# 引用另一个参数
node.par.brightness1.expr = "op('/project1/constant1').par.value0.val"

# Export（从 CHOP 到参数的单向绑定）
# 这让参数跟随某个 CHOP 通道值
op('/project1/noise1').par.seed.val  # 也可由 export 驱动
```

### 参数类型

| 类型 | Python 类型 | 示例 |
|------|------------|---------|
| Float | `float` | `node.par.brightness1.val = 0.5` |
| Int | `int` | `node.par.seed.val = 42` |
| Toggle | `bool` | `node.par.monochrome.val = True` |
| String | `str` | `node.par.text.val = 'hello'` |
| Menu | `int`（索引）或 `str`（标签） | `node.par.type.val = 'sine'` |
| File | `str`（路径） | `node.par.file.val = '/path/to/file'` |
| OP 引用 | `str`（路径） | `node.par.dat.val = '/project1/text1'` |
| Color | 分开的 r/g/b/a 浮点 | `node.par.colorr.val = 1.0` |
| XY/XYZ | 分开的 x/y/z 浮点 | `node.par.tx.val = 0.5` |

## 创建与删除算子

```python
# 通过父组件创建
parent = op('/project1')
new_node = parent.create(noiseTop)         # 用类引用
new_node = parent.create(noiseTop, 'my_noise')  # 带自定义名

# MCP 的 create_td_node 工具会自动处理：
# create_td_node(parentPath="/project1", nodeType="noiseTop", nodeName="my_noise")

# 删除
node = op('/project1/my_noise')
node.destroy()

# 复制
original = op('/project1/noise1')
copy = parent.copy(original, name='noise1_copy')
```

## 连接（给算子接线）

### 输出到输入的连接

```python
# 把 noise1 的输出连到 level1 的输入
op('/project1/noise1').outputConnectors[0].connect(op('/project1/level1'))

# 连到指定输入索引（用于 Composite 等多输入算子）
op('/project1/noise1').outputConnectors[0].connect(op('/project1/composite1').inputConnectors[0])
op('/project1/text1').outputConnectors[0].connect(op('/project1/composite1').inputConnectors[1])

# 断开所有输出
op('/project1/noise1').outputConnectors[0].disconnect()

# 查询连接
node = op('/project1/level1')
inputs = node.inputs          # 已连接的输入算子列表
outputs = node.outputs        # 已连接的输出算子列表
```

### 常见设置的连接模式

```python
# 线性链：A -> B -> C -> D
ops_list = [op(f'/project1/{name}') for name in ['noise1', 'level1', 'blur1', 'null1']]
for i in range(len(ops_list) - 1):
    ops_list[i].outputConnectors[0].connect(ops_list[i+1])

# 扇出：A -> B, A -> C, A -> D
source = op('/project1/noise1')
for target_name in ['level1', 'composite1', 'transform1']:
    source.outputConnectors[0].connect(op(f'/project1/{target_name}'))

# 合并：A + B + C -> Composite
comp = op('/project1/composite1')
for i, source_name in enumerate(['noise1', 'text1', 'ramp1']):
    op(f'/project1/{source_name}').outputConnectors[0].connect(comp.inputConnectors[i])
```

## DAT 内容操作

### Text DAT

```python
dat = op('/project1/text1')

# 读取
content = dat.text          # 以字符串返回全文

# 写入
dat.text = "new content"
dat.text = '''multi
line
content'''

# 追加
dat.text += "\nnew line"
```

### Table DAT

```python
dat = op('/project1/table1')

# 读单元格
val = dat[0, 0]         # 第 0 行、第 0 列
val = dat[0, 'name']    # 第 0 行、名为 'name' 的列
val = dat['key', 1]     # 名为 'key' 的行、第 1 列

# 写单元格
dat[0, 0] = 'value'

# 读行/列
row = dat.row(0)         # Cell 对象列表
col = dat.col('name')    # Cell 对象列表

# 维度
rows = dat.numRows
cols = dat.numCols

# 追加行
dat.appendRow(['col1_val', 'col2_val', 'col3_val'])

# 清空
dat.clear()

# 设置整张表
dat.clear()
dat.appendRow(['name', 'value', 'type'])
dat.appendRow(['frequency', '440', 'float'])
dat.appendRow(['amplitude', '0.8', 'float'])
```

## 时间与动画

```python
# 全局时间
td.absTime.frame       # 绝对帧号（永不重置）
td.absTime.seconds     # 绝对秒数

# 时间线时间（受播放/暂停/循环影响）
me.time.frame          # 时间线上的当前帧
me.time.seconds        # 时间线上的当前秒
me.time.rate           # FPS 设置

# 时间线控制（通过 execute_python_script）
project.play = True
project.play = False
project.frameRange = (1, 300)   # 设置时间线范围

# cook 帧（算子上次计算的时间）
node.cookFrame
node.cookTime
```

## 扩展（组件上的自定义 Python 类）

扩展给 COMP 添加自定义 Python 方法和属性。

```python
# 在 Base COMP 上创建扩展
base = op('/project1/myBase')

# 扩展类定义在 COMP 内部的一个 Text DAT 中
# 通常命名为 'ExtClass'，扩展代码如下：

extension_code = '''
class MyExtension:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        self.counter = 0

    def Reset(self):
        self.counter = 0

    def Increment(self):
        self.counter += 1
        return self.counter

    @property
    def Count(self):
        return self.counter
'''

# 把扩展代码写入 COMP 内的 DAT
op('/project1/myBase/extClass').text = extension_code

# 在 COMP 上配置扩展
base.par.extension1 = 'extClass'  # DAT 的名称
base.par.promoteextension1 = True  # 把方法提升到父级

# 调用扩展方法
base.Increment()       # 调用 MyExtension.Increment()
count = base.Count     # 访问 MyExtension.Count 属性
base.Reset()
```

## 实用内置模块

### tdu —— TouchDesigner 工具集

```python
import tdu

# 依赖跟踪（响应式值）
dep = tdu.Dependency(initial_value)
dep.val = new_value   # 触发依赖项重新 cook

# 文件路径工具
tdu.expandPath('$HOME/Desktop/output.mov')

# 数学
tdu.clamp(value, min, max)
tdu.remap(value, from_min, from_max, to_min, to_max)
```

### TDFunctions

```python
from TDFunctions import *

# 常用工具
clamp(value, low, high)
remap(value, inLow, inHigh, outLow, outHigh)
interp(value1, value2, t)  # 线性插值
```

### TDStoreTools —— 持久化存储

```python
from TDStoreTools import StorageManager

# 存储能在工程重载后保留的数据
me.store('myKey', 'myValue')
val = me.fetch('myKey', default='fallback')

# 存储 dict
me.storage['key'] = value
```

## 通过 execute_python_script 的常见模式

### 构建完整链

```python
# 创建一条完整的音频响应噪声链
parent = op('/project1')

# 创建算子
audio_in = parent.create(audiofileinChop, 'audio_in')
spectrum = parent.create(audiospectrumChop, 'spectrum')
chop_to_top = parent.create(choptopTop, 'chop_to_top')
noise = parent.create(noiseTop, 'noise1')
level = parent.create(levelTop, 'level1')
null_out = parent.create(nullTop, 'out')

# 接线
audio_in.outputConnectors[0].connect(spectrum)
spectrum.outputConnectors[0].connect(chop_to_top)
noise.outputConnectors[0].connect(level)
level.outputConnectors[0].connect(null_out)

# 设置参数
audio_in.par.file = '/path/to/music.wav'
audio_in.par.play = True
spectrum.par.size = 512
noise.par.type = 1  # Sparse
noise.par.monochrome = False
noise.par.resolutionw = 1920
noise.par.resolutionh = 1080
level.par.opacity = 0.8
level.par.gamma1 = 0.7
```

### 查询网络状态

```python
# 获取工程内所有 TOP
tops = [c for c in op('/project1').findChildren(type=TOP)]
for t in tops:
    print(f"{t.path}: {t.OPType} {'ERROR' if t.errors() else 'OK'}")

# 查找所有有错误的算子
def find_errors(parent_path='/project1'):
    parent = op(parent_path)
    errors = []
    for child in parent.findChildren(depth=-1):
        if child.errors():
            errors.append((child.path, child.errors()))
    return errors

result = find_errors()
```

### 批量参数修改

```python
# 一次性给多个节点设置参数
settings = {
    '/project1/noise1': {'seed': 42, 'monochrome': False, 'resolutionw': 1920},
    '/project1/level1': {'brightness1': 1.2, 'gamma1': 0.8},
    '/project1/blur1': {'sizex': 5, 'sizey': 5},
}

for path, params in settings.items():
    node = op(path)
    if node:
        for key, val in params.items():
            setattr(node.par, key, val)
```

## Python 版本与包

TouchDesigner 捆绑 Python 3.11+ 并预装以下包：
- **numpy** —— 数组运算、快速数学
- **scipy** —— 信号处理、FFT
- **OpenCV** (cv2) —— 计算机视觉
- **PIL/Pillow** —— 图像处理
- **requests** —— HTTP 客户端
- **json**、**re**、**os**、**sys** —— 标准库

**重要：** 下方示例中的参数名仅为示意。始终先运行发现流程（SKILL.md 第 0 步）以获取你所用 TD 版本的实际名称。切勿照抄这些示例中的参数名。

自定义包可安装到 TD 的 Python site-packages 目录。各平台的具体路径请参见 TD 文档。

## SOP 顶点/点访问（TD 2025.32）

在 TD 2025.32 中，`td.Vertex` 没有 `.x`、`.y`、`.z` 属性。请用索引访问：

```python
# 错误 —— 在 TD 2025.32 中会崩溃：
vertex.x, vertex.y, vertex.z

# 正确 —— 索引/属性访问：
pt = sop.points()[i]
pos = pt.P          # Position 对象
x, y, z = pos[0], pos[1], pos[2]

# 始终先内省：
dir(sop.points()[0])   # 查看实际存在哪些属性
dir(sop.points()[0].P) # 查看 Position 对象接口
```
