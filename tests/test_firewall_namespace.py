"""Real nftables syntax and idempotency, only inside an isolated network namespace."""

import shutil
import subprocess
import sys

import pytest


def test_real_nft_rules_in_isolated_namespace():
    if not shutil.which("unshare") or not shutil.which("nft"):
        pytest.skip("unshare/nft not installed")
    supported = subprocess.run(
        ["unshare", "-Urn", "true"], capture_output=True, timeout=5
    )
    if supported.returncode:
        pytest.skip("unprivileged user/network namespaces disabled")
    script = '''
import json, subprocess
from uwucraft.firewall import apply
original = """add table inet testfilter
add chain inet testfilter incoming { type filter hook input priority 0; policy drop; }
add rule inet testfilter incoming tcp dport 22 accept comment "existing-ssh"
add table ip second
add chain ip second incoming { type filter hook input priority 10; policy drop; }
add rule ip second incoming counter comment "existing-counter"
"""
subprocess.run(['nft','-f','-'],input=original,text=True,check=True)
assert apply(25565) == {'state':'configured','chains':2}
first=json.loads(subprocess.check_output(['nft','-j','-a','list','ruleset']))
rules=[i['rule'] for i in first['nftables'] if 'rule' in i]
assert len(rules)==4
assert [r['comment'] for r in rules].count('uwucraft-tcp-25565')==2
assert any(r['comment']=='existing-ssh' for r in rules)
assert any(r['comment']=='existing-counter' for r in rules)
assert all(i['chain']['policy']=='drop' for i in first['nftables'] if 'chain' in i)
apply(25565)
second=json.loads(subprocess.check_output(['nft','-j','-a','list','ruleset']))
assert [i['rule'] for i in second['nftables'] if 'rule' in i]==rules
print('Real nft transaction and repeat: OK')
'''
    result = subprocess.run(
        ["unshare", "-Urn", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
