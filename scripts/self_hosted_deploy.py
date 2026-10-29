#!/usr/bin/env python3
"""Deploy the Docker bot without putting its private runtime directory in Git."""
import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

DATABASES = ('data.db', 'message_context.db', 'staff_context.db', 'brobank.db')
COMPOSE_FILES = ('compose.yaml', 'compose.tunnel.yaml', 'compose.production.yaml')


def command(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def capture(args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def atomic_write(path, text):
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(text)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def compose(root):
    args = ['sudo', 'docker', 'compose', '--project-name', 'broedenbot',
            '--project-directory', str(root)]
    for name in COMPOSE_FILES:
        args.extend(['--file', str(root / name)])
    if (root / 'compose.git.yaml').is_file():
        args.extend(['--file', str(root / 'compose.git.yaml')])
    return args


def validate_config(config, root):
    app = config['services']['app']
    if str(app.get('environment', {}).get('BROEDEN_SEED_MODE')).lower() != 'false':
        raise RuntimeError('Production seed mode must be false.')
    mounts = [v for v in app.get('volumes', []) if v.get('target') == '/data']
    if len(mounts) != 1 or mounts[0].get('type') != 'bind' or Path(mounts[0]['source']).resolve() != (root / 'state').resolve():
        raise RuntimeError('The persistent data mount does not match this server.')
    ports = app.get('ports', [])
    if not ports or any(p.get('host_ip') != '127.0.0.1' for p in ports):
        raise RuntimeError('Application ports must remain local-only.')
    if app.get('restart') != 'unless-stopped':
        raise RuntimeError('The production restart policy is missing.')


def check_drop_window(root, now=None):
    now = time.time() if now is None else now
    with sqlite3.connect('file:' + str(root / 'state/data.db') + '?mode=ro', uri=True) as database:
        tables = {r[0] for r in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'event_drops' not in tables:
            return
        count = database.execute("SELECT COUNT(*) FROM event_drops WHERE status IN ('pending','sending') OR (status='active' AND expires_at>?)", (now,)).fetchone()[0]
        if count:
            raise RuntimeError('An Event Drop is being delivered or accepting claims. Deploy after its claim window closes.')
        if 'event_drop_campaigns' in tables:
            upcoming = database.execute("SELECT MIN(next_drop_at) FROM event_drop_campaigns WHERE status='active' AND next_drop_at>?", (now,)).fetchone()[0]
            if upcoming and float(upcoming) < now + 300:
                raise RuntimeError('An Event Drop is due within five minutes. Deploy after its claim window closes.')


def snapshot(root, destination, owner=None, group=None):
    """Called with sudo after the single application container is stopped."""
    root = root.resolve()
    destination = destination.resolve()
    if not destination.is_relative_to(root / 'deployments'):
        raise RuntimeError('Snapshots must stay in this runtime directory.')
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in DATABASES:
        source = root / 'state' / name
        if not source.is_file():
            raise RuntimeError('Required database is missing: ' + name)
        with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as original:
            with sqlite3.connect(destination / name) as saved:
                original.backup(saved)
                if saved.execute('PRAGMA quick_check(1)').fetchall() != [('ok',)]:
                    raise RuntimeError('Snapshot integrity check failed: ' + name)
    assets = root / 'state/visual-assets'
    if assets.is_dir():
        shutil.copytree(assets, destination / 'visual-assets', symlinks=True)
    for name in (*COMPOSE_FILES, 'compose.git.yaml', '.env', 'service.env', 'SOURCE-COMMIT'):
        path = root / name
        if path.is_file():
            shutil.copyfile(path, destination / name)
    (destination / 'snapshot.json').write_text(json.dumps({'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'databases': list(DATABASES)}, indent=2) + '\n')
    for path in [destination, *destination.rglob('*')]:
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)
        if owner is not None:
            os.chown(path, owner, group)


def select_git_override(text):
    names = ':'.join((*COMPOSE_FILES, 'compose.git.yaml'))
    lines = [line for line in text.splitlines() if not line.startswith(('COMPOSE_FILE=', 'COMPOSE_PATH_SEPARATOR='))]
    return '\n'.join([*lines, 'COMPOSE_FILE=' + names, 'COMPOSE_PATH_SEPARATOR=:']) + '\n'


def wait_ready(root, image, timeout=300, log_path=None):
    deadline = time.monotonic() + timeout
    container = capture(compose(root) + ['ps', '--quiet', 'app'])
    if not container:
        raise RuntimeError('The application container did not start.')
    actual = capture(['sudo', 'docker', 'inspect', '--format', '{{.Image}}', container])
    wanted = capture(['sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    if actual != wanted:
        raise RuntimeError('The running application image does not match the requested release.')
    started = capture(['sudo', 'docker', 'inspect', '--format', '{{.State.StartedAt}}', container])
    while time.monotonic() < deadline:
        logs = capture(compose(root) + ['logs', '--no-color', '--since', started, 'app'])
        if log_path:
            atomic_write(log_path, logs)
        ready = re.search(r'is ready in [1-9][0-9]* guild\(s\)', logs)
        failures = re.search(r'Failed to load extension|Modules failed to load|heartbeat blocked|database is locked|Unknown interaction', logs, re.I)
        try:
            with urllib.request.urlopen('http://127.0.0.1:3000/health', timeout=5) as response:
                health = json.load(response)
            healthy = health.get('status') == 'ok' and health.get('database', {}).get('readable') is True
        except Exception:
            healthy = False
        if ready and healthy and not failures:
            return
        time.sleep(3)
    raise RuntimeError('Discord readiness and dashboard health did not verify. Inspect the private startup log.')


def deploy(args):
    root, source = args.root.resolve(), args.source.resolve()
    revision = capture(['git', '-C', source, 'rev-parse', 'HEAD'])
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise RuntimeError('A full Git commit ID is required.')
    if capture(['git', '-C', source, 'status', '--porcelain']):
        raise RuntimeError('The server source checkout has local changes.')
    for name in (*COMPOSE_FILES, 'service.env', '.env'):
        if not (root / name).is_file():
            raise RuntimeError('Runtime configuration is missing: ' + name)
    for name in DATABASES:
        if not (root / 'state' / name).is_file():
            raise RuntimeError('Persistent database is missing: ' + name)
    required = sum((root / 'state' / n).stat().st_size for n in DATABASES)
    assets = root / 'state/visual-assets'
    if assets.is_dir():
        required += sum(p.stat().st_size for p in assets.rglob('*') if p.is_file() and not p.is_symlink())
    if shutil.disk_usage(root).free < required * 2 + 2 * 1024**3:
        raise RuntimeError('There is not enough free space for pre-deploy and failure-recovery snapshots.')
    if args.plan:
        print('BOT DEPLOY PLAN VERIFIED: main commit ' + revision + '; private configuration and four databases are present. No build or restart performed.')
        return
    command(['sudo', '-v'])
    validate_config(json.loads(capture(compose(root) + ['config', '--format', 'json'])), root)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = root / 'deployments' / (stamp + '-' + revision[:12])
    folder.mkdir(mode=0o700, parents=True)
    release = folder / 'source'
    release.mkdir(mode=0o700)
    data = subprocess.check_output(['git', '-C', str(source), 'archive', '--format=tar', revision])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        if any(not (m.isfile() or m.isdir()) for m in archive.getmembers()):
            raise RuntimeError('The source archive contains unsupported links or special files.')
        if any(not (release / m.name).resolve().is_relative_to(release.resolve()) for m in archive.getmembers()):
            raise RuntimeError('The source archive contains a path outside the release.')
        if hasattr(tarfile, 'data_filter'):
            archive.extractall(release, filter='data')
        else:
            archive.extractall(release)
    image = 'broedenbot:git-' + revision[:12]
    print('Building and checking the new image while the current bot stays online.', flush=True)
    command(['sudo', 'docker', 'build', '--label', 'org.opencontainers.image.revision=' + revision, '--tag', image, release])
    command(['sudo', 'docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-m', 'pip', 'check'])
    command(['sudo', 'docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-m', 'compileall', '-q', '/app/main.py', '/app/cogs', '/app/utils', '/app/dashboard', '/app/scripts'])
    if args.check:
        print('BOT DEPLOY CHECK PASSED: candidate image verified; the production bot was not restarted.')
        return
    check_drop_window(root)
    override = root / 'compose.git.yaml'
    old_override = override.read_text() if override.exists() else None
    old_env = (root / '.env').read_text()
    old_metadata = {name: (root / name).read_text() if (root / name).is_file() else None
                    for name in ('SOURCE-COMMIT', 'deployment-result.json')}
    old_image = json.loads(capture(compose(root) + ['config', '--format', 'json']))['services']['app']['image']
    switched = False
    stopped = False
    backup_command = ['sudo', 'python3', str(Path(__file__).resolve()), '--snapshot', str(root), str(folder / 'before'), '--owner', str(os.getuid()), '--group', str(os.getgid())]
    try:
        command(['sudo', '-v'])
        print('Stopping the single bot instance and saving the current databases and assets.', flush=True)
        stopped = True
        command(compose(root) + ['stop', 'app'])
        check_drop_window(root)
        command(backup_command)
        atomic_write(override, json.dumps({'services': {'app': {'image': image, 'build': {'context': str(release), 'dockerfile': 'Dockerfile'}}}}, indent=2) + '\n')
        atomic_write(root / '.env', select_git_override(old_env))
        validate_config(json.loads(capture(compose(root) + ['config', '--format', 'json'])), root)
        switched = True
        command(compose(root) + ['up', '--detach', '--no-build', '--no-deps', 'app'])
        wait_ready(root, image, log_path=folder / 'startup.log')
        atomic_write(root / 'SOURCE-COMMIT', revision + '\n')
        atomic_write(root / 'deployment-result.json', json.dumps({'revision': revision, 'image': image, 'backup': str(folder / 'before'), 'verified_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2) + '\n')
        print('BOT DEPLOY VERIFIED: ' + revision + '\nBackup: ' + str(folder / 'before'))
    except BaseException:
        if stopped:
            print('Deployment did not verify. Restoring the previous code using the current data; no database backup will be restored.', file=sys.stderr, flush=True)
            command(compose(root) + ['stop', 'app'])
            if switched:
                recovery = list(backup_command)
                recovery[recovery.index(str(folder / 'before'))] = str(folder / 'after-failure')
                command(recovery)
            if old_override is None:
                override.unlink(missing_ok=True)
            else:
                atomic_write(override, old_override)
            atomic_write(root / '.env', old_env)
            for name, value in old_metadata.items():
                if value is None:
                    (root / name).unlink(missing_ok=True)
                else:
                    atomic_write(root / name, value)
            command(compose(root) + ['up', '--detach', '--no-build', '--no-deps', 'app'])
            wait_ready(root, old_image, log_path=folder / 'rollback.log')
            print('Previous code is running with the current data.', file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / 'broedenbot')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--snapshot', nargs=2, type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--owner', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--group', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.snapshot:
        snapshot(*args.snapshot, owner=args.owner, group=args.group)
        return
    if args.source is None:
        parser.error('--source is required; use the installed deploy command')
    try:
        deploy(args)
    except Exception as error:
        print('BOT DEPLOY FAILED: ' + str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
