# 本地验证记录

日期：2026-11-06。环境：macOS arm64，Python 3.12.16，Node 22.18.0，Docker Desktop Engine 27.3.1 / Compose 2.30.3，官方 Mihomo v1.19.33。未使用真实订阅、业务账号或远程 VPS。

## 已执行

- `MIHOMO_BIN=/tmp/proxy-egress-mihomo .venv/bin/python -m pytest -q`：37 项通过，无跳过。包括真实主程序启动/登录/健康/无合格节点拒绝/优雅停止，实际 Mihomo TCP/UDP 链路，以及 source 3 更新和无效 provider 更新时原连接保持。
- 数据路径：TCP 双向与 UDP ASSOCIATE 实际往返、UDP 控制关闭、无后台扫描时授权到期拒绝、跨组撤销隔离、碎片/外来 UDP 拒绝、清理未结束拒绝新租约。
- 控制状态：正常保持、新订阅不换、身份过期不盲切且仍可独立故障切换、钉住、无备用暂停、完整/部分身份碰撞、非美国/无效地址、良性换址旧占用保留、旧观测不得刷新期限、重启不恢复授权、非法设置不部分应用。
- 探测链路：本地 HTTP 身份和国家服务 + SOCKS + STUN，分别观测不同 TCP/UDP IP；任一必需身份目标失败不标合格。该测试验证协议实现，不证明公共服务对机场可靠。
- 恢复：运行中锁拒绝离线维护、备份 0600、离线备份与恢复、控制锁卡住仍到期关闭、恢复失败保持维护态、磁盘标记跨重启保留且拒绝不一致备份、维护态普通操作不能绕过、坏备份提前拒绝。
- 管理面：未认证拒绝、Origin/CSRF、会话退出、状态秘密隐藏、无默认口令、反代共享地址失败计数不锁正确管理员。
- `node tests/ui_dom.cjs`：登录、两组卡片、各管理标签、订阅弹窗、退出的 DOM 模拟通过。使用独立回环临时服务与临时数据，没有真实代理租约；不算真实浏览器或视觉测试。
- `node --check web/app.js`：通过。
- `docker compose config --quiet`：使用虚构环境值通过，不连接引擎，不代表容器启动通过。
- `scripts/check_release.py`：运行产物排除、第三方 Actions 固定 SHA、无 Docker socket/privileged/公网核心/公网 SOCKS 映射检查通过。
- `pip check`：通过。运行依赖精确固定，已核对许可证元数据；按完整固定运行依赖清单执行 `pip-audit --no-deps --disable-pip -r requirements.txt`，未发现已知漏洞。此结果不是对未来漏洞或基础操作系统镜像的保证。
- Docker Registry 核查：固定 Mihomo 与 Python 基础镜像摘要，manifest 包含 amd64 / arm64；另核对 Mihomo 镜像 entrypoint 为 `/mihomo`、工作目录 `/`。

## 本轮容器与浏览器部署验收

- Docker 原故障是虚拟机停止后后台进程残留，API socket 请求超时。退出残留后台并正常启动 Docker Desktop 后，`/_ping` 返回 `OK`；没有重置数据或改动既有镜像。拉取时使用独立临时 Docker CLI 配置，避开卡住的凭据助手，没有修改用户凭据配置。
- 按仓库 Dockerfile 分别构建 Linux arm64 / amd64 镜像成功。两架构均在实际镜像内安装临时测试依赖、挂载测试目录，配合同架构的官方 Mihomo 执行全部测试：arm64 **37 passed / 3.99s**；amd64 **37 passed / 7.61s**，均无跳过。amd64 使用 Docker Desktop 转译，不等于原生 amd64 VPS 验收。
- 首次 Compose 启动发现 Mihomo 的行内 `tmpfs` 列表未引用，逗号被解析成第二个挂载项，报 `invalid mount path: 'mode=1777'`。已加引号修复；实际启动与 `up --wait --wait-timeout 90` 均通过。CI 新增独立网络与项目的实际 Compose 启动/健康检查和退出清理；远程 Actions 尚未执行。
- 使用独立 `pem-acceptance` 项目、业务测试网络和空卷，完成 init-core → Mihomo → 应用启动。应用健康检查通过；应用与核心均为 UID 10001、只读根文件系统，正确挂载 tmpfs；只有管理端口映射至宿主回环地址。容器内核心 DNS 解析正常，业务别名可解析，两组未授权 SOCKS 连接立即关闭。
- 在线运行维护命令按预期被文件锁拒绝且未产生备份。停止应用后，通过 Compose 一次性容器完成离线备份与恢复；备份权限为 0600，恢复维护标记已清除。重启后浏览器创建的第三组仍存在，全部组 `lease_valid=false`，调度健康恢复，旧登录会话失效。
- 真实内置 Chromium 连接实际容器，完成登录、新建第三组并等待后台任务完成、五个管理标签、订阅弹窗打开/取消、设置原值提交、退出检查。桌面 1280×800 与 390×844 CSS 视口均检查了页面宽度，未见横向溢出。测试 HTTP 开关仅置于临时回环验收 override，生产 Compose 没有启用。
- 浏览器 snapshot 连续超时，以上浏览器结论来自真实页面 DOM、交互及服务端状态；尚无截图视觉审查结论，不等同于完整视觉验收。
- 初次 Linux 测试因挂载目录与测试中硬编码 `.venv/bin/python` 而有三项环境失败。调整测试挂载至 `/app/tests`，离线维护测试改用 `sys.executable`，随后两架构完整通过；未通过跳过测试规避问题。

## 独立审查

- 数据路径与调度 Reviewer：指出部分身份碰撞及非全球地址遗漏，已修复。针对复核确认这些修复、清理中拒绝新租约、观测年龄、候选业务入口复验和完整有界下载无残留实质问题。
- 管理面与部署 Reviewer：指出恢复崩溃屏障缺失和反代登录锁定问题，已修复；同一 Reviewer 针对性复核确认闭合。
- 两条审查中途曾遭遇模型服务 503；恢复同一 Reviewer 后完成复核，没有以主会话自审替代独立结果。

## 现场验证边界

- 原生 amd64 / arm64 GitHub Actions、GHCR 发布及匿名拉取现已通过，见文末发布记录。真实服务器 HTTPS/反代、1Panel、机场订阅/身份端点及告警渠道尚未接入；不宣称 VPS 上线完成或真实出口足够独立。
- TCP/UDP 运行测试使用隔离 fixture，不能证明机场节点及公共身份服务现场稳定性。真实订阅与服务器上线按单独授权执行。

## 发布前本地收尾

