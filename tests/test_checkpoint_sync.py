"""Backup target and remote verification safeguards, without any network credentials."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec=importlib.util.spec_from_file_location('facai_checkpoint',Path(__file__).parents[1]/'scripts/checkpoint.py')
checkpoint=importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


@pytest.mark.parametrize('result,complete,expected',[(0,True,0),(0,False,2),(128,False,2),(128,True,0)])
def test_new_repository_verifies_both_refs_without_persisting_credentials(tmp_path,monkeypatch,capsys,result,complete,expected):
    def real(*args):return subprocess.run(['git',*args],cwd=tmp_path,capture_output=True,check=True)
    real('init','-b','facai/production-build')
    (tmp_path/'README.md').write_text('source')
    (tmp_path/'github-backup.json').write_text(json.dumps({'repository':'lwt131619/facai-latest-source-chatgpt','target_branch':'main'}))
    real('add','README.md','github-backup.json')
    real('-c','user.name=QA','-c','user.email=qa@example.invalid','commit','-m','initial')
    head=real('rev-parse','HEAD').stdout.decode().strip()
    token='synthetic-checkpoint-credential'
    monkeypatch.setenv('GH_TOKEN',token)
    monkeypatch.setattr(checkpoint,'ROOT',tmp_path)
    original=checkpoint.git
    pushes=[]
    def git(*args,check=True):
        if args[0]=='push':
            pushes.append(args)
            return subprocess.CompletedProcess(args,result,b'',b'permission denied' if result else b'')
        if args[0]=='ls-remote':
            refs=f'{head}\trefs/heads/main\n'
            if complete:refs+=f'{head}\trefs/heads/facai/production-build\n'
            return subprocess.CompletedProcess(args,0,refs.encode(),b'')
        return original(*args,check=check)
    monkeypatch.setattr(checkpoint,'git',git)
    assert checkpoint.checkpoint(validation='partial')==expected
    assert 'facai-latest-source-chatgpt.git' in pushes[0][1]
    assert 'HEAD:refs/heads/main' in pushes[0] and 'HEAD:refs/heads/facai/production-build' in pushes[0]
    assert '--force' not in pushes[0]
    state=(tmp_path/'dist/checkpoint-status.json').read_text()
    assert json.loads(state)['remote_verified'] is (expected==0)
    assert token not in state and token not in capsys.readouterr().out
    assert token not in (tmp_path/'.git/config').read_text()
    assert (tmp_path/'dist/FACAI-resume.bundle').is_file()


def test_checkpoint_refuses_recursive_tracked_bundle(tmp_path, monkeypatch):
    def real(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, check=True)
    real('init', '-b', 'facai/production-build')
    (tmp_path/'dist').mkdir()
    (tmp_path/'dist/FACAI-resume.bundle').write_bytes(b'previous backup')
    real('add', 'dist/FACAI-resume.bundle')
    real('-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid',
         'commit', '-m', 'legacy tracked backup')
    monkeypatch.setattr(checkpoint, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='recursive growth'):
        checkpoint.checkpoint(push=False)
