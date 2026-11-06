# 第三方软件与发布核查

本项目原创部分采用根目录 MIT 许可证。该声明不重新许可第三方内容。

## 独立上游

- Mihomo v1.19.33：MetaCubeX 上游独立镜像 / 二进制，GPL-3.0，源码及许可：https://github.com/MetaCubeX/mihomo/tree/v1.19.33 。Compose 引用上游镜像，不将内核代码复制进项目原创代码。独立容器不自动决定所有许可义务，分发方式改变时重新核查。
- Python 基础镜像：Docker Official Image `python:3.12.16-slim-bookworm`，包含 Python/系统软件各自许可，基础镜像与系统组件不采用本项目 MIT。

## Python 依赖

`requirements.txt`：aiohttp (Apache-2.0 AND MIT)、aiohttp-socks (Apache-2.0)、PyYAML (MIT)。间接依赖在安装后核查包元数据和分发许可；包括 python-socks (Apache-2.0)、attrs (MIT)、multidict (Apache-2.0)、yarl (Apache-2.0)、frozenlist (Apache-2.0)、aiosignal (Apache-2.0)、aiohappyeyeballs (PSF-2.0)、propcache (Apache-2.0)、typing-extensions (PSF-2.0)、idna (BSD-3-Clause)、async-timeout (Apache-2.0；受 Python 版本条件约束)。测试依赖 pytest、pytest-asyncio 及其间接依赖不进入生产镜像。

生产安装保留依赖 `.dist-info` 许可文件，不使用剥离许可的打包方式。发布产生 SBOM 与构建来源信息，需审查最终镜像而不仅是源码依赖清单。发现不兼容或不明确的依赖分发条件时暂停公开发布，不以 MIT 标签覆盖它。

项目镜像内 `/app/LICENSE` 仅适用于原创代码，`/app/THIRD-PARTY.md` 说明组合软件的独立许可；镜像不以整体 MIT 的 OCI 标签覆盖基础系统和依赖。CI 从 Compose 固定摘要的当前原生架构镜像提取 `/mihomo` 运行测试，与部署使用同一镜像对象，不再另行下载未校验的发布压缩包。

## 固定摘要

2026-11-06 通过 Docker Registry manifest 核对：

- `metacubex/mihomo:v1.19.33`：`sha256:9211361a3ed85305383288d38c05e85862d6d5a590c10007c37fc059cca82e9c`
- `python:3.12.16-slim-bookworm`：`sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df`

两个 manifest 均列出 Linux amd64 / arm64。这证明仓库提供对应架构条目，不等于本项目在两架构的运行测试已经通过。测试记录单独维护。

本地最终运行依赖版本：aiohttp 3.14.5、aiohttp-socks 0.12.0、multidict 6.9.1、yarl 1.25.1；其余精确固定版本见 requirements.txt。已按该清单执行已知漏洞审计，结果见 verification.md。