- 初始化本地 Git，原创 MIT 及第三方说明随源码保留；`.env.*` 默认忽略，仅保留空白 `.env.example`。暂存文件常见高风险凭据模式及运行产物检查无命中，不代表完整秘密审计。
- README 与实际部署验收状态对齐，新增 `release.md`，以 `v0.1.0` 为候选，明确远程 CI、首次 GHCR 公开拉取核验、手动升级及首版没有历史镜像可回滚。未创建远程仓库、版本标签或公开镜像。
- 回归：macOS 实际 Mihomo 的 37 项测试再次全部通过（1.91 秒）；UI DOM 模拟、JS 语法、依赖一致性、发布边界与工作流 YAML 解析通过。
- 本轮内置浏览器截图接口恢复，取得桌面 1280×800 登录/分组/设置、手机 CSS 390×844 设置/分组/订阅弹窗等截图；来源为隔离 UI demo，无真实代理。六张图已保持原分辨率并通过本地解码核验。截图留在本机 browser-artifacts，不加入发布源码。
- 上一轮视觉观察发现手机导航最后一个标签裁切，已改为自动换行。本轮追加九张原分辨率分段图，隔离视觉观察者完整查看全部画面，确认桌面设置中部/底部、分组底部、手机第二组、导航修复和设置底部均无重叠、容器裁切或不可读文字；滚动内容在相邻画面完整衔接。浏览器另核对 390 与 320 CSS 像素宽度无页面横向溢出，390 下五页操作按钮均在视口宽度内。此次补齐了上轮剩余视觉覆盖。

## 发布复核修正与验证

- 发布文件独立复核指出：release 未包含镜像/Compose 启动测试、测试核心与部署核心来源不一致、镜像第三方许可说明缺失、维护命令未固定项目名/旧摘要且恢复失败可能继续启动。已逐项修正，交由同一复核者针对复核。
- Release 复用同一提交的原生双架构 CI，包含宿主完整测试、镜像构建与隔离 Compose 健康检查；CI 从 Compose 固定摘要镜像提取实际 `/mihomo`，不下载未经摘要校验的独立压缩包。发布前检查既有版本，认证或网络结果不明确也拒绝；摘要与源码提交附于 Release 文件，保留 SBOM 与构建来源。远程发布尚未执行。
- 生产 `compose.yaml` 移除 build，显式源码构建位于 `compose.build.yaml`；项目镜像包含原创 LICENSE 和 THIRD-PARTY.md，去除将组合镜像整体标为 MIT 的 OCI 标签。
- 维护文档固定实际项目名、部署环境文件、旧 Compose 与镜像摘要，备份导出到卷外，恢复成功才启动。使用本地不可变镜像 ID、独立项目、空测试数据，实际完成停服、离线备份、卷外导出、按 UID 10001 保护恢复副本、只读挂载恢复、重建和健康检查。测试项目/网络/卷均已清理，不修改现有业务。
- 修改后镜像构建与 `scripts/compose_smoke.sh` 实际运行通过，许可文件在容器内存在。actionlint v1.7.7 无报错；工作流 YAML、全部 shell 步骤的 Bash 语法、生产构建隔离与发布测试依赖检查通过。
- 对发布前注册表检查做五种模拟：既有版本、认证错误、网络错误均拒绝，明确 manifest 不存在时允许。未使用真实 GHCR 凭据或执行推送。
- 本轮 macOS 真实 Mihomo 回归 37 项通过（1.93 秒），UI DOM 模拟、JS 语法及发布边界检查通过。
- 同一发布复核者针对复核确认原四项问题已闭合；另提出推送成功但附件上传失败后的恢复窗口，已补充同提交既有镜像只读摘要与源码 revision 核验、跳过构建并补传附件。再次针对复核确认此窗口无残留问题。发布文件独立复核与本地剩余视觉收尾均已完成。

## 首次公开发布验收

- 用户授权后创建公开仓库 https://github.com/youjiaxing/proxy-egress-manager ，发布源码提交 `a3b35e97ab8decc45bcf916724154cfd94dfe8c4`。
- 原生 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42215486669 ：amd64 37 项通过（4.15 秒）、arm64 37 项通过（4.25 秒），均完成镜像构建与实际 Compose 健康检查。
- Release https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.1.0 ，发布工作流 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42215650003 成功，再次执行两架构 CI 后构建推送；`deployment-reference.txt` 已附在 Release。
- 多架构镜像：`ghcr.io/youjiaxing/proxy-egress-manager:v0.1.0@sha256:e4d5423e856c3fd918e9fe6c37c373ac8b9d19c06a440c68f6fb4bf5ff59325a`。
- 通过空 Docker CLI 配置、不登录 GHCR 的客户端读取 manifest 并拉取：索引及 amd64 镜像实际下载成功。两个架构 revision 均匹配发布提交，运行 UID 均为 10001；Release 附件摘要与注册表一致，构建来源与 SBOM 信息均包含两架构。
- amd64 manifest：`sha256:6145da5e99c310a8108acba07ce41dd18c06570aeccd71005d1663aa85524951`；arm64 manifest：`sha256:b9641902c616e474520cbfe42e0bcd817e6f2e1947d24f2efe95b3fc4adf5d55`。
- 下载后的实际发布镜像再次运行完整 fixture 测试：arm64 37 项通过（4.58 秒），amd64 37 项通过（7.90 秒，本机转译）。固定索引镜像的隔离 Compose 启动与健康检查通过，退出清理全部测试容器、网络、数据卷。
- GitHub CLI 的现有令牌没有 read:packages scope，因此包元数据 API 返回 403；未申请额外权限，公开可用性由实际匿名拉取验证。未部署远程服务器或接入真实订阅。

## VPS 首次部署与公网边界验收

- 按用户授权通过 SSH 部署 `v0.1.0` 固定摘要，独立 Compose 项目为 `proxy-egress-manager`，使用原有共享网络 `sub2api-network`。应用目录位于 `/opt/1panel/docker/compose/proxy-egress-manager`；没有修改既有业务容器或业务代理设置。
- 管理域名 `https://proxy.youjiaxing.top`。复用服务器已有阿里云 DNS 账号，仅新增精确 `proxy` A 记录，未改泛域名或其他子域名。公网 DNS-over-HTTPS 验证新 A 记录生效，该名称不再继承原泛域名的 IPv6 结果。
- 通过 SSH 在 1Panel 使用的 OpenResty `conf.d` 与 `sites` 目录新增独立站点配置，未写 1Panel 数据库，因此不是面板网站列表中已登记的站点。后续应按配置文件维护，不要在 UI 重复创建同名站点。先通过 `nginx -t` 并重载，验证 HTTPS 已使用新证书、80 仅 301 跳转，再启动应用。
- 使用服务器既有 acme.sh 和阿里云 DNS challenge 申请独立证书，有效期 2026-11-06 至 2027-02-04。证书安装到新站点 ssl 目录，私钥 0600；已有 ACME cron 包含此证书，续期 hook 在配置测试通过后重载 OpenResty。未更改其他站点证书。
- 管理密码通过私密输入写入权限 0600 的服务器环境文件，没有写入聊天、Git 或日志。核心使用独立随机秘密。API Key 未提供，不需要启用 1Panel API；未修改面板认证或 MFA。
- 应用启动与健康检查通过。实际 HTTPS 登录成功，生产 Cookie 的 Secure/HttpOnly/SameSite=Strict 标志正确；无 CSRF 操作返回 403，退出后状态接口返回 401。OpenResty 登录限流实测出现 429。UID 10001、只读根文件系统与唯一 `127.0.0.1:18080` 管理映射已核对。
- 实际状态为订阅 0、节点 0、分组 2，全部组 `lease_valid=false`；未导入测试订阅或真实订阅，未让业务使用代理。
- 独立外部机器验证：https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42221203514 成功。检查可信 TLS、DNS、HTTP 跳转、未认证 API 拒绝、错误 Origin 拒绝，公网 1081/1082/9090/18080 均不可达。
- 本机网络曾对多个随机端口立即报告 TCP connect 成功，不能据此判断公网监听；上述端口结论以独立 GitHub runner 与服务器监听/映射证据为准。一次新 SSH 连接遇到不同主机密钥时严格校验拒绝，未传凭据；随后原密钥连接恢复并核对服务器公钥匹配原 known_hosts，未接受其他密钥或关闭校验。
- 公网管理面没有 MFA、SSO 或自动漏洞修复保证。现有边界是 HTTPS、独有强口令、登录限流、会话与 CSRF 防护；管理员能导出订阅秘密及配置外部目标，必须作为高权限入口管理。真实订阅、TCP/UDP 美国出口及组间独立性仍需后续接入验收；此次上线只证明管理面及端口边界。
- 同一独立安全复核者检查已加载配置与待审配置摘要一致，核对证书、HTTP 跳转、回环映射、续期 hook 和外部验收，确认公网管理面边界闭合，并撤回本机 TCP 握手误报。

