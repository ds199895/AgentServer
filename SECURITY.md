# 安全策略

## 支持范围

安全修复面向最新发布版本和 `main` 分支。旧 commit、个人 Fork 或已被替换的部署版本通常
不会单独维护；收到报告后，维护者会确认实际受影响范围。

## 私下报告漏洞

请不要为尚未修复的漏洞创建公开 Issue。优先使用 GitHub 仓库 Security 页面中的
“Report a vulnerability”提交私密报告：

<https://github.com/ds199895/AgentServer/security/advisories/new>

如果该入口暂不可用，请通过仓库所有者 GitHub 主页中公开的联系方式私下联系维护者。报告请
包含：

- 受影响版本或 commit SHA；
- 漏洞类型、影响和攻击前提；
- 可复现步骤或最小验证代码；
- 建议修复方式（如有）；
- 是否已经或计划向其他人披露。

不要在报告中发送真实生产 Token、密码、Cookie 或私钥。请使用最小化、可撤销的测试凭据。

## 协调披露

维护者确认问题后会评估影响、准备修复并协调披露时间。请在修复发布前避免公开细节。修复完成
后，可在征得报告者同意后于安全公告中致谢。

## 部署安全基线

- 生产入口必须使用 HTTPS，并设置 `COOKIE_SECURE=1`；
- frps Dashboard 和 AgentServer 的 18100 端口只应监听本机或受信网络；
- FRP Token、SSH 私钥和生产环境变量不得进入 Git；
- 部署 SSH key 应绑定 `scripts/github_deploy_receiver.sh` forced command；
- 定期轮换凭据、更新依赖，并检查 GitHub 安全告警。
