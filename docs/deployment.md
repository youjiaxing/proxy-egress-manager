# 1Panel 部署

## 前提与交付边界

- Linux Docker Engine、Compose v2、可用的 1Panel 反向代理；建议至少 1 CPU / 1 GiB 可用内存，实际容量压测后确定，非性能保证。
- 现有业务 Compose 已加入同一个外部 Docker network。这里只需要该 network 的名字，不需要业务服务名、镜像或账号配置。
- 一个用于管理网页的域名及 HTTPS 证书，能访问机场订阅与身份/国家查询服务。
- 获取项目版本镜像与固定的 `metacubex/mihomo:v1.19.33`；Compose 已固定经 registry 核对的多架构摘要。目标为 Linux amd64 / arm64，未经该架构实际构建运行测试不宣称已支持。

`v0.3.7` 已公开发布，默认使用以下发布流程生成的固定镜像：

```text
ghcr.io/youjiaxing/proxy-egress-manager:v0.3.7@sha256:dda0415c01baa8b71e3558315bb8a077a2b8a95e49f8063d9a4730be7ac8fe3a
```

源码与 Release：https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.7 。原生 Linux amd64 / arm64 CI 均通过；真实 VPS、1Panel/HTTPS 和真实订阅仍须现场验收。不要使用未经核验的陌生镜像站。

v0.2 新增独立诊断核心，使用专用网络与配置卷，无宿主端口，默认限制 128 MiB / 0.25 CPU。跨 v1/v2 升级与回退必须按 [数据迁移说明](quality.md#v1--v2-升级与降级) 执行，不能只改镜像。

## 首次部署

1. 将项目源码（本地构建模式）或版本对应的 `compose.yaml`、`.env.example` 放入 1Panel Compose 项目目录。复制 `.env.example` 为 `.env`，文件权限设为仅管理员可读。
2. 设置：
   - `PEM_IMAGE`：已发布的版本+摘要；本地构建可用 `proxy-egress-manager:local`。
   - `BUSINESS_NETWORK`：已有共享网络名。Compose 不创建/重命名业务网络。
   - `PUBLIC_URL`：准确的管理 origin，例如 `https://egress.example.com`，不带路径。浏览器 Origin 与其不一致时拒绝写操作。
   - `CORE_SECRET`：独立随机秘密，至少 24 字符。
   - `ADMIN_PASSWORD`：独有管理员密码，至少 16 字符；无默认可登录密码。
   - `WEB_HOST_PORT` 默认 18080；仅绑定宿主 `127.0.0.1`，不开放公网代理端口。
3. 在 1Panel 构建/启动 Compose。命令等价于：

```sh
# 未发布镜像时，仅本地构建项目镜像：
docker compose -f compose.yaml -f compose.build.yaml build
# 已有镜像则先拉取；拉取失败不要先停已有运行版本：
docker compose pull
# 首次启动或按维护流程更新：
docker compose up -d --no-build --wait --wait-timeout 90
```

本地构建未发布的项目镜像时，不要对其强行 `pull`；先单独 `docker compose pull mihomo`，再构建并启动。

生产配置 `compose.yaml` 不含源码构建指令；只有显式加入 `compose.build.yaml` 才能构建。1Panel 项目名可能覆盖文件内默认值：首次部署须记录实际 Compose 项目名，后续备份、恢复和升级始终使用同一个 `-p`，否则可能访问另一套空数据卷。

`init-core` 是一次性初始化任务，生成固定 provider、select 组和监听配置后以退出码 0 退出，Mihomo 再启动。此服务显式禁用镜像继承的网页健康检查；管理服务仍保留健康检查。已退出是初始化成功后的正常状态，部分 1Panel 版本可能仍按未运行容器显示“部分停止”，不能仅凭该汇总判断故障。应核对初始化退出码为 0、Mihomo 正在运行和管理服务健康。应用重试核心就绪，在验证前不放行。`depends_on` 只控制初次启动顺序，运行中保护由网关授权实现。

4. 在 1Panel 网站/反代配置中，设置域名 HTTPS 反向代理至 `http://127.0.0.1:18080`（同机部署）。如果 1Panel 反代运行在独立容器中，容器的 `127.0.0.1` 不是宿主：将其加入业务网络，反代到 `http://proxy-egress-manager:8080`，不要为此把管理端口开放到公网。保留原始 Host；应用不信任 X-Forwarded-For 来绕过登录限流。
5. 登录网页，在“订阅来源”添加 HTTPS URL，或直接粘贴 Mihomo YAML `proxies:`、URI 列表/base64。URL 不跟随跳转，避免秘密被转发到其他站；有跳转的订阅请使用最终 HTTPS URL。完整 Clash/Mihomo 配置只提取 `proxies`，不导入其中的规则、DNS、控制接口等配置。
6. 查看出口组。完成 TCP 连通、TCP 美国国家验证与 TCP 组间独立性验证后，才显示“可用”。UDP 仅展示观测，不影响放行。在业务中分别填写固定 SOCKS5 地址。

## 网络与持久化

- `business`：现有外部网络，仅项目网关接入。SOCKS 无用户名密码，依赖该网络的成员信任边界；不要让不受信任容器加入。任何同网容器都可能使用这些代理入口，无法按业务账号识别身份。
- `backend`：本项目专属网络，网关与 Mihomo 互通，Mihomo 需要正常外网出站。不是 `internal: true`（该选项会影响所需出站）；隔离依赖仅项目组件加入且不发布核心端口。
- `state`：SQLite 状态和维护备份；`core-config`：生成配置、provider 及核心缓存，均包含秘密。应用 UID/GID 10001，初始化复制目录的权限也归该用户。自定义 bind mount 时须预先赋予这个用户写权限。
- UDP 中继使用每关联动态端口，返回业务 TCP 连接所到达网卡地址，业务容器须能在共享网络内访问该地址的 UDP 端口。不得经公网端口映射使用此入口；容器内网不要求固定 IP。

默认预留 16 个订阅、32 个分组；可通过容量变量扩大，但涉及核心静态入口配置，必须按维护流程停服重建，不能在线随意重载。业务组端口 `1080+组槽位`，核心内部端口 `20000+组槽位`，候选固定 20000。删除后槽位可重用。

## 首次现场验收

确认健康页反映调度而非仅网页进程。检查 TCP 观测有值、两组 TCP 出口集合无交集；UDP 可无响应，仅供参考；手动暂停一组应立即断其旧会话，另一组保持；钉住故障时不得换节点。实际机场/身份服务未验证成功前，不视为上线完成。

代理仍不可用时查看脱敏事件，不要直接公开 Compose 完整环境、订阅配置或备份。身份服务与机场故障的处理见安全文档。