## v0.1.1 登录页文案修正

- 按用户要求移除登录页“只有验证合格的美国出口才会放行。正常保持，失效即停。”段落。
- 源码提交 `6ba86510a99ec271d6028456ae676e13a3422ad0`；原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42222634674 通过，发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42222774769 成功。
- 发布镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.1.1@sha256:7f00b9c7436f95263a83032821e429f76d53d779a4d248504160a4ef2cb0b77e` 已部署。先拉取、确认没有活跃分组、保留旧环境与离线状态备份，再更新重建；健康检查及真实 HTTPS 页面文案验证通过。
- UI DOM 检查通过。本地完整测试首次有一项就绪等待超时，随后该项单独复核通过，完整重跑 37 项通过（2.20 秒）；远程两个原生架构的完整测试和 Compose 启动均通过。

## 订阅编辑与 v0.1.3

- 补齐“订阅来源 → 编辑”：修改名称、HTTPS URL 或导入替换文本，保留来源编号；仅改名称或清空 URL 不触发 provider 更新和探测。完整 URL 仅主动认证读取，普通 state 不包含；关闭、取消、保存、退出时清空弹窗表单。
- 独立审查发现新节点和新 URL 分两次保存的窗口，已修正为同一次 SQLite 提交，并在 provider 更新前写持久中断屏障。失败回退旧 provider/元信息/节点；回退失败时先撤销所有组租约再写状态；进程中断重启保持维护。针对性独立逻辑复核确认原发现闭合，按实际外层 raise、节点配置指纹及同锁调度证据撤回两项误判，无剩余阻塞项。
- 本地测试 55 项通过（2.24 秒），包括提交快照一致、提交失败回退、进程中断屏障、日志失败不虚报回滚、持续磁盘错误撤销所有租约、URL 认证读取及后台锁占用时详情仍可响应。既有应用启动测试改为先确认实际 Mihomo 就绪，避免两个进程启动竞态误报；生产启动逻辑未改变。
- 隔离虚构订阅的 UI DOM 与真实内置浏览器完成编辑加载、改名保存、重新打开、URL 不出现在卡片、取消清除；390px 下弹窗和控件边界正常。
- `v0.1.2` 发布并上线后，实际 61 节点的后台探测暴露详情接口等待控制锁超时；未发生订阅修改。随后发布 `v0.1.3`，只读详情改为无 await 的一致快照读取，回归测试覆盖控制锁被占用时仍能读取。
- 最终原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42229655140 与发布 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42229866913 均通过。源码提交 `65cbcfd708951dc6af1b82765be5c5956b57d3c5`；镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.1.3@sha256:15c27173b2075d83ba2a55502d9b8a9ab3908fc5e07eb810f051d9da927fc2df` 已部署、健康检查通过。
- 升级前确认无活跃分组，分别保留旧环境、旧镜像和卷外一致状态备份，支持失败回退。升级前已有用户订阅 1 个、节点 61 个；未用真实订阅做替换试验。
- 最终线上通过 HTTPS 登录，读取编辑详情（认证/no-store）、提交原名称与原 URL、等待保存任务成功，比较保存前后来源信息及节点定义完全一致：1 个订阅、61 个节点均保留。普通 state 不含 URL、退出后详情返回 401；不会向工具输出订阅地址或名称。

## v0.1.5 异步操作反馈

