# CI 故障排查速查

常见的 CI 失败模式，以及如何从日志中诊断它们。

## 阅读 CI 日志

```bash
# 用 gh
gh run view <RUN_ID> --log-failed

# 用 curl —— 下载并解压
curl -sL -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/actions/runs/<RUN_ID>/logs \
  -o /tmp/ci-logs.zip && unzip -o /tmp/ci-logs.zip -d /tmp/ci-logs
```

## 常见失败模式

### 测试失败

**日志中的特征：**
```
FAILED tests/test_foo.py::test_bar - AssertionError
E       assert 42 == 43
ERROR tests/test_foo.py - ModuleNotFoundError
```

**诊断：**
1. 从 traceback 中找到测试文件和行号
2. 用 `read_file` 读取失败的测试
3. 判断是代码里的逻辑错误，还是过时的测试断言
4. 注意 `ModuleNotFoundError` —— 通常是 CI 里缺少依赖

**常见修复：**
- 更新断言以匹配新的预期行为
- 把缺失的依赖加到 requirements.txt / pyproject.toml
- 修复不稳定的测试（加重试、mock 外部服务、修竞态条件）

---

### Lint / 格式化失败

**日志中的特征：**
```
src/auth.py:45:1: E302 expected 2 blank lines, got 1
src/models.py:12:80: E501 line too long (95 > 88 characters)
error: would reformat src/utils.py
```

**诊断：**
1. 读取被提及的具体 文件:行号
2. 判断是哪个 linter 在报错（flake8、ruff、black、isort、mypy）

**常见修复：**
- 在本地跑格式化工具：`black .`、`isort .`、`ruff check --fix .`
- 编辑文件修复具体的样式违规
- 如果用 `patch`，确保匹配已有的缩进风格

---

### 类型检查失败（mypy / pyright）

**日志中的特征：**
```
src/api.py:23: error: Argument 1 to "process" has incompatible type "str"; expected "int"
src/models.py:45: error: Missing return statement
```

**诊断：**
1. 读取被提及行的文件
2. 检查函数签名和传入的内容

**常见修复：**
- 加类型转换
- 修函数签名
- 作为最后手段加 `# type: ignore` 注释（并附说明）

---

### 构建 / 编译失败

**日志中的特征：**
```
ModuleNotFoundError: No module named 'some_package'
ERROR: Could not find a version that satisfies the requirement foo==1.2.3
npm ERR! Could not resolve dependency
```

**诊断：**
1. 检查 requirements.txt / package.json 里是否缺少或存在不兼容的依赖
2. 对比本地和 CI 的 Python/Node 版本

**常见修复：**
- 把缺失的依赖加到 requirements 文件
- 固定兼容的版本
- 更新 lockfile（`pip freeze`、`npm install`）

---

### 权限 / 鉴权失败

**日志中的特征：**
```
fatal: could not read Username for 'https://github.com': No such device or address
Error: Resource not accessible by integration
403 Forbidden
```

**诊断：**
1. 检查工作流是否需要特殊权限（token scopes）
2. 检查 secrets 是否已配置（缺失 `GITHUB_TOKEN` 或自定义 secrets）

**常见修复：**
- 给工作流 YAML 加 `permissions:` 块
- 核实 secrets 是否存在：`gh secret list` 或检查仓库设置
- 对 fork PR：某些 secrets 按设计就不可用

---

### 超时失败

**日志中的特征：**
```
Error: The operation was canceled.
The job running on runner ... has exceeded the maximum execution time
```

**诊断：**
1. 检查是哪一步超时
2. 找死循环、卡住的进程、或缓慢的网络调用

**常见修复：**
- 给具体步骤加超时：`timeout-minutes: 10`
- 修底层的性能问题
- 拆分成并行作业

---

### Docker / 容器失败

**日志中的特征：**
```
docker: Error response from daemon
failed to solve: ... not found
COPY failed: file not found in build context
```

**诊断：**
1. 检查 Dockerfile 中失败的那一步
2. 核实所引用的文件在仓库中确实存在

**常见修复：**
- 修 COPY/ADD 命令里的路径
- 更新基础镜像 tag
- 把缺失的文件加到 `.dockerignore` 排除项，或从中移除

---

## 自动修复决策树

```
CI Failed
├── Test failure
│   ├── Assertion mismatch → update test or fix logic
│   └── Import/module error → add dependency
├── Lint failure → run formatter, fix style
├── Type error → fix types
├── Build failure
│   ├── Missing dep → add to requirements
│   └── Version conflict → update pins
├── Permission error → update workflow permissions (needs user)
└── Timeout → investigate perf (may need user input)
```

## 修复后重新运行

```bash
git add <fixed_files> && git commit -m "fix: resolve CI failure" && git push

# 然后监控
gh pr checks --watch 2>/dev/null || \
  echo "Poll with: curl -s -H 'Authorization: token ...' https://api.github.com/repos/.../commits/$(git rev-parse HEAD)/status"
```
