# 贡献指南

感谢你愿意改进 AgentServer。无论是问题报告、文档、测试还是代码贡献都很欢迎。

## 开始之前

- 搜索已有 Issue 和 Pull Request，避免重复工作。
- 较大的功能、协议或架构调整请先创建 Issue，说明问题、目标和备选方案。
- 安全漏洞请遵循 [安全策略](SECURITY.md) 私下报告，不要公开披露。
- 参与本项目即表示同意遵守 [行为准则](CODE_OF_CONDUCT.md)。
- 提交贡献即表示你有权提交相关内容，并同意该贡献按本仓库的
  [MIT License](LICENSE) 授权。

## 报告问题

请使用仓库提供的 Issue 模板，并尽量包含：

- AgentServer commit SHA 或版本；
- 操作系统、浏览器、Python、Node.js 和 frp 版本；
- 可复现的最小步骤、预期结果和实际结果；
- 已脱敏的日志、截图或配置片段。

不要提交 FRP Token、管理员密码、Cookie、私钥、完整 `known_hosts`、生产 IP 清单或其他
敏感信息。

## 本地开发

Fork 仓库后，从最新 `main` 创建分支：

```bash
git clone git@github.com:YOUR_ACCOUNT/AgentServer.git
cd AgentServer
git remote add upstream https://github.com/ds199895/AgentServer.git
git fetch upstream
git switch -c feat/short-description upstream/main
```

安装和启动步骤见 [README](README.md#本地安装与开发)。建议使用以下分支前缀：

- `feat/`：新功能；
- `fix/`：问题修复；
- `docs/`：文档；
- `test/`：测试；
- `refactor/`：不改变行为的重构。

## 代码约定

- 保持改动聚焦，避免在同一个 PR 中混入无关格式化或重构。
- 后端沿用现有 FastAPI、类型标注和 `unittest` 风格。
- 前端沿用现有 React、TypeScript、Tailwind CSS 和组件结构。
- 新增或修改行为时同步添加测试；修改用户流程时优先补充浏览器契约测试。
- 不要修改或提交真实 `.env`、Token、密钥、数据库、日志、依赖目录或本地发布制品。
- 数据格式、API、WebSocket 消息或环境变量发生变化时，同步更新 README 和兼容性测试。

架构图的源文件是 `deploy/agentserver.architecture.json`。不要直接手改生成的 HTML 或 SVG；
架构变化应先更新 JSON，再使用 Archify 重新验证并生成 `agentserver-architecture.html` 和
`agentserver-architecture.svg`。

## 提交前验证

至少运行与改动相关的测试。准备提交 PR 时，应完成 README 中的
[完整验证](README.md#完整验证)。常用快速检查：

```bash
./.venv/bin/python -m unittest discover -s tests -v
npm --prefix frontend run test:layout
npm --prefix frontend run build
bash -n scripts/*.sh
git diff --check
```

## Commit 和 Pull Request

Commit 标题建议使用简洁的祈使语气，也可以使用 Conventional Commits：

```text
feat: add terminal agent detection
fix: preserve mobile keyboard focus
docs: clarify FRP token setup
```

Pull Request 应：

- 说明要解决的问题及实现方式；
- 关联对应 Issue，例如 `Closes #123`；
- 列出已执行的测试；
- UI 变化附带截图或短视频；
- 明确兼容性、配置、数据库或部署影响；
- 确认未包含任何敏感信息。

维护者可能要求拆分过大的 PR、补充测试或调整设计。所有 CI 检查通过后才会进入合并评估。
贡献合并后，GitHub 会自动将提交作者收录到项目的
[Contributors](https://github.com/ds199895/AgentServer/graphs/contributors) 页面；多人协作时请在
Commit 中保留正确的 `Co-authored-by` 信息。