- 原因：旧页面仅短暂显示 7 秒排队提示，慢任务完成前缺少持续反馈。新增操作进度区域，提交时立即显示，处理期间显示等待时长；查询短暂失败自动重试，成功/失败保留结果，添加订阅期间禁止重复提交。分组操作结果显示实际是否放行与原因，不把任务完成等同于代理放行。
- `node tests/ui_progress.cjs`：隔离 DOM 与受控接口响应，覆盖慢任务、跨标签可见、网络中断恢复、成功、失败、任务记录 400/404 丢失以及退出清理。真实管理服务 `UI_SAMPLE_SOURCE=1 node tests/ui_dom.cjs` 通过。没有把 DOM 模拟称为真实浏览器验收。
- 本地全部后端测试 55 passed，JS 语法及发布边界检查通过；双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42240612521 和最终发布 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42240702274 成功。
- 独立只读复核确认反馈、去重、秘密转义及登出清理无阻塞，指出任务记录丢失实际返回 400 的边界，已补充 400/404 处理及对应回归。复核者未自行重跑测试；该小修由主代理验证。
- 线上部署固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.1.5@sha256:a2155f40c74d584ed5a4c8624d1bf2f23018875cad9c26f199a72f8d5e53dd09`。先确认无活跃分组、预拉镜像和离线一致备份，备份目录 `/root/proxy-egress-manager-deployment-backups/20261106T151426Z-pre-v0.1.5`。真实 HTTPS 登录、状态、页面/脚本资源、健康与退出检查通过。
- 用户已有 2 个订阅、77 个节点；升级前后来源元数据与节点配置（排除探测结果、刷新时间等运行字段）的 SHA256 完全相同，未导入测试订阅、未更改 STUN 或放行策略。Google STUN 经当前候选通道超时的问题仍独立存在，本轮没有以减少验证范围绕过。
- 进度只保存在当前网页内；刷新或关闭会丢失进度记录，后台任务继续。服务端未新增细分阶段，因此界面准确显示“排队或处理中”，不声称知道实际下载/探测百分比。

## v0.1.6 分组说明与初始化健康检查

- 一次性 `init-core` 显式配置 `healthcheck.disable: true`，保留退出码 0 的初始化依赖和管理服务原健康检查。原生双架构 Compose 启动验证增加实际初始化容器退出码及 `Healthcheck.Test[0] == NONE` 断言。成功退出的任务仍显示已停止；未修改 1Panel 的项目汇总规则，不保证消除“部分停止”汇总。
- 分组页面改用“固定当前节点”“允许自动换节点”“启用分组”等名称，区分“可用”“暂不可用”“已手动暂停”。按钮说明包含前提、连接中断影响及全组重验行为，触屏可展开名词与操作指南。原始调度策略和验证条件不变；部分技术原因在展示时转为日常用语。
- 本地后端 55 passed；`tests/ui_group_help.cjs` 覆盖悬停、键盘聚焦、Esc、提示可悬停、轮询期间焦点保持、触屏说明、固定/切换弹窗和无配置写入；异步进度回归与真实管理服务 DOM 流程均通过。内置 Chromium 1280×800 和 390×844 验证提示在视口内、说明可展开、Esc 可关闭、页面无横向溢出；没有把这些 DOM/布局检查称为截图视觉审查。
- 独立只读复核确认分组语义与初始化设置；提出卷外已校验备份未用于回滚，已改为从宿主机备份通过标准输入传入旧镜像的一次性恢复容器，临时文件在成功与失败时清除，并用隔离桩验证实际包装代码。外部健康检查增加有界重试。复核者纠正了先前关于 Compose `up --wait` 不等待应用健康的判断，最终确认两处修正闭合、无剩余阻塞。
- 最终代码 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42246247398 和发布 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42246388621 成功。部署镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.1.6@sha256:6d270529f81953af4fa4714556cf6e2a7546775d060cc5d3fa7500d78be388bc`，同时更新实际 Compose 并重建初始化容器。
- 部署前已告知有活跃分组、重启会断开连接。先预拉镜像和验证待用 Compose，再停机做一致备份；备份目录 `/root/proxy-egress-manager-deployment-backups/20261106T155325Z-pre-v0.1.6`，旧环境与旧 Compose 均纳入回滚。启动后不复活旧授权，由系统重新验证节点。
- 线上初始化容器 `exited / exit=0`、健康检查 `NONE` 且 `State.Health` 不存在；管理服务健康。真实 HTTPS 登录、状态、帮助资源和退出检查通过。升级前后来源、节点（排除探测结果/刷新运行字段）、检测设置与分组手动偏好完全一致：2 个来源、77 个节点。验收时第 1 组已重新验证为可用，第 2 组仍不可用，不将界面改善冒充所有分组业务验收通过。

## v0.2.0 节点范围与质量能力（发布前）

- 实现分组全部/指定订阅/指定节点清单，稳定代际标识防止槽位复用继承权限。保留当前节点保存不撤销会话（包括持久化失败回退），移出需确认并先撤销；保留暂停/固定状态。无候选采用 1/5/15 分钟退避，不重复检测失效当前节点。
- 订阅精确查看节点、搜索、卡片/表格及浏览器本地布局记忆；质量详情区分运行/候选/独立测试与目标、方法、配置世代。成功率不叫丢包率；重启、失败和长缺测断开波动与曲线，恢复生成新统计世代。
- 独立诊断容器手动 HTTP 和 UDP、全局串行/去重/取消；不操作业务核心映射与租约。隔离 Docker fixture 通过真实核心 HTTP 和 20/20 UDP，验证诊断内存限制 128 MiB、CPU 0.25，测试完成后的采样 12.11 MiB / 0.01% CPU；不是生产压测或峰值保证。初次虚拟 SOCKS fixture 返回域名 UDP 中继地址，Mihomo 要求数值地址，修正 fixture 为未指定 IP 后通过。
- 后端覆盖范围/代际、退避、历史截断、跨重启均值与波动、恢复成功/失败、手动取消和清理失败凭据擦除；真实双核心用回环 fixture 验证独立测试不切换、不关闭已建立业务连接。UI DOM 回归涵盖精确来源筛选、布局记忆、范围多选与断开确认、空历史、手动请求及退出清理，不等同于截图视觉验收。
- 迁移和诊断/历史分别经过独立只读审查；修正严格 schema2、不重放遗留迁移库、离线恢复先写屏障、跨重启/恢复分段、完整窗口的 UDP/状态记录、取消文案及清理失败回滚凭据风险。保留节点保存失败误撤销也已修正。
- 使用真实 v0.1.6 发布镜像和新版镜像、临时 Docker 卷及 `--network none`，完成旧备份→v2 限定范围→旧程序拒绝墓碑→原样部署脚本回退代码读取卷外备份→旧版成功恢复→再次 v2 迁移；确认清除 v2 ready/数据库、保留独立质量历史，临时卷已清理。不涉及真实订阅。
- 本地隔离浏览器此前已检查桌面表格、布局记忆、范围弹窗与空历史；本轮没有追加截图/手机视觉验收。已关闭任务页和虚拟演示服务。

## v0.2.0 发布与 VPS 验收

- 源码 `71842cb293dd3d8c7e744f4586a4e79df139d7b8`，本地最终 73 passed（6.66 秒）。原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42332932267 ：amd64 73 passed（8.66 秒），arm64 73 passed（10.56 秒），两架构均完成实际 Compose HTTP + 20/20 UDP fixture 和资源限制验证。
- 发布 https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.2.0 ，流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42332991515 成功，重复原生双架构测试后产出 `ghcr.io/youjiaxing/proxy-egress-manager:v0.2.0@sha256:e2c01c49bdd211d45f8c27347e9391588aaa06b82bf26b8bf80aeeaeaee39953`。Release 附件摘要、架构索引和源码提交一致；空 Docker 配置匿名读取 manifest 成功，VPS 实际拉取部署成功。
- 先预拉、校验待用 Compose，再停止项目并保存旧环境、旧 Compose 和旧镜像的离线一致备份。备份目录 `/root/proxy-egress-manager-deployment-backups/20261107T031649Z-pre-v0.2.0`。升级脚本经独立只读复核与真实镜像隔离回退演练，修正 Bash `if rollback` 抑制 errexit 时两处 cp 失败应显式 return 的路径，无剩余阻塞。
- VPS 迁移前后 2 个订阅、77 个节点、探测设置及分组手动偏好一致；原组迁移范围为 all，来源与分组获得代际标识。初始化退出 0 / Healthcheck NONE；应用健康，独立诊断核心就绪。诊断核心只有一个专用网络、无发布端口，内存 134217728 字节、CPU 250000000 NanoCpus；上线该次快照约 20.29 MiB / 0.07%，不代表峰值保证。
- 真实 HTTPS 认证、Secure/HttpOnly Cookie、state 脱敏、scope、质量接口及新 UI 静态资源通过，退出后 state 返回 401。没有向真实节点额外发起手动诊断或全订阅测速。独立历史已积累正常调度的观测。
- 外部验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42333294788 成功：DNS、可信 TLS、HTTP 跳转、未认证与错误 Origin 拒绝，公网 1081/1082/9090/18080 均不可达。
- **业务出口限制仍存在：** 读取升级前备份与升级后状态确认两组此前都未放行。验收时第 1 组 TCP/UDP 身份各有结果，HTTP/STUN 连通样本成功，但国家归属结果为空，完整美国出口资格不足；第 2 组无合格独立备用并按退避重试。未修改用户探测目标、国家准入或业务服务设置，不将管理面部署成功描述为两组业务出口可用。

