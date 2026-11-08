import base64
import ipaddress
import struct

import pytest
import yaml
from scheduler.core import parse_subscription, Core
from scheduler.probe import COOKIE, stun_response


def test_source_names_are_isolated_and_config_change_changes_identity():
    text = 'proxies:\n - {name: 美国 1, type: socks5, server: example.com, port: 1080, udp: true}'
    a, _ = parse_subscription(text, 1)
    b, _ = parse_subscription(text, 2)
    assert not set(a) & set(b)
    changed, _ = parse_subscription(text.replace('1080','1081'), 1)
    assert not set(a) & set(changed)
    assert next(iter(a.values()))["hint_us"]


def test_raw_base64_uri_support_and_unsafe_config_rejection():
    text = base64.b64encode(b'socks5://user:password@example.com:1080#US').decode()
    nodes, payload = parse_subscription(text, 1)
    assert len(nodes) == 1
    assert yaml.safe_load(payload)["proxies"][0]["password"] == "password"
    with pytest.raises(ValueError):
        parse_subscription('proxies: [{name: x, type: direct, server: example.com, port: 1}]', 1)
    with pytest.raises(ValueError):
        parse_subscription('proxies: []', 1)


def test_core_has_reject_first_and_separate_candidate_listener(tmp_path):
    core = Core(tmp_path, secret="x"*32, capacity=3, source_capacity=3)
    core.initialize()
    config = yaml.safe_load((tmp_path/'config.yaml').read_text())
    assert len(config['listeners']) == 4
    assert all(g['proxies'][0] == 'REJECT' for g in config['proxy-groups'])
    assert all(l['udp'] for l in config['listeners'])
    assert config['profile']['store-selected'] is False
    assert len(config['proxy-providers']) == 3


@pytest.mark.parametrize('ip', ['8.8.8.8','2606:4700:4700::1111'])
def test_stun_transaction_and_xor_ipv4_ipv6(ip):
    transaction = b'x'*12
    address = ipaddress.ip_address(ip)
    key = struct.pack('!I',COOKIE)+transaction
    encoded = bytes(a^b for a,b in zip(address.packed,key))
    attr = b'\x00'+bytes([1 if address.version==4 else 2])+struct.pack('!H',12345^(COOKIE>>16))+encoded
    body = struct.pack('!HH',0x20,len(attr))+attr
    packet = struct.pack('!HHI',0x101,len(body),COOKIE)+transaction+body
    assert stun_response(packet,transaction)==ip
    with pytest.raises(ValueError):
        stun_response(packet,b'y'*12)


def test_identity_requires_every_configured_target():
    from scheduler.probe import Observation
    result=Observation(True,True,{'8.8.8.8'},{'8.8.4.4'},{'8.8.8.8':'US','8.8.4.4':'US'},identity_complete=False)
    assert not result.complete and not result.us


def test_admission_requires_tcp_only_and_retains_udp_metadata():
    from scheduler.probe import Observation
    result = Observation(True, True, {'8.8.8.8'}, {'8.8.4.4'}, {'8.8.8.8': 'US', '8.8.4.4': 'DE'})
    assert result.complete and result.us
    assert result.public()['countries']['8.8.4.4'] == 'DE'
    result.tcp_ok = False
    assert not result.us


def test_subscription_aliases_rejected():
    with pytest.raises(ValueError):
        parse_subscription('shared: &x {name: US, type: socks5, server: example.com, port: 1080}\nproxies: [*x]',1)
