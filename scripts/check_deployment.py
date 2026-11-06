"""Read-only, credential-free HTTPS and public-port acceptance from an external host."""
import http.client
import ipaddress
import os
import socket
import ssl

host = os.environ['DEPLOY_HOST']
expected = str(ipaddress.ip_address(os.environ['DEPLOY_IPV4']))
assert host and all(c.isalnum() or c in '.-' for c in host)
addresses = {r[4][0] for r in socket.getaddrinfo(host, 443, socket.AF_INET)}
assert addresses == {expected}, ('Unexpected public IPv4 DNS', addresses)
context = ssl.create_default_context()


def request(path, method='GET', body=None, headers=None, tls=True):
    cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
    kwargs = {'context': context} if tls else {}
    conn = cls(host, 443 if tls else 80, timeout=10, **kwargs)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read(65536)
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), data
    finally:
        conn.close()


status, headers, _ = request('/', tls=False)
assert status in (301, 308) and headers.get('location') == 'https://' + host + '/'
status, headers, body = request('/')
assert status == 200 and b'Proxy Egress Manager' in body
assert 'frame-ancestors' in headers.get('content-security-policy', '')
assert headers.get('strict-transport-security', '').startswith('max-age=')
assert request('/healthz')[0] == 200
for path in ('/api/state', '/api/session', '/api/diagnostics'):
    assert request(path)[0] == 401, path
assert request('/api/login', 'POST', '{}', {'Content-Type': 'application/json', 'Origin': 'https://untrusted.invalid'})[0] == 403
for port in (1081, 1082, 9090, 18080):
    try:
        connection = socket.create_connection((expected, port), timeout=3)
    except OSError:
        print('Public port', port, 'unreachable: PASS')
    else:
        connection.close()
        raise AssertionError('Unexpected public TCP listener: ' + str(port))
print('External DNS, trusted TLS, HTTP redirect, unauthenticated API rejection and public-port isolation: PASS')
