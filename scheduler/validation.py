import math
import re
from urllib.parse import parse_qs, urlparse

TIME_KEYS = ("check_interval", "udp_check_interval", "failure_seconds", "health_grace", "identity_ttl", "probe_timeout")


def validate_settings(settings):
    for key in TIME_KEYS:
        value = settings.get(key, 1800) if key == 'udp_check_interval' else settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 86400:
            raise ValueError("时间参数范围为 1–86400 秒")
    for key in ("tcp_identity_urls", "tcp_check_urls", "stun_servers"):
        if not isinstance(settings[key], list) or not 1 <= len(settings[key]) <= 4:
            raise ValueError("每类探测目标须为 1–4 个")
    for url in settings["tcp_identity_urls"] + settings["tcp_check_urls"] + [settings["country_url"]]:
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("探测地址无效")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("探测端点须为无凭据的公共 HTTPS 地址")
        if any(re.search(r"key|token|secret|password|auth", name, re.I) for name in parse_qs(parsed.query)):
            raise ValueError("探测 URL 不支持内嵌密钥，请使用公共或自有无认证端点")
    if "{ip}" not in settings["country_url"]:
        raise ValueError("国家查询 URL 必须含 {ip}")
    for server in settings["stun_servers"]:
        if not isinstance(server, str) or len(server) > 255:
            raise ValueError("STUN 地址无效")
        try:
            host, port = server.rsplit(":", 1)
            if not host or not 1 <= int(port) <= 65535 or any(c in host for c in "/@?#"):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("STUN 地址须为主机:端口") from None
    alert = settings.get("alert_url", "")
    if alert and (not isinstance(alert, str) or len(alert) > 4096 or urlparse(alert).scheme != "https" or not urlparse(alert).hostname):
        raise ValueError("告警地址须为 HTTPS URL")
    if not isinstance(settings.get("alert_enabled"), bool):
        raise ValueError("告警启用状态无效")
