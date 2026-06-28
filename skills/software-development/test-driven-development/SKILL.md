---
name: test-driven-development
description: "TDD：强制执行 RED-GREEN-REFACTOR，先写测试再写代码。"
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [testing, tdd, development, quality, red-green-refactor]
    related_skills: [systematic-debugging, plan, subagent-driven-development]
---

# 测试驱动开发（TDD）

## 概览

先写测试。看它失败。再写最小代码让它通过。

**核心原则：** 如果你没看到测试失败，你就不知道它测的是不是对的东西。

**违反规则的字面意义，就是违反规则的精神。**

## 何时使用

**始终：**
- 新功能
- 修 bug
- 重构
- 行为变更

**例外（先征求用户意见）：**
- 一次性原型
- 生成代码
- 配置文件

想着 "就这一次跳过 TDD"？停下。那是在自我合理化。

## 铁律

```
没有先写失败的测试，就不要写生产代码
```

先写了代码再写测试？删掉它。从头来。

**没有例外：**
- 不要把它留作 "参考"
- 不要在写测试时 "改造" 它
- 不要看它
- 删除就是删除

从测试出发重新实现。就这样。

## Red-Green-Refactor 循环

### RED —— 写失败的测试

写一个最小测试，展示应有的行为。

**好的测试：**
```python
def test_retries_failed_operations_3_times():
    attempts = 0
    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise Exception('fail')
        return 'success'

    result = retry_operation(operation)

    assert result == 'success'
    assert attempts == 3
```
名字清晰、测试真实行为、只测一件事。

**坏的测试：**
```python
def test_retry_works():
    mock = MagicMock()
    mock.side_effect = [Exception(), Exception(), 'success']
    result = retry_operation(mock)
    assert result == 'success'  # 重试次数呢？时机呢？
```
名字含糊、测的是 mock 而非真实代码。

**要求：**
- 一个测试只测一种行为
- 名字清晰具描述性（名字里有 "and"？拆开它）
- 用真实代码，不用 mock（除非真的避不开）
- 名字描述行为，而非实现

### 验证 RED —— 看着它失败

**强制。绝不跳过。**

```bash
# 用 terminal 工具运行特定测试
pytest tests/test_feature.py::test_specific_behavior -v
```

确认：
- 测试失败（不是拼写错误导致的报错）
- 失败信息符合预期
- 是因为功能缺失而失败

**测试立即通过？** 你测的是已有行为。修正测试。

**测试报错？** 修正错误，重新运行，直到它正确地失败。

### GREEN —— 最小代码

写最简单的代码让测试通过。仅此而已。

**好的：**
```python
def add(a, b):
    return a + b  # 没有多余的东西
```

**坏的：**
```python
def add(a, b):
    result = a + b
    logging.info(f"Adding {a} + {b} = {result}")  # 多余！
    return result
```

不要加功能、不要重构其他代码、不要在测试之外 "改进"。

**GREEN 阶段作弊是允许的：**
- 硬编码返回值
- 复制粘贴
- 重复代码
- 跳过边界情况

我们会在 REFACTOR 阶段修正。

### 验证 GREEN —— 看着它通过

**强制。**

```bash
# 运行特定测试
pytest tests/test_feature.py::test_specific_behavior -v

# 再跑所有测试检查回归
pytest tests/ -q
```

确认：
- 测试通过
- 其他测试仍然通过
- 输出干净（没有错误、警告）

**测试失败？** 修代码，不要改测试。

**其他测试失败？** 立即修回归。

### REFACTOR —— 清理

仅在 green 之后：
- 移除重复
- 改进命名
- 抽取辅助函数
- 简化表达式

全程保持测试 green。不要加行为。

**重构时测试失败：** 立即回退。迈更小的步子。

### 重复

下一个失败测试对应下一个行为。一次一个循环。

## 避免水平切片

**不要**先写完所有测试再写所有实现。那是水平切片：RED 变成 "写一堆想象出来的测试"，GREEN 变成 "让这堆通过"。它会产生脆弱的测试，因为测试是在实现还没教会你哪些行为和接口真正重要之前就设计好的。

改用垂直曳光弹：

```text
WRONG:
  RED:   test1, test2, test3, test4
  GREEN: impl1, impl2, impl3, impl4

RIGHT:
  RED→GREEN: test1→impl1
  RED→GREEN: test2→impl2
  RED→GREEN: test3→impl3
```

一条曳光弹是一个端到端的行为切片。它证明路径走得通，教会你接口，并让下一个测试扎根于你刚学到的东西。

## 为什么顺序很重要

**"我之后写测试验证它能不能用"**

代码之后写的测试会立即通过。立即通过什么都证明不了：
- 可能测错了东西
- 可能测的是实现，不是行为
- 可能漏掉你忘记的边界情况
- 你从没看到它抓住 bug

测试先行强制你看到测试失败，证明它确实测了点东西。

**"我已经手动测过所有边界情况了"**

手动测试是临时的。你以为测全了，但是：
- 没有记录测了什么
- 代码变了没法重跑
- 压力下容易忘情况
- "我试的时候能用" ≠ 全面

自动化测试是系统化的。它们每次都以同样方式运行。

