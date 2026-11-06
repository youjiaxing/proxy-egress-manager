# 升级、备份和恢复

## 日常操作

- 正常不按测速换节点，切换后不自动回切。手动切换也必须校验双协议、国家与独立性。
- 钉住仅禁止换节点，不免除合格检查。定时解除使用墙钟；授权期限使用单调时钟。
- 订阅刷新默认每 6 小时，失败 5 分钟后重试，保留旧配置。文本导入无 URL 时只有手动刷新。
- 候选每组每轮最多检查 3 个，按轮转推进；美国名称标注仅影响候选优先级，不是准入证据。超大订阅或服务不可达会延长首次分配时间。
- 告警为用户配置的 HTTPS JSON webhook，启用后投递暂停/切换/错误事件。每条只作一次有界尝试，不保证送达；URL 在普通状态、事件和诊断中隐藏。未配置不发消息。

## 分组操作与帮助

一个分组对应一个固定代理入口，背后节点变化时入口地址不变。页面分别显示“可用”“暂不可用”和“已手动暂停”，验证失败不会冒充手动暂停。

“钉住”在网页中改称“固定当前节点”：节点故障时不自动更换，验证失效后该组停止转发；固定节点不保证固定公网 IP。“允许自动换节点”解除固定，正常时仍保持当前节点。“启用分组”取消手动暂停，需要重新验证才能使用。

鼠标移到分组操作按钮或用 Tab 聚焦时可读到作用、前提和连接中断影响；Esc 关闭提示。页面还提供可点击展开的名词与操作说明，供触屏和首次使用时查阅。检查连接会复查全部分组，分组管理操作也可能触发其他组重验；不只是当前节点测速。自动选择每轮最多尝试 3 个节点。

## 操作进度

提交添加订阅、编辑、切换等操作后，页面显示持续的“操作进度”，包含提交、排队或处理、等待时间以及完成或失败结果；暂时无法查询任务时会重试，不代表后台任务失败。分组操作完成不等于放行，结果同时显示实际放行状态和原因。处理中添加订阅按钮不可重复提交。

进度可跨管理标签查看，结束记录需手动清除；刷新或关闭网页会丢失进度记录，但不会取消已提交的后台任务。此时先核对订阅或分组当前状态，避免重复提交。

## 编辑订阅

在“订阅来源”卡片点击“编辑”，可修改名称、HTTPS URL，或粘贴新的节点文本。名称修改直接保存，不刷新 provider 或触发探测；保留原来源编号和节点身份。清空 URL 会停止自动下载，保留现有节点用于手动导入。

更换 URL 时下载并校验新内容；同时填写节点文本时优先导入文本。导入/刷新成功后才保存新的名称和 URL。下载、解析或核心更新失败时保留原来源信息；如原节点将被替换，会先安全暂停受影响分组，重新验证后才允许放行。

编辑过程中的新来源信息和节点在同一 SQLite 提交中保存，provider 更新前写入中断屏障。若提交失败会尝试恢复旧 provider 与状态；回退失败或进程意外中断时保持维护暂停，应恢复一致备份后重验，不会自动运行混合的新旧数据。

完整 URL 只在已登录管理员主动编辑时读取，普通状态和卡片不显示。关闭编辑框或退出登录会清空表单中的秘密。

## 网页备份恢复

“下载完整备份”取得同一控制锁下的 SQLite 快照与 provider 内容，包含订阅/节点秘密；存放到受限目录或加密保险库。管理员密码和部署 `.env` 不包含在网页备份中，须另行安全保存。

网页“覆盖恢复”先进入全局维护态，撤销所有授权，等旧连接和 UDP 关联清理后再更新 provider 与状态。在修改 provider 之前将维护标记持久化并同步到磁盘；失败或中断时标记保留，重启仍保持维护暂停，不自动运行半套恢复数据。状态与历史提交完成后才清除标记；可重试有效备份，或停服使用离线恢复。成功后重新验证才放行；不恢复旧授权。不保证备份之后的配置/历史继续存在。

网页备份用于日常恢复，不替代下述升级前的停写一致备份。历史事件最多保存 5000 条，诊断不包含完整备份，也不默认导出历史。

## 手动升级（有中断）

发布新镜像不会更新服务器。选择维护窗口，并记录原版本对应的 Compose、`.env`、项目/Mihomo 镜像摘要与数据 schema。先预拉新镜像并检查备份空间，不要停止后才发现镜像无法获取。

1. 保留旧镜像/版本文件，不运行自动清理旧镜像或 `docker compose down -v`。
2. 在 1Panel 停止整个项目，或使用 `docker compose stop`。应用优雅退出会撤销授权并清理会话；核心也必须停止。核验所有旧容器确实停止，不得同时运行新旧控制者。
3. 固定**实际原项目名、旧版本 Compose 与部署环境文件、旧项目镜像摘要**。以下模板中的路径及镜像必须替换成实际值；在 Bash 中一次执行，任一步失败即停止。不要使用 `compose.build.yaml`，不要传 `--build`。

