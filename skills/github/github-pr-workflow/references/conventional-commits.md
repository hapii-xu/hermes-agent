# 约定式提交（Conventional Commits）速查

格式：`type(scope): description`

## 类型（Types）

| 类型 | 何时使用 | 示例 |
|------|------------|---------|
| `feat` | 新功能或新能力 | `feat(auth): add OAuth2 login flow` |
| `fix` | bug 修复 | `fix(api): handle null response from /users endpoint` |
| `refactor` | 代码重构，不改变行为 | `refactor(db): extract query builder into separate module` |
| `docs` | 仅文档 | `docs: update API usage examples in README` |
| `test` | 新增或更新测试 | `test(auth): add integration tests for token refresh` |
| `ci` | CI/CD 配置 | `ci: add Python 3.12 to test matrix` |
| `chore` | 维护、依赖、工具链 | `chore: upgrade pytest to 8.x` |
| `perf` | 性能改进 | `perf(search): add index on users.email column` |
| `style` | 格式化、空白、分号 | `style: run black formatter on src/` |
| `build` | 构建系统或外部依赖 | `build: switch from setuptools to hatch` |
| `revert` | 撤销之前的提交 | `revert: revert "feat(auth): add OAuth2 login flow"` |

## 作用域（scope，可选）

代码库区域的简短标识符：`auth`、`api`、`db`、`ui`、`cli` 等。

## 破坏性变更（Breaking Changes）

在 type 后加 `!`，或在页脚加 `BREAKING CHANGE:`：

```
feat(api)!: change authentication to use bearer tokens

BREAKING CHANGE: API endpoints now require Bearer token instead of API key header.
Migration guide: https://docs.example.com/migrate-auth
```

## 多行正文（Body）

72 字符换行。多项改动用项目符号：

```
feat(auth): add JWT-based user authentication

- Add login/register endpoints with input validation
- Add User model with argon2 password hashing
- Add auth middleware for protected routes
- Add token refresh endpoint with rotation

Closes #42
```

## 关联 Issue

在提交正文或页脚中：

```
Closes #42          ← 合并后关闭该 issue
Fixes #42           ← 效果相同
Refs #42            ← 仅引用，不关闭
Co-authored-by: Name <email>
```

## 快速决策指南

- 新增了东西？→ `feat`
- 某处坏了并修复了？→ `fix`
- 只改了代码的组织方式，没改它做什么？→ `refactor`
- 只动了测试？→ `test`
- 只动了文档？→ `docs`
- 更新了 CI/CD 流水线？→ `ci`
- 更新了依赖或工具链？→ `chore`
- 让某处更快了？→ `perf`