**"删掉 X 小时的工作太浪费"**

沉没成本谬误。时间已经花掉了。你现在的选择：
- 删除并用 TDD 重写（高置信）
- 留着它并事后补测试（低置信，大概率有 bug）

真正的"浪费"是留下你无法信任的代码。

**"TDD 太教条，务实意味着变通"**

TDD 本身就是务实的：
- 在提交前发现 bug（比事后调试快）
- 防止回归（测试立即抓住破坏）
- 记录行为（测试展示如何使用代码）
- 使重构成为可能（放心改，测试抓住破坏）

"务实"的捷径 = 在生产环境调试 = 更慢。

**"事后测试能达到同样目的——重要的是精神不是仪式"**

不。事后测试回答 "这做了什么？" 先行测试回答 "这应该做什么？"

事后测试受你的实现偏见影响。你测的是你构建的，而不是要求的。先行测试强制你在实现之前发现边界情况。

## 常见的自我合理化

| 借口 | 现实 |
|--------|---------|
| "太简单不用测" | 简单代码会坏。测试只要 30 秒。 |
| "我之后再测" | 立即通过的测试什么都证明不了。 |
| "事后测试能达到同样目的" | 事后测试 = "这做了什么？" 先行测试 = "这应该做什么？" |
| "已经手动测过了" | 临时 ≠ 系统化。没记录、没法重跑。 |
| "删掉 X 小时太浪费" | 沉没成本谬误。留着未验证代码是技术债。 |
| "留作参考，先写测试" | 你会改造它。那就是事后测。删除就是删除。 |
| "需要先探索" | 没问题。把探索的丢掉，用 TDD 重新开始。 |
| "测试难写 = 设计不清" | 听测试的。难测 = 难用。 |
| "TDD 会拖慢我" | TDD 比调试快。务实 = 测试先行。 |
| "手动测更快" | 手动测证明不了边界情况。每次改动都要重测。 |
| "现有代码没有测试" | 你在改进它。为你触碰的代码加测试。 |

## 危险信号 —— 停下并从头来

如果你发现自己在做以下任何一项，删掉代码，用 TDD 重启：

- 先写代码再写测试
- 实现之后才写测试
- 首次运行测试立即通过
- 说不出测试为什么失败
- 测试"以后"再加
- 自我合理化"就这一次"
- "我已经手动测过了"
- "事后测试能达到同样目的"
- "留作参考"或"改造现有代码"
- "已经花了 X 小时，删掉太浪费"
- "TDD 太教条，我是务实的"
- "这次不一样，因为……"

**以上全部意味着：删除代码。用 TDD 从头来。**

## 验证清单

把工作标记为完成之前：

- [ ] 每个新函数/方法都有测试
- [ ] 实现前看到每个测试失败
- [ ] 每个测试都因预期原因失败（功能缺失，而非拼写错误）
- [ ] 为每个测试写了让其通过的最小代码
- [ ] 所有测试通过
- [ ] 输出干净（没有错误、警告）
- [ ] 测试使用真实代码（仅在避不开时才用 mock）
- [ ] 覆盖了边界情况和错误情况

没法全部勾选？你跳过了 TDD。从头来。

## 卡住时

| 问题 | 解决方案 |
|---------|----------|
| 不知道怎么测 | 写出你心目中的 API。先写断言。问用户。 |
| 测试太复杂 | 设计太复杂。简化接口。 |
| 必须 mock 一切 | 代码耦合太重。用依赖注入。 |
| 测试搭建巨大 | 抽取辅助函数。还是复杂？简化设计。 |

## Hermes Agent 集成

### 运行测试

每一步都用 `terminal` 工具运行测试：

```python
# RED —— 验证失败
terminal("pytest tests/test_feature.py::test_name -v")

# GREEN —— 验证通过
terminal("pytest tests/test_feature.py::test_name -v")

# 完整套件 —— 验证无回归
terminal("pytest tests/ -q")
```

### 配合 delegate_task

派发子代理做实现时，在目标中强制 TDD：

```python
delegate_task(
    goal="Implement [feature] using strict TDD",
    context="""
    Follow test-driven-development skill:
    1. Write failing test FIRST
    2. Run test to verify it fails
    3. Write minimal code to pass
    4. Run test to verify it passes
    5. Refactor if needed
    6. Commit

    Project test command: pytest tests/ -q
    Project structure: [describe relevant files]
    """,
    toolsets=['terminal', 'file']
)
```

### 配合 systematic-debugging

发现 bug？写一个复现它的失败测试。遵循 TDD 循环。测试既证明修复有效，又防止回归。

永远不要在没有测试的情况下修 bug。

## 测试反模式

- **测的是 mock 行为而非真实行为** —— mock 应验证交互，而不是替换被测系统
- **测的是实现细节** —— 测行为/结果，而不是内部方法调用
- **只测顺利路径** —— 总要测边界情况、错误、临界值
- **脆弱的测试** —— 测试应验证行为而非结构；重构不应破坏它们

## 最终规则

```
生产代码 → 存在先失败过的测试
否则 → 不是 TDD
```

没有用户的明确许可，没有例外。
