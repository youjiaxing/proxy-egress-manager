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
