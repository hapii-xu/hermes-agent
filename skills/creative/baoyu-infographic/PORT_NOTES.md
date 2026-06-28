# 移植说明 — baoyu-infographic

从 [JimLiu/baoyu-skills](https://github.com/JimLiu/baoyu-skills) v1.56.1 移植而来。

## 与上游版本的差异

仅修改了 `SKILL.md`。全部 45 个参考文件均为原样复制。

### SKILL.md 的适配

| 变更项 | 上游版本 | Hermes 版本 |
|--------|----------|--------|
| 元数据命名空间 | `openclaw` | `hermes` |
| 触发方式 | `/baoyu-infographic` 斜杠命令 | 自然语言技能匹配 |
| 用户配置 | EXTEND.md 文件（项目/用户/XDG 路径） | 已移除 — 不属于 Hermes 基础设施 |
| 用户提问 | `AskUserQuestion`（批量） | `clarify` 工具（一次一个） |
| 图像生成 | baoyu-imagine（Bun/TypeScript） | `image_generate` 工具 |
| 平台支持 | Linux/macOS/Windows/WSL/PowerShell | 仅 Linux/macOS |
| 文件操作 | Bash 命令 | Hermes 文件工具（write_file、read_file） |

### 保留的内容

- 全部布局定义（21 个文件）
- 全部风格定义（21 个文件）
- 核心参考文件（analysis-framework、base-prompt、structured-content-template）
- 推荐组合表
- 关键词快捷方式表
- 核心原则与工作流结构
- 作者、版本、主页归属信息

## 与上游同步

拉取上游更新：
```bash
# 比较版本
curl -sL https://raw.githubusercontent.com/JimLiu/baoyu-skills/main/skills/baoyu-infographic/SKILL.md | head -5
# 查找 version: 行

# 对比参考文件
diff <(curl -sL https://raw.githubusercontent.com/.../references/layouts/bento-grid.md) references/layouts/bento-grid.md
```

参考文件可以直接覆盖（它们与上游一致）。SKILL.md 必须手动合并，因为它包含 Hermes 特有的适配内容。