## v0.3.0 低开销流量统计（发布前）

- 用户确认按分组/实际节点统计上传、下载、合计，区分业务与探测；北京时间今日/本月/累计；60 秒后台保存；统计失败不额外中断业务并提示缺口。批准完整实施、测试、发布和 SSH 升级。方案评审子代理单次等待超时，按 yjx-grill 第 7 节关闭并由当前会话自审，未宣称取得独立方案评审结论；本轮实现另行进行独立复核。
- 新独立 `traffic.db`，活路径只做内存增量，线程保存幂等批次，不轮询已结束 Mihomo 连接；经回环计量通道统计原有探测 TCP 透传与 UDP 承载字节，不新增外网探测。分组 UID 和 node_ref 在授权/探测开始时冻结，旧账不跟随槽位/名称复用。
- UI 只打开或手动刷新时请求，分组/节点和全历史入口；100 行分页但顶部汇总覆盖全部页。业务、日常、候选、手动分别显示；非计费用量、不保证远端送达与缺口边界随页面说明。统计容量/脏内存有界，配置恢复保留账本并记时间，不提供清零。
- 本机回环 TCP 对照：每轮 64 MiB，首轮预热后旧网关/计量网关交替 7 次取中位数。第一次 1408.8 / 1367.4 MiB/s，CPU 0.0433 / 0.0449 秒；第二次 1312.0 / 1287.7 MiB/s，CPU 0.0464 / 0.0476 秒。两次吞吐差约 2–3%，CPU 约多 1–2 ms/64 MiB。dirty 汇总仅 3 键，分别一次刷盘 1.30 / 1.08 ms，DB 20480 字节。不代表生产容量或零开销保证；没有部署额外周期性测速。
- 本地计量测试覆盖真实回环 TCP/UDP、短连接、探测通道、节点切换旧授权撤销、日/月切换、重启、歧义提交重试与并发查询无重复、容量/内存上限、idle不写盘、配置恢复/API、分页总量及关闭等待保存。UI DOM 覆盖按需读取、UID视角、分页不漏总量、标签转义、缺口提示与退出，不称为真实浏览器视觉验收。

- 实现独立复核续接后完成：后端复核检查冻结身份、载荷口径、授权检查、探测隔离与超时、批次幂等/查询快照、容量失败、关闭保存、恢复标记、SQLite 显式关闭及计量通道初始化失败回退，没有发现实质阻塞；未独立运行测试。UI/API 与升级脚本复核未发现具体阻塞，部分最新变更和回退实证采用主代理证据，不宣称已独立重跑。
- 额外验证计量监听初始化失败会回退原检测路由并标记用量缺口；磁盘保存失败后，同一条真实回环业务连接仍可继续收发。最新本地完整回归 84 passed（6.77 秒）。
- 原样升级回退 Python 包装代码以真实 v0.2.0 和新版容器、隔离临时卷、`--network none` 演练：恢复 format2 限定范围配置、保留新版累计并标记旧版运行缺口；临时卷已清理。回退标记统计失败不会阻止旧版业务启动。

## v0.3.0 发布与 VPS 验收

