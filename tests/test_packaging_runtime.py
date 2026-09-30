import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest
from price.worker import Queue

SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'install_standalone.py'
spec=importlib.util.spec_from_file_location('installer',SCRIPT);installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

@pytest.fixture
def source(tmp_path):
    root=tmp_path/'package';(root/'price').mkdir(parents=True)
    (root/'pyproject.toml').write_text('[project]\nname="price-tax-agent"')
    (root/'price'/'api.py').write_text('# synthetic installer fixture')
    (root/'.data').mkdir();(root/'.data'/'secret.sqlite3').write_text('private fixture')
    (root/'.chatgpt2codex').mkdir();(root/'.chatgpt2codex'/'local.log').write_text('must not export')
    return root

def test_installer_creates_only_new_standalone_source(source,tmp_path):
    target=tmp_path/'workspaces'/'price';r=installer.install(target,source)
    assert (target/'price'/'api.py').exists() and not (target/'.data').exists()
    assert not (target/'.chatgpt2codex').exists()
    assert not r['worker_registered'] and not r['service_started']
    before=(target/'price'/'api.py').read_bytes()
    with pytest.raises(ValueError):installer.install(target,source)
    assert (target/'price'/'api.py').read_bytes()==before

def test_installer_refuses_parent_repo_or_recursive(source,tmp_path):
    with pytest.raises(ValueError):installer.install(tmp_path/'CORP-OS'/'price',source)
    with pytest.raises(ValueError):installer.install(source/'price'/'price',source)
    with pytest.raises(ValueError):installer.install(tmp_path/'wrongname',source)

def test_worker_cli_logs_status_not_transaction_payload(w):
    tx=w.tx(counterparty='PRIVATE_SYNTHETIC_COUNTERPARTY_DO_NOT_LOG',business_purpose='PRIVATE_SYNTHETIC_PURPOSE_DO_NOT_LOG')
    Queue(w.svc).enqueue(w.owner,w.company,tx['id'])
    r=subprocess.run([sys.executable,'-m','price.worker','--db',str(w.db.path),'--once'],capture_output=True,text=True,check=True,timeout=15)
    out=json.loads(r.stdout)
    assert out['processed'] and out['state']=='SUCCEEDED'
    assert 'result' not in out and 'PRIVATE_SYNTHETIC' not in r.stdout
