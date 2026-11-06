# 配置说明

部署参数在根目录 `.env.example`，运行设置在网页中管理，核心配置由 `init-core` 生成，不手工维护另一套权威配置。

`probes.example.json` 是网页“设置与恢复”字段对应的公开示例，不会自动导入。默认公共探测目标须在真实服务器和机场上验证，不能仅凭示例断言可靠。探测 URL 不支持内嵌认证密钥；可使用公共或自有无认证服务。`subscription.example.yaml` 只展示输入结构，域名虚构，不能放行。

Mihomo YAML 推荐用于机场的完整协议选项；URI 导入支持 socks5、SS（无 plugin）、VMess、Trojan、VLESS、Hysteria2、TUIC 的常用字段。复杂协议/插件参数用明确的 Mihomo YAML，不能把 URI 转换失败误当节点可用。YAML alias/anchor 引用不接受，请展开普通定义。