```bash
set -euo pipefail
PROJECT='原1Panel项目名'
OLD_COMPOSE='/absolute/path/old/compose.yaml'
DEPLOY_ENV='/absolute/path/private/.env'
OLD_IMAGE='ghcr.io/你的账号/proxy-egress-manager:v0.1.0@sha256:实际旧摘要'
# 先核对容器标签中的项目名与卷，不能凭目录名称猜测。
docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}'
docker image inspect "$OLD_IMAGE" >/dev/null
export PEM_IMAGE="$OLD_IMAGE"
dc_old() { docker compose --env-file "$DEPLOY_ENV" -p "$PROJECT" -f "$OLD_COMPOSE" "$@"; }
dc_old stop
# 必须无运行中容器；发现有输出就中止，而不是继续备份。
test -z "$(dc_old ps --status running -q)"
dc_old run --rm --no-deps --entrypoint python proxy-egress-manager \
  scripts/maintenance.py backup /data/backups/pre-upgrade.json
# 备份必须复制到数据卷之外。标准输出仅包含备份，禁止显示到终端/日志。
BACKUP_DIR='/absolute/path/private/upgrade-backup'
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
umask 077
dc_old run --rm -T --no-deps --entrypoint cat proxy-egress-manager \
  /data/backups/pre-upgrade.json > "$BACKUP_DIR/pre-upgrade.json"
test -s "$BACKUP_DIR/pre-upgrade.json"
python3 -m json.tool "$BACKUP_DIR/pre-upgrade.json" >/dev/null
```

脚本取得同一数据目录独占锁，应用仍在运行时拒绝操作；`--no-deps` 防止启动核心。备份文件权限为 0600。将卷外备份复制至另一存储位置，并另行安全保存原 Compose、部署秘密、项目名、镜像摘要及校验和；同宿主备份不能抵御磁盘损坏。

4. 确认备份有效后，使用新版本 Compose、相同项目名与部署环境文件，显式把 `PEM_IMAGE` 改成已预拉并核验的新镜像摘要，再执行 `up -d --no-build --force-recreate --wait --wait-timeout 90`。初始化任务先写配置；不要单独热重载整个 Mihomo 配置升级。
5. 所有组启动默认暂停，重验后逐组放行。检查网页与调度、TCP/UDP、独立占用以及业务连通。任何失败保持停止或暂停，不带病放行。

## 回退

不以“数据格式看起来兼容”为依据只换回旧镜像。停止全部新组件并确认旧会话清理；恢复旧 Compose、部署秘密、项目名和旧镜像摘要；使用旧项目镜像恢复卷外备份。以下命令沿用上面的 `dc_old`、`BACKUP_DIR` 定义，确保已重新导出 `PEM_IMAGE="$OLD_IMAGE"`，且新旧所有组件都已停止。

```bash
set -euo pipefail
export PEM_IMAGE="$OLD_IMAGE"
docker image inspect "$OLD_IMAGE" >/dev/null
test -z "$(dc_old ps --status running -q)"
# 只读挂载卷外备份；恢复成功后才允许启动。
dc_old run --rm --no-deps -v "$BACKUP_DIR:/backup-input:ro" --entrypoint python proxy-egress-manager \
  scripts/maintenance.py restore /backup-input/pre-upgrade.json &&
  dc_old up -d --no-build --force-recreate --wait --wait-timeout 90
```

卷外备份目录和文件须让容器 UID 10001 可读。可将备份的专用恢复副本设为宿主 UID 10001 所有、目录 0700 / 文件 0600；不要将秘密文件改为全员可读。恢复前核对同一原项目的数据卷，禁止在不同项目名下创建新卷冒充恢复。

**回退会丢失升级前备份点之后的配置和历史。** 备份属于其他 schema 时拒绝恢复，不提供未验证的自动降级迁移。中途恢复失败保持所有服务停止，修复或重试，不启动部分恢复的系统。

## 运行中问题

- `unhealthy` 不会自动断电或触发 Docker restart。数据路径授权负责阻断；restart 只响应退出。
- 身份查询失败不刷新有效期；到期暂停相应组，不据此盲切。独立连通故障计时继续，满足条件后可找备用；探测目标对照也不可用时不认定节点故障。
- 维护态恢复失败：检查备份和核心 API，重新提交有效恢复；不要手工改数据库尝试放行。
- 宿主磁盘满/持久化失败：授权不能先于持久化提交；保持暂停并释放空间后重验。先保全数据库与秘密，勿删除数据卷。