- 最终源码 `2cfdac27878c5ab975d97a45ed5e77ffa0cb3b11`。原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42342108032 成功：amd64 84 passed（9.58 秒）、arm64 84 passed（11.87 秒），均完成实际 Compose HTTP + 20/20 UDP 计量 fixture。
- Release https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.0 ，发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42342183176 成功。部署固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.0@sha256:c34e0a0acf0a79056c3835d9116c6b5e312dc4880e11982040e75b6c434ff903`；附件源码与摘要一致，VPS 已实际拉取。
- 升级前保存旧 `.env`、Compose、旧镜像的一致 format2 配置备份；已有统计库另行做快照（本次首次启用，没有旧流量库）。目录 `/root/proxy-egress-manager-deployment-backups/20261107T052146Z-pre-v0.3.0`。重建后应用 healthy，init 退出 0 / healthcheck NONE；实际运行镜像与固定摘要一致。
- 另外读取升级前备份与当前 SQLite 状态比较：2 来源、77 节点、探测设置、来源 UID 和分组 UID/范围/暂停/固定偏好完整保留（排除刷新时间与探测快照）。`traffic.db` 权限 0600。
- 真实 HTTPS 登录、按 group UID 读取、100 行分页/完整汇总、新 UI 资源和退出后统计 401 均通过；没有向真实节点额外发起手动诊断。多轮 60 秒后台保存已发生，验收时统计 12 行，最后保存时间正常、gap=false，累计已观测 303395 字节。该数字是当时采样，继续随调度增长。进程资源单次快照约 48.7 MiB / 0.06% CPU，不是峰值或容量保证。
- 外部验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42342803090 成功，包含管理面 DNS/TLS/鉴权和公网端口边界。当前两组仍因国家验证未完成/无合格独立备用而不放行，未修改验证目标或绕过准入。
- 上线时本机到 VPS 的默认路由落在异常虚拟接口，GitHub/TLS 与 SSH 出现间歇故障。仅对任务连接绑定正常物理接口后完成 SSH 核验，并为 GitHub CLI 使用任务独占回环 CONNECT 通道（TLS 端到端），不改系统路由/VPN，不关闭主机密钥校验。临时通道和 SSH 复用连接在验收结束后关闭。

## v0.3.1 国家查询限流修复与质量概览

- 现场重现：TCP/UDP 均取得出口地址，但 `ipwho.is` 返回 HTTP 429 / `Rate limit exceeded`，国家归属为空，出口授权无法续期。临时将现有国家查询配置改为 `https://ipapi.co/{ip}/json/` 后，组 1 完整重验通过。没有删除必需身份目标、放宽美国/双协议/独立出口条件。
- 根因修复：国家元数据按模板与实际 IP 缓存 6 小时，最多 512 项；失败缓存 60 秒，429 按模板退避。TCP/UDP 当前身份和租约有效期仍逐轮验证。国家查询错误、部分身份目标失败及出口冲突保留具体原因。
- 质量页把当前入口状态、延迟、失败比例、波动和带失败红标的趋势放在前面。节点、配置、来源、目标、协议仍分序列，不把检查失败率当作真实丢包；恢复记录不再混成停止转发。说明、历史候选和其他节点默认折叠。
- 固定源码 `7adac5d0a06ab0e8c8a45c1337f90aaf5714a350`，本地固定 Mihomo 测试 86 passed，四组 UI DOM 回归通过（没有宣称浏览器截图视觉验收）。独立只读复核未发现已确认阻塞。原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42347906406 成功；发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42348459432 再次执行双架构测试及真实 Compose，成功。
- 发布镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.1@sha256:ca8af1b1256fd859af16f489d1ac94a17ae6ba996c6c54b532593da6463240ab`，附件源码和摘要匹配；VPS 已实际拉取，仅重建应用，业务核心及诊断核心未重启。升级备份 `/root/proxy-egress-manager-deployment-backups/20261107T065026Z-pre-v0.3.1` 含旧环境、Compose、一致配置及独立 traffic/quality SQLite 快照。
- 线上验证：应用 healthy，HTTPS 认证、质量页新资源、含配置代际的质量 API、退出后 401 均通过。2 订阅、77 节点、来源与分组 UID、范围、暂停/固定设置、配置保留。逐身份核对累计用量没有倒退，统计起点相同，质量记录 518 → 526 条。组 1 租约有效，实际 SOCKS 1081 请求成功且出口匹配当前验证地址。
- 组 2 未放行：既有候选存在必需 UDP 身份目标未响应，更新后重试的候选仍有被独立对照确认的协议故障；未绕过准入放行，页面保留最近候选原因。该状态不代表组 1 仍不可用。
- 外部 DNS、可信 TLS、HTTP 跳转、匿名接口拒绝及公网端口隔离验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42348812888 成功。未单独宣称本版本匿名镜像拉取验收。

## v0.3.2 分组反馈、连续查找与节点池

- 手动自动查找从最多 3 个候选改为连续检查允许范围（单次最多 100 个），每个候选结束释放控制锁。后台故障恢复仍每轮 3 个并退避；不为未使用节点新增周期探测。卡片显示当前进度、逐候选原因及最终结果，区分保存、应用、未应用、取消、中断；重启将未完成操作标为中断。
- 新增自动优选与手动组池。按需评估不更改业务映射或授权，按完整美国出口资格、组间独立性及 HTTP/UDP 耗时推荐最多 3 个，待确认保存。手动多选表显示资格、原因、时间、较慢 HTTP 目标耗时与既有同配置单目标历史；打开列表只读历史，不发探测。历史检查通过比例不作为真实丢包率，一次评估不代表长期稳定。
- 独立复核发现并修正：候选业务入口复验失败或取消后立即重新观测并恢复原可用节点，不重用旧授权；成功授权和操作结果在同一控制锁中提交，后续暂停不篡改已完成操作结果。针对修正的复核闭环，最终节点池/UI/API 复核无已确认实质阻塞；复核者未独立重跑测试。
- 最终源码 `92bbb9727478f66de30361f512ba528968efd987`，本地 98 passed（6.98 秒），六组 UI DOM 回归通过；未另作实际浏览器视觉验收。原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42352504883 和发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42352813151 成功，包含真实 Compose 回环验证。
- Release https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.2 ，部署固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.2@sha256:57bbf04feeb4e36dae2f079c068faca8e7bd5ddd0f6c90c612c77c0f50420db4`，附件源码与摘要匹配。仅重建应用、保持两个核心运行；一致备份目录 `/root/proxy-egress-manager-deployment-backups/20261107T075218Z-pre-v0.3.2`，包括旧环境、Compose、配置及独立统计库快照。
- 组 2 现场阻塞来自必需 Google STUN 19302 超时。先通过现有候选通道核对 Cloudflare 与 Twilio 返回一致美国出口，再将配置调整为 `stun.cloudflare.com:3478` 和 `global.stun.twilio.com:3478`。两路必需 UDP 目标、TCP/UDP 美国身份和组间独立要求保留；两组完整复验后恢复有效授权。
- 升级后应用 healthy；真实 HTTPS 登录、退出后 401、新资源、质量 API、流量 API 通过。2 订阅、77 节点、来源/分组 UID、范围、暂停/固定偏好和升级前配置一致。累计用量按身份不倒退、统计起点相同、gap=false（记录 135 → 174）；质量历史 675 → 723。外部验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42353204961 成功，包含 DNS/TLS、认证与公网端口边界。
- 线上对组 2 实际执行一次自动优选：77/77 个候选全部完成，19 个通过完整出口检查，推荐 3 个（本次较慢 HTTP 目标耗时 49.84 / 57.22 / 59.58 ms）。评估期间逐轮检查组 1 授权保持有效；完成后两组节点及范围均与升级备份一致，未自动应用推荐。认证池历史返回 77 个摘要项，匿名访问 401。两组实际 SOCKS 请求均成功、地址匹配当前验证结果且两组出口无交集。推荐仅表示本次结果，不等同长期稳定性。

## v0.3.4 统一分组配置流程

- 根因是节点池功能以两个并列按钮追加，原“更换节点”仍打开单选；主入口与用户配置多节点池的预期不一致。卡片现以“配置分组”为主入口，先选择自动优选或手动多选，已有推荐可直接查看、无需重新探测。单节点切换明确改名“立即切换当前节点”并收进“更多操作”；轮询保留已展开的更多操作。
- 配置页明确当前范围、自动优选的流量和100个上限、保存前确认、暂停/固定状态不自动解除，以及可用分组当前节点仍在新池内时继续使用。手动表不再一概称“保存并验证”，避免把保留当前连接的保存操作说成强制重验；说明同步更新。
- 本地 98 passed（6.99秒）；六组 UI DOM 回归通过。新增真实 DOM 点击覆盖统一入口、自动评估、推荐确认、手动多选、已保存推荐无额外请求、暂停禁用自动评估但可编辑、折叠单节点入口和轮询保留展开。旧质量测试改为从统一入口进入，未通过保留旧直达按钮绕过新流程；未宣称实际浏览器截图视觉验收。
- 独立只读复核确认自动/手动/旧推荐/单节点切换调用关系，指出暂不可用分组保存变更范围时仍会重新查找，不能一概承诺不换节点。已按后端 active 分支修正页面、帮助和文档，并增加 DOM 断言通过。没有改调度策略或扩大探测范围。v0.3.3 未部署线上，本次使用修正后的 v0.3.4。
- 修正源码 `6531ad7f2b02cb593abf8109b43791e4648cdd22`，发布 https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.4 ，原生双架构及真实 Compose 发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42355276413 成功。固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.4@sha256:50b912a22cf453c5aa33032a10db4b342ec816917dcafe81ea84c20b99ad2314`，附件源码与摘要匹配。仅重建应用，两个核心保持运行；备份 `/root/proxy-egress-manager-deployment-backups/20261107T082730Z-pre-v0.3.4`。
- 首次线上业务验收如实失败：当前国家服务 ipapi.co 已限流，必需 Twilio STUN 超时，两组出口身份到期。只读目标对照发现 ipwho.is 同样429、ipwhois.app正常返回两组US；Google19302在组2超时，但Google3478和Cloudflare3478在两组均响应且与TCP身份一致。先用复制设置完整 Prober 观测两组均 complete/us，再通过认证设置操作切换至 `https://ipwhois.app/json/{ip}` 与 Cloudflare3478/Google3478。仍要求两路UDP、TCP/UDP美国身份及独立出口，不将服务可达性等同准入，也不声称免费公共查询服务长期可靠。
- 更新检测目标后，HTTPS登录/退出、新界面资源、质量/流量API、两组有效授权和两组实际SOCKS请求均通过；两个出口独立且与当前验证地址一致。来源/分组UID、77节点、原节点及范围保持，既有77候选/19通过/3推荐记录保留，未重新全量探测或自动应用推荐；旧评估仍需当前配置复验。累计用量按身份不倒退、统计起点相同、gap=false，质量历史882→898。外部验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42355572072 成功（TLS/认证/公网端口隔离）。

