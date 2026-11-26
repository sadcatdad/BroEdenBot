#!/usr/bin/env python3
"""Restart the deployed app image and reload service.env without a code deployment."""

import argparse
import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import self_hosted_deploy as deployment


def restart(args):
    root = args.root.expanduser().resolve()
    for name in (*deployment.COMPOSE_FILES, 'service.env', '.env'):
        if not (root / name).is_file():
            raise RuntimeError('Runtime configuration is missing: ' + name)
    for name in deployment.DATABASES:
        if not (root / 'state' / name).is_file():
            raise RuntimeError('Persistent database is missing: ' + name)
    with (root / '.deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A deployment or restart is already running.') from None
        deployment.command(['sudo', '-v'])
        compose = deployment.compose(root)
        config = json.loads(deployment.capture(compose + ['config', '--format', 'json']))
        deployment.validate_config(config, root)
        containers = deployment.capture(compose + ['ps', '--all', '--quiet', 'app']).split()
        if len(containers) != 1:
            raise RuntimeError('Expected exactly one existing app container; use bebotdeploy to establish the deployment.')
        image = deployment.capture(['sudo', 'docker', 'inspect', '--format', '{{.Image}}', containers[0]])
        configured = config['services']['app'].get('image')
        if not configured:
            raise RuntimeError('The production app must select its deployed image.')
        wanted = deployment.capture(['sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}', configured])
        if image != wanted:
            raise RuntimeError('The configured image differs from the deployed image. Use bebotdeploy to change releases.')
        try:
            deployment.check_drop_window(root)
        except RuntimeError as exc:
            raise RuntimeError(str(exc).replace('Deploy after', 'Restart after')) from None
        revision_file = root / 'SOURCE-COMMIT'
        revision = revision_file.read_text().strip() if revision_file.is_file() else ''
        release = revision[:12] if re.fullmatch(r'[0-9a-f]{40}', revision) else 'unrecorded revision'
        if args.plan:
            print('BOT RESTART PLAN VERIFIED: current image ' + image + '; release ' + release +
                  '. The app would reload service.env. No pull, build, or restart performed.')
            return
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        folder = root / 'restarts' / stamp
        folder.mkdir(mode=0o700, parents=True)
        folder.parent.chmod(0o700)
        log_path = folder / 'startup.log'
        print('Restarting the current bot and dashboard image and reloading service.env.', flush=True)
        # A plain `compose restart` retains the old environment. Recreate only
        # app with the already-verified local image; leave the tunnel running.
        deployment.command(compose + ['up', '--detach', '--no-build', '--no-deps',
                                     '--pull', 'never', '--force-recreate', 'app'])
        try:
            deployment.wait_ready(root, image, timeout=args.timeout, log_path=log_path)
        except Exception as exc:
            raise RuntimeError('Restart did not verify: ' + str(exc) + '\nPrivate startup log: ' + str(log_path)) from None
        print('BOT RESTART VERIFIED: release ' + release + '\nStartup log: ' + str(log_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / 'broedenbot')
    parser.add_argument('--plan', action='store_true', help='Validate the restart without changing containers.')
    parser.add_argument('--timeout', type=int, default=180, help='Seconds to wait for Discord and dashboard health (default 180).')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('--timeout must be between 1 and 600 seconds')
    try:
        restart(args)
    except Exception as error:
        print('BOT RESTART FAILED: ' + str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
