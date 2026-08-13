# Repository Guidelines

## Scope and Communication

默认使用中文回复。先给结论，再补充完成工作、验证结果和风险。多步骤任务在调用工具前用一两句话说明首个动作；低风险细节可自行判断，高风险配置、数据迁移、权限或行为变化必须先确认。只实现用户明确要求的内容，不顺带重构或新增功能。

## Project Layout

- `backend/api/routes/`：FastAPI 路由；路由层保持薄，只处理请求、鉴权和响应。
- `backend/indexing/`：文档解析、分块、Embedding、Milvus 写入；`backend/rag/`：检索、评分、改写和合成。
- `backend/chat/`：Agent、流式输出、会话上下文；`backend/infra/` 与 `backend/db/`：认证、缓存、数据库。
- `frontend/src/`：Vue 组件、Pinia stores、类型与 API 工具；`tests/`：后端 `unittest`；前端测试与 Store 同目录，命名为 `*.spec.ts`。

存在 `.codegraph/` 时，理解或定位代码前优先使用 CodeGraph；只在索引未覆盖配置、文档或刚修改文件时使用 `rg`/直接读取。

## Development and Validation

```powershell
uv sync
pwsh .\scripts\supermew.ps1 -Action start
uv run python -m unittest discover -s tests
Set-Location frontend; npm run build; npm test
```

代码改动后按需执行：受影响的单元测试 -> 类型检查或构建 -> 最小接口/UI 冒烟检查。后端测试必须可在 60 秒内完成；单元测试应 mock Milvus、模型 API、MinerU 等外部服务。无法验证时，明确说明原因和下一步检查方式。

## Implementation Rules

Python 使用 4 空格、`snake_case` 函数/模块、`PascalCase` 类；Vue 使用 `<script setup lang="ts">`、`PascalCase.vue` 组件和 `camelCase` 变量。新增公共函数写类型标注，优先早返回、短函数和命名常量；注释解释取舍，不复述代码。

先追踪根因，不用吞异常、伪造成功或新建隐蔽 fallback 掩盖问题。已有的显式降级行为必须保留可观测性，并在 trace、日志或错误中说明是否生效。共享校验、权限、路由、缓存和 API schema 发生变化时，先定义唯一不变量，避免重复实现和第二数据源。

## Security and Review

密钥只放 `.env`，不得提交、记录或展示。边界处校验文件、请求与环境变量；数据库查询必须参数化。提交前执行 `git diff --check` 并检查重复逻辑、死代码、未说明的行为变化、弱测试与安全回归。

当前仓库没有提交历史。新提交使用命令式 Conventional Commit，例如 `feat: add MinerU parser`；PR 说明影响模块、配置或迁移、验证命令，并为前端改动附截图。
