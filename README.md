# Proxy Egress Manager

用于 Docker 共享网络的 SOCKS5 出口分组工具。每组使用固定入口、独立验证的美国出口；正常保持，持续故障自动寻找独立备用，没有备用则暂停。支持 TCP CONNECT 和 UDP ASSOCIATE、网页管理、多订阅、钉住、备份恢复和 webhook 告警。

**状态：v0.1.6 已公开发布，VPS 管理面与 HTTPS 反代已验收。** 原生 Linux arm64 / amd64 远程 CI、镜像构建与 Compose 启动均通过；公网端口边界与登录保护已独立验证，详细范围见 [验证记录](docs/verification.md)。已接入用户订阅；独立美国出口放行仍受现场探测结果限制，公共身份服务可靠性仍需验证。不得仅凭节点名称或 `udp: true` 放行。

## 组件

- `gateway/`：自建 SOCKS5 网关。每次 TCP 转发写入和双向 UDP 数据报都检查单调时钟、调度期限、组身份期限和授权世代；没有内核离线持续转发。
- `scheduler/`：SQLite 持久化、单控制者、Mihomo provider、独立候选通道和真实出口探测。
- `web/`：原生 HTML/CSS/JS 管理台及 aiohttp API。HTTPS origin 校验、HttpOnly 会话、CSRF 和管理员凭据保护。
- `compose.yaml`：项目应用、初始化和独立上游 Mihomo。只将管理网页映射到宿主回环地址，代理入口仅在业务网络提供。

初始业务入口：

```text
socks5://proxy-egress-manager:1081
socks5://proxy-egress-manager:1082
```

这些地址只在加入同一 Docker network 的容器中可用。单组端口固定；节点切换不改变业务入口。不管理业务账号密码。

## 部署

完整步骤：[1Panel 部署](docs/deployment.md)、[升级与恢复](docs/operations.md)、[安全与保证范围](docs/security.md)、[设计与协议](docs/architecture.md)。

公开源码：[GitHub](https://github.com/youjiaxing/proxy-egress-manager)，当前版本：[v0.1.6](https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.1.6)。GHCR 镜像已验证匿名拉取，设置 `PEM_IMAGE` 为：

```text
ghcr.io/youjiaxing/proxy-egress-manager:v0.1.6@sha256:6d270529f81953af4fa4714556cf6e2a7546775d060cc5d3fa7500d78be388bc
```

优先部署固定版本与摘要。自行构建时使用 `docker compose -f compose.yaml -f compose.build.yaml build`；生产配置本身不构建源码，Mihomo 仍使用 Compose 中的固定上游摘要。

复制 `.env.example` 到 `.env`，设置独有秘密、HTTPS origin、已有业务 network 和镜像引用。示例没有默认密码。不要将真实 `.env`、数据卷或备份加入 Git。

## 本地开发

Python 3.12（与生产镜像一致）、Node（仅用于 JS 语法验证）、可选 Docker：

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_release.py
node --check web/app.js
```

真实核心集成测试需要官方 Mihomo v1.19.33 可执行文件：

```sh
MIHOMO_BIN=/path/to/mihomo .venv/bin/python -m pytest -q
```

不指定 `MIHOMO_BIN` 时，该项会明确跳过，不能称为真实核心测试通过。集成测试使用回环地址、临时目录和本地 SOCKS 测试节点，不接触真实订阅。

仅看网页可运行 `.venv/bin/python scripts/local_demo.py`：回环 `http://127.0.0.1:18080`，测试密码 `local-preview-password-2026`，不启动调度或代理。此脚本不进入生产镜像。它不是可用代理演示，管理变更可能因没有后端失败。

## 发布与维护

源码与项目镜像默认公开，自编代码 MIT。上游和第三方依赖保留其独立许可；见 [第三方说明](docs/third-party.md)。GitHub Actions 在显式发布稳定 `vX.Y.Z` release 后运行测试并推送 GHCR，普通提交仅测试，**不会升级服务器**。首次发布、包的公开可见性与账号归属需要仓库所有者配置并核验，项目不会自行创建远程仓库。

首个公开版本为 `v0.1.0`，发布检查顺序及交付内容见 [首次发布准备](docs/release.md)。

系统不是静态 IP 服务，不保证所有网站同出口、全地址族隔离或绝不风控。隔离只覆盖配置验证目标实际观测的出口，其他目的地/地址族明确未知。业务主动直连或代理失败回退直连不在本工具控制内。
