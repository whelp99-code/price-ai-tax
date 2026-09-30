from __future__ import annotations
import importlib.util
from pathlib import Path
import json
import pytest


def runtime_module():
    path=Path(__file__).resolve().parents[1]/'scripts/runtime.py'
    spec=importlib.util.spec_from_file_location('runtime_recovery_under_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def configured(monkeypatch,tmp_path,api_alive,worker_alive):
    module=runtime_module();root=tmp_path/'project';root.mkdir();data=root/'.data';data.mkdir();(data/'demo.sqlite3').write_bytes(b'synthetic-placeholder')
    monkeypatch.setattr(module,'ROOT',root);monkeypatch.setattr(module,'DATA',data);monkeypatch.setattr(module,'STATE',data/'runtime.json')
    commands={'api':[module.sys.executable,'-m','price','--db',str(data/'demo.sqlite3'),'serve','--host','127.0.0.1','--port',str(module.PORT)],'worker':[module.sys.executable,'-m','price.worker','--db',str(data/'demo.sqlite3')]}
    known={};state={'schema':'price.local-runtime/1','root':str(root)}
    for name,pid,alive in [('api',101,api_alive),('worker',102,worker_alive)]:
        ident={'cwd':str(root),'args':commands[name],'started':str(pid)}
        state[name]={'pid':pid,'identity':ident,'log':str(data/(name+'.log'))}
        if alive: known[pid]=ident
    module.save(state)
    monkeypatch.setattr(module,'identity',lambda pid:known.get(pid))
    monkeypatch.setattr(module,'health',lambda:{'ok':True,'project':'price','live_filing':False})
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    calls=[]
    class FakeProcess:
        def __init__(self,command,**kwargs):
            self.pid=200+len(calls);self.command=command;self.killed=False
            calls.append(self);known[self.pid]={'cwd':str(root),'args':command,'started':str(self.pid)}
        def poll(self): return None
        def terminate(self): self.killed=True;known.pop(self.pid,None)
        def wait(self,timeout=None): return 0
    monkeypatch.setattr(module.subprocess,'Popen',FakeProcess)
    class FakeSocket:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def bind(self,address):return None
    monkeypatch.setattr(module.socket,'socket',lambda *args,**kwargs:FakeSocket())
    return module,calls


def test_start_repairs_missing_worker_without_replacing_api(monkeypatch,tmp_path):
    module,calls=configured(monkeypatch,tmp_path,True,False)
    state=module.start()
    assert state['api_running'] and state['worker_running']
    assert len(calls)==1 and 'price.worker' in calls[0].command
    assert state['api_pid']==101 and not calls[0].killed


def test_start_repairs_missing_api_without_replacing_worker(monkeypatch,tmp_path):
    module,calls=configured(monkeypatch,tmp_path,False,True)
    state=module.start()
    assert state['api_running'] and state['worker_running']
    assert len(calls)==1 and calls[0].command[2]=='price'
    assert state['worker_pid']==102


def test_start_is_idempotent_when_both_owned_components_alive(monkeypatch,tmp_path):
    module,calls=configured(monkeypatch,tmp_path,True,True)
    state=module.start()
    assert state['api_running'] and state['worker_running'] and calls==[]


def test_optional_finite_schedule_worker_does_not_replace_other_workers(monkeypatch,tmp_path):
    module,calls=configured(monkeypatch,tmp_path,True,True)
    state=module.start(bookkeeping=True)
    assert state['api_pid']==101 and state['worker_pid']==102
    assert len(calls)==1 and 'price.book_schedule_worker' in calls[0].command
    assert state['bookkeeping_worker_running'] and state['bookkeeping_worker_enabled']
    module.start()
    assert len(calls)==1