## v0.3.5 连续配置旅程

- 将配置分组串为选择方式→挑选节点→确认配置→查看结果。确认页在写配置前列出拟保存清单、当前节点和连接影响；保留断开影响确认，返回修改保留选择。评估与保存留在当前对话框显示进度；关闭明确提示后台继续，用户关闭后不会在评估结束时擅自弹回。
- 保存后直接展示实际节点池、当前节点、授权状态和原因；暂停、固定、暂不可用分别提供启用、解除固定、重试路径。错误保留草稿；请求是否执行不确定时不自动重提，先只读核对实际状态，再由用户选择是否修改。一次结果状态刷新失败不宣称当前业务已生效。
- 异步历史回填绑定对话框世代，转入确认页后旧回填不再写入；请求保留捕获的分组UID，不将旧流程绑定到复用槽位。保存失败的卡片标题不再沿用上次成功结果。
- 本地固定 Mihomo 回归 98 passed（7.04秒），七组 UI DOM 回归通过。新增 journey 覆盖编辑既有节点池、确认前无写入、移出确认、返回/失败保留选择、实际结果、暂停启用、固定解除、模糊提交不重试及先读状态、评估关闭不重开和迟到历史不覆盖确认页；未宣称实际浏览器截图视觉验收。
- 独立实现复核未发现已确认阻塞，确认挑选不写配置、显式确认才保存、失败保留选择、结果及历史回填的主要路径。复核未独立重跑测试，最后的显式UID优先、关闭评估不重开、unknown核对后回草稿没有重读；主代理逐项核对固定提交源码并重跑针对性journey通过，不将这些结论冒充独立执行。
- 固定源码 `67d5e3a163646a1dff0de437bc72602b1246f901`；原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42357120455 与发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42357451998 均成功，包含真实 Compose 验证。Release https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.5 ，固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.5@sha256:fa2d155ea977f9d4c548d17934afb9c1355b6a58a0706c57a3ebf1de5520653d`，附件源码/摘要已核对。
- 线上备份 `/root/proxy-egress-manager-deployment-backups/20261107T085809Z-pre-v0.3.5`，仅重建应用，两个核心保持运行。HTTPS登录/退出、四步流程资源、质量/流量API、原订阅与节点及来源/分组UID、原节点/范围/暂停/固定设置和检测配置保留通过。两组租约有效、实际SOCKS请求出口匹配且相互独立；既有77候选/19通过/3推荐历史保留，没有额外全量评估或应用推荐。流量逐身份不倒退、起点一致、gap=false，质量历史1004→1006。
- 外部 DNS、可信TLS、认证和公网代理/核心/管理端口隔离验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42357700423 成功。线上没有为验收修改用户节点池；旅程写入、失败与恢复路径在隔离DOM fixture/CI中验证，线上只核对资源、只读配置与实际出口。

## v0.3.7 简单节点面板

- 根据用户参考图，将节点卡片简化为名称、醒目彩色 HTTP 延迟/失败状态、订阅和时间；详情/优先级/管理折叠。数字可单击发起独立轻量测试，无需先打开连接质量弹窗。勾选卡片高亮，原批量测试保留；测试中明确显示，批量轮询只读取已完成记录，不增加外网探测。
- 最新有效手动失败不再回退显示较早成功，超时独立标记，来源身份变化的结果不采用；HTTP请求耗时不称 ICMP ping。未改分组映射、租约、节点池或自动调度频率。
- 本地98 passed（7.07秒）与八组UI DOM通过。新增node_latency覆盖超时覆盖旧成功、颜色、折叠、选中高亮、单击测试/进度、来源失效结果排除及只读操作不发探测。参考图经独立视觉观察确认三列卡片、数字与Timeout和选中高亮；未将截图推断为具体点击行为，未声称浏览器截图视觉验收。
- 独立复核发现重叠单测/批量提交会清除先前测试的running标记，v0.3.6未部署；修正版在任何写入running标记或API请求之前拒绝集合交叉，回归覆盖单测A后批测A+B不提交且A仍测试中。同步将完成判定改为服务器记录变化，避免浏览器/服务器时钟偏差；保留详情展开、保护卡片优先级编辑，前置拒绝禁用节点。针对修正由主代理核对与回归，不宣称独立重跑。
- 修正后本地98 passed（6.95秒），固定源码 `677f54bff74066795dbc8091b0e44deb584b97d7`，发布 https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.7 ，双架构/真实Compose流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42363484916 成功。固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.7@sha256:dda0415c01baa8b71e3558315bb8a077a2b8a95e49f8063d9a4730be7ac8fe3a`，附件源码/摘要核对一致；仅重建应用、核心保持运行。备份 `/root/proxy-egress-manager-deployment-backups/20261107T102418Z-pre-v0.3.7`。
- 线上HTTPS认证、新节点面板资源、质量/流量API、2订阅77节点与分组UID/范围/原节点/设置完整保留；两组授权有效且真实SOCKS出口匹配、独立。仅对组1当前节点做一次独立HTTP测试，返回83.3ms、current=true，结果已写入节点状态；前后分组节点/范围不变。未进行全订阅测速或重新优选。流量累计逐身份不倒退、起点一致、gap=false；质量历史1357→1360。完整按钮行为在隔离DOM/CI验证，未将线上API验收称为浏览器点击验收。
- 外部验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42363811245 首次恰逢应用重建，首页200断言失败；服务恢复后原样重跑attempt2成功，DNS/TLS/认证及公网端口隔离通过，未修改验收标准。

## v0.3.8 TCP 准入、UDP 参考与 Google 访问恢复

