# ComfyUI 技能测试

覆盖本技能脚本的 pytest 套件。纯标准库单元测试无需任何设置即可运行；云端集成测试需要 Comfy Cloud API key。

## 运行

```bash
# 仅单元测试（无需网络）— 不到 1 秒运行完成
python3 -m pytest tests/ -c tests/pytest.ini -o addopts="-p no:xdist"

# 包括云端集成测试
COMFY_CLOUD_API_KEY="comfyui-..." python3 -m pytest tests/ \
  -c tests/pytest.ini -o addopts="-p no:xdist"

# 仅云端测试
COMFY_CLOUD_API_KEY="comfyui-..." python3 -m pytest tests/test_cloud_integration.py \
  -c tests/pytest.ini -o addopts="-p no:xdist" -v
```

`-c` 和 `-o` 覆盖将此套件与任何父级
`pyproject.toml` 的 pytest 配置（例如父级仓库的 `-n auto`）隔离。

## 测试文件

| 文件 | 覆盖范围 |
|------|----------|
| `test_common.py` | 云端检测、URL 路由、格式验证、embeddings、路径、种子、模型列表解析、文件夹别名 |
| `test_extract_schema.py` | 连接追踪、正向/负向提示检测、去重逻辑、embedding 依赖 |
| `test_run_workflow.py` | 参数注入（含 -1 种子、链接拒绝）、输出下载遍历、runner 构造 |
| `test_check_deps.py` | 模型名模糊匹配、安装命令建议 |
| `test_cloud_integration.py` | 实时云端 API 契约测试（无 API key 时自动跳过） |

## 添加测试

当你更改脚本时：

1. 如果更改是纯逻辑（云端检测、解析等），添加单元测试
2. 如果更改依赖云端 API 行为，添加云端集成测试
   （使用 `pytestmark = pytest.mark.cloud` 以便在没有 key 时自动跳过）
3. 工作流 fixtures 位于 `conftest.py`（`sd15_workflow`、`flux_workflow`、
   `video_workflow`）

## 为什么要显式 `-c` / `-o`？

父级 hermes-agent 仓库的 `pyproject.toml` 默认启用 `pytest-xdist`
（`-n auto`）。此套件足够小，并行化不值得其复杂性，且 pytest-xdist 并不总是安装在用户的环境中。`-c tests/pytest.ini -o addopts="-p no:xdist"` 标志使套件无论父级项目配置如何都以相同方式运行。
