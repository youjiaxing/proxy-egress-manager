"""Static release guard: reject unsafe defaults and accidental runtime artifacts."""
from pathlib import Path
import re
import yaml

root=Path(__file__).resolve().parents[1]
for name in ('data','.env','backups'):
    # Runtime paths can exist locally; never stage/include them in release build contexts.
    assert name in (root/'.dockerignore').read_text(), name+' missing from build exclusions'
for path in (root/'.github/workflows').glob('*.yml'):
    for line in path.read_text().splitlines():
        match=re.search(r'uses:\s*([^\s]+)',line)
        if match:
            assert re.search(r'@[a-f0-9]{40}$',match[1]), 'Unpinned action in '+str(path)
compose=yaml.safe_load((root/'compose.yaml').read_text())
for name,service in compose['services'].items():
    assert not service.get('privileged'),name
    for volume in service.get('volumes',[]):
        assert 'docker.sock' not in str(volume),name
assert 'ports' not in compose['services']['mihomo']
assert not any('1081' in str(p) or '1082' in str(p) for p in compose['services']['proxy-egress-manager'].get('ports',[]))
print('Release boundaries checked')
