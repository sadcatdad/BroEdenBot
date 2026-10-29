import json
import io
from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest
import scripts.self_hosted_deploy as deployment

from scripts.self_hosted_deploy import (
    DATABASES, check_drop_window, select_git_override, snapshot, validate_config,
)


def test_snapshot_keeps_committed_wal_data_and_private_runtime_files(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    connections = []
    try:
        for name in DATABASES:
            db = sqlite3.connect(state / name)
            connections.append(db)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE checks(value TEXT)')
            db.execute("INSERT INTO checks VALUES ('committed in WAL')")
            db.commit()
        assets = state / 'visual-assets'
        assets.mkdir()
        (assets / 'upload.txt').write_text('member upload')
        (tmp_path / 'service.env').write_text('EXAMPLE_SETTING=private')
        destination = tmp_path / 'deployments' / 'test' / 'before'
        snapshot(tmp_path, destination)
        for name in DATABASES:
            with sqlite3.connect(destination / name) as db:
                assert db.execute('SELECT value FROM checks').fetchone() == ('committed in WAL',)
        assert (destination / 'visual-assets/upload.txt').read_text() == 'member upload'
        assert (destination / 'service.env').stat().st_mode & 0o777 == 0o600
        assert destination.stat().st_mode & 0o777 == 0o700
    finally:
        for db in connections:
            db.close()


def test_snapshot_rejects_a_destination_outside_private_deployments(tmp_path):
    with pytest.raises(RuntimeError, match='Snapshots must stay'):
        snapshot(tmp_path, tmp_path / 'source')


def test_production_config_rejects_seed_mode_or_wrong_data_mount(tmp_path):
    config = {'services': {'app': {
        'environment': {'BROEDEN_SEED_MODE': 'false'},
        'volumes': [{'type': 'bind', 'source': str(tmp_path / 'state'), 'target': '/data'}],
        'ports': [{'host_ip': '127.0.0.1', 'target': 3000}],
        'restart': 'unless-stopped',
    }}}
    validate_config(config, tmp_path)
    config['services']['app']['environment']['BROEDEN_SEED_MODE'] = 'true'
    with pytest.raises(RuntimeError, match='seed mode'):
        validate_config(config, tmp_path)
    config['services']['app']['environment']['BROEDEN_SEED_MODE'] = 'false'
    config['services']['app']['volumes'][0]['source'] = str(tmp_path / 'wrong-data')
    with pytest.raises(RuntimeError, match='persistent data mount'):
        validate_config(config, tmp_path)


def test_compose_selection_keeps_existing_values_and_selects_production_override():
    value = select_git_override('EXAMPLE=value\nCOMPOSE_FILE=old.yaml\nCOMPOSE_PATH_SEPARATOR=;\n')
    assert 'EXAMPLE=value\n' in value
    assert value.count('COMPOSE_FILE=') == 1
    assert 'compose.production.yaml:compose.git.yaml' in value
    assert 'COMPOSE_PATH_SEPARATOR=:' in value


def test_open_claims_and_imminent_drops_prevent_a_restart(tmp_path):
    (tmp_path / 'state').mkdir()
    with sqlite3.connect(tmp_path / 'state/data.db') as db:
        db.executescript('''
            CREATE TABLE event_drops(status TEXT, expires_at REAL);
            CREATE TABLE event_drop_campaigns(status TEXT, next_drop_at REAL);
            INSERT INTO event_drops VALUES ('active',2000);
        ''')
    with pytest.raises(RuntimeError, match='accepting claims'):
        check_drop_window(tmp_path, now=1000)
    with sqlite3.connect(tmp_path / 'state/data.db') as db:
        db.execute("UPDATE event_drops SET status='deleted'")
        db.execute("INSERT INTO event_drop_campaigns VALUES ('active',1100)")
    with pytest.raises(RuntimeError, match='within five minutes'):
        check_drop_window(tmp_path, now=1000)
    check_drop_window(tmp_path, now=1200)


def test_failed_new_code_keeps_newer_writes_when_old_code_is_restarted(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    source = tmp_path / 'source'
    source.mkdir()
    (root / 'state').mkdir(parents=True)
    for name in DATABASES:
        with sqlite3.connect(root / 'state' / name) as db:
            db.execute('CREATE TABLE entries(value TEXT)')
    for name in deployment.COMPOSE_FILES:
        (root / name).write_text('{}')
    (root / '.env').write_text('COMPOSE_FILE=original\n')
    (root / 'service.env').write_text('EXAMPLE=value\n')
    (root / 'SOURCE-COMMIT').write_text('previous\n')
    image = 'broedenbot:git-' + 'a' * 12
    config = {'services': {'app': {
        'image': 'broedenbot:previous',
        'environment': {'BROEDEN_SEED_MODE': 'false'},
        'volumes': [{'type': 'bind', 'source': str(root / 'state'), 'target': '/data'}],
        'ports': [{'host_ip': '127.0.0.1'}], 'restart': 'unless-stopped',
    }}}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        member = tarfile.TarInfo('Dockerfile')
        content = b'FROM python:3.13-slim\n'
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    monkeypatch.setattr(deployment.subprocess, 'check_output', lambda *a, **kw: archive.getvalue())
    monkeypatch.setattr(deployment.shutil, 'disk_usage', lambda *a: SimpleNamespace(free=100 * 1024**3))

    def capture(args):
        if 'rev-parse' in args:
            return 'a' * 40
        if 'status' in args:
            return ''
        if 'config' in args:
            return json.dumps(config)
        raise AssertionError(args)

    def command(args, **kwargs):
        if '--snapshot' in args:
            i = args.index('--snapshot')
            snapshot(Path(args[i + 1]), Path(args[i + 2]))
        elif 'up' in args and (root / 'compose.git.yaml').exists():
            with sqlite3.connect(root / 'state/data.db') as db:
                db.execute("INSERT INTO entries VALUES ('new activity after startup')")

    restarted = []

    def ready(root, requested, **kwargs):
        if requested == image:
            raise RuntimeError('New dashboard did not become healthy')
        restarted.append(requested)

    monkeypatch.setattr(deployment, 'capture', capture)
    monkeypatch.setattr(deployment, 'command', command)
    monkeypatch.setattr(deployment, 'wait_ready', ready)
    with pytest.raises(RuntimeError, match='dashboard did not'):
        deployment.deploy(SimpleNamespace(root=root, source=source, plan=False, check=False))
    assert restarted == ['broedenbot:previous']
    assert not (root / 'compose.git.yaml').exists()
    assert (root / '.env').read_text() == 'COMPOSE_FILE=original\n'
    assert (root / 'SOURCE-COMMIT').read_text() == 'previous\n'
    with sqlite3.connect(root / 'state/data.db') as db:
        assert db.execute('SELECT value FROM entries').fetchall() == [('new activity after startup',)]
    backup = next((root / 'deployments').glob('*/after-failure/data.db'))
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 1