- 用户确认 TCP 为硬性条件、UDP 仅观测，并明确授权实施和上线。保留全部 TCP 身份目标成功、TCP 美国归属与组间 TCP 出口独立；UDP 未响应、国家或地址变化不再参与暂停、换节点或冲突判断。UDP 转发仍保留。可选测量使用健康预算与身份有效期的余量，不能丢弃已完成 TCP 结果；质量记录增加策略分段，帮助和拒绝原因同步更新。
- 回归先在旧实现上得到 6 failed / 2 passed，复现 UDP 导致换节点/拒绝候选等问题；随后本地固定核心完整 105 passed、8 组 DOM 检查通过。另以真实 SOCKS 增加身份有效期为 1 秒且 UDP 卡住的失败回归，再收紧可选测量预算并通过。独立审查未发现已确认发布阻塞；最终提交与 CI 状态由主代理核对，不宣称审查者独立重跑 CI 或 VPS。
- 最终源码 `ea80d5bd4b64bf9218bcb98c02ac206187d06037`。原生双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42434514268 成功；Release https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.8 ，发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42434876432 再次完成双架构测试、构建与真实 Compose，成功。固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.8@sha256:a80e48ce2b8cd5cf10a01c7bdfccba66fadcd2a9872f7a562f6962d1afd62096`，发布附件源码/摘要一致，VPS 匿名拉取成功，镜像 revision 匹配。
- 应用更新前，一致配置备份及独立 traffic/quality SQLite 快照保存在 `/root/proxy-egress-manager-deployment-backups/20261108T013918Z-pre-v0.3.8`。无格式迁移，仅停止并重建应用，两个既有 Mihomo 核心容器身份和启动时间不变。新应用 healthy；HTTPS 认证、新 TCP/UDP 文案、质量和流量 API、退出后 401 通过。更新时 2 来源、77 节点、来源/分组 UID、设置、范围与暂停/固定偏好保留。流量 208 行逐身份不倒退、统计起点相同、gap=false；质量历史 5376→5378。
- 现场基线：组 1 的三条 Google Fonts 完整 GET 均超时，组 2 均 200。组 1 原清单 3 个引用仅 1 个仍有效。临时独立 Mihomo（无宿主端口、128 MiB / 0.25 CPU）先测旧池，再扩大至已启用的美国标注候选；候选总数 32，但实际只测 4 个就找到 3 个通过三条完整字体 GET、TCP 美国身份且不与组 2 冲突的节点，然后停止。临时静态配置最初因重复代理名启动失败，按配置名去重后才开始候选探测；不修改业务核心来做这些测试。临时容器、配置和脚本已清理，脱敏候选结果留在本次备份目录。
- 通过现有认证管理 API 将组 1 清单替换为上述 3 个引用，并明确确认断开旧连接；正式检查遇到“圣何塞04”的 TCP 身份目标暂未返回时跳过，随后“圣何塞03”候选与业务入口复验通过，操作持久化结果 `saved_available`。组 2 当前“圣何塞01”节点及范围未改。最终 TCP 地址分别为 `104.251.122.87`、`207.32.216.109`，两组授权有效；地址只是当时观测，不保证永久静态。
- 实际网关 SOCKS5 1081/1082 各对用户报错的 Google Sans Flex、Inter/Inter Tight、Google Sans Text 三条 Fonts URL 连续复测两轮：12 次完整响应均 HTTP 200，CSS 内容包含 font-face，耗时 55.1–164.8 ms；两组实际 TCP 身份请求也匹配管理器记录。未修改现有业务容器，不将这次代理入口验收称为调用方服务自身的完整端到端验收。Fonts 检查只用于本次恢复，没有加入周期任务，也未给空闲节点新增周期探测。
- 外部 DNS、可信 TLS、HTTP 跳转、匿名接口拒绝与公网端口隔离验收 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42435226474 成功。

## v0.3.9 省流检测档上线

- 用户授权采用推荐档：TCP 每 300 秒、UDP 每 1800 秒，匹配调度健康宽限 420 秒和 TCP 身份有效期 900 秒；持续 TCP 故障阈值仍 180 秒，请求超时仍 8 秒。TCP 美国身份及组间 TCP 出口独立规则不变。新字段只补 UDP 缺省值，不自动覆盖旧部署的 TCP 设置；现场通过认证设置 API 明确应用四项参数。
- 独立 UDP 调度按分组 UID、节点 `node_ref`、STUN 目标、国家 URL 和间隔计时；候选仅核验 TCP，新业务节点复验时尝试 UDP。跳过的轮次不发 STUN、不生成假样本，保留绑定实际来源/节点身份的 UDP 上次结果和时间。删除/恢复清理运行计时，旧备份缺字段可恢复为 1800 秒。设置页新增独立频率及省流建议，卡片/表格展示 UDP 上次观测时间。
- 本地完整固定 Mihomo 回归 107 passed（8.06 秒），八组 UI DOM 和发布边界检查通过；覆盖跨 1800 秒的独立频率、TCP 等待期间授权有效、节点/来源 UID/间隔改变、真实 SOCKS 下 TCP-only 不调用 STUN，以及旧格式备份的实际离线恢复。独立审查最终源码 `15f48837677e210ce6b52eeefaeee8bb9d88efab` 未发现阻塞；线上与 CI 由主代理验证，不宣称审查者独立执行。
- 双架构 CI https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42437938606 成功；Release https://github.com/youjiaxing/proxy-egress-manager/releases/tag/v0.3.9 ，发布流程 https://github.com/youjiaxing/proxy-egress-manager/actions/runs/42438204963 再次完成双架构测试、构建及真实 Compose，成功。固定镜像 `ghcr.io/youjiaxing/proxy-egress-manager:v0.3.9@sha256:b6f862b9525458f596bde54fb200eddf47eaafd1a15755f9e6c0e3e6cd3a5ab6`，附件源码/摘要和 VPS 镜像 revision 一致，匿名拉取成功。
- 升级备份 `/root/proxy-egress-manager-deployment-backups/20261108T022543Z-pre-v0.3.9` 含旧环境、Compose、一致配置及独立流量/质量 SQLite 快照。只重建应用，两个 Mihomo 核心容器身份与启动时间不变，应用 healthy。HTTPS 认证、新频率/时间文案与质量/流量 API、退出后拒绝访问通过；2 来源、77 节点、分组 UID/节点池/固定暂停偏好保留，流量 220 行逐身份不倒退、起点相同、gap=false；质量记录 5592→5594。
- 通过认证设置 API 应用 300/1800/420/900，比较确认其他设置、两个业务节点、节点池和分组属性均不变。真实等待约 361 秒，仅每 60 秒只读状态；过渡期旧等待结束后，观测到组 1/2 的两次正式 TCP 验证间隔分别 300.42/300.41 秒。整个观察期租约均有效、UDP 观测时间不变、最新记录 `udp_checked=false`。30 分钟 UDP 到期的完整跨度由本地可控时钟测试覆盖，未宣称线上等待了 30 分钟。
- 最后各从实际 SOCKS 1081/1082 发一次完整 Google Fonts GET，均 HTTP 200、响应包含 font-face，耗时 158.1/156.2 ms。仅读取全分页账本，在约 301 秒稳定观察窗口，日常检测合计增加 27368 字节；按这一短样本外推约 7.9 MB/天、236 MB/30 天，不含业务/候选/手动/直连请求及网络协议封装，不等同订阅计费或长期保证。没有全节点扫描，也没有新增周期目标；验收脚本已清理。
