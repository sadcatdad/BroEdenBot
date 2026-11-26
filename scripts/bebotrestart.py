#!/usr/bin/env python3
"""Restart BroEdenBot using the SSH route already configured in bebotdeploy."""

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys


def deployment_route(definition):
    """Accept the existing simple SSH alias without evaluating shell code."""
    lines = [line.strip() for line in definition.splitlines() if line.strip()]
    if not lines:
        raise ValueError('bebotdeploy is not configured in zsh.')
    words = shlex.split(lines[-1])
    if (not words or Path(words[0]).name != 'ssh' or len(words) < 3
            or not words[-1].endswith('/deploy-from-git.sh')):
        raise ValueError('Expected bebotdeploy to be an SSH alias ending in /deploy-from-git.sh.')
    if any(word in (';', '&&', '||', '|') for word in words):
        raise ValueError('The SSH alias contains unsupported shell operators.')
    route = words[:-1]
    for index in range(1, len(route)):
        if route[index - 1] in ('-i', '-F', '-S', '-E'):
            route[index] = str(Path(route[index]).expanduser())
    if not any(word in ('-t', '-tt') for word in route[1:]):
        route.insert(1, '-t')
    return route, str(Path(words[-1]).parent)


def restart_command(route, root, *, plan=False, timeout=180):
    # Send public restart code as a quoted Python command, not through stdin:
    # stdin/TTY remain available for the existing SSH and sudo password prompts.
    # This works before the helper has been deployed to the server's checkout.
    source = Path(__file__).with_name('self_hosted_restart.py').read_text()
    args = ['bebotrestart', '--root', root, '--timeout', str(timeout)]
    if plan:
        args.append('--plan')
    launcher = (
        'import os,sys; '
        f'sys.argv={args!r}; '
        f'exec(compile({source!r}, "<bebotrestart>", "exec"), '
        f'{{"__name__":"__main__","__file__":os.path.expanduser({root + "/source/scripts/self_hosted_restart.py"!r})}})'
    )
    return [*route, shlex.join(['python3', '-c', launcher])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Check server configuration, current image, and safe restart window only.')
    parser.add_argument('--timeout', type=int, default=180, help='Readiness timeout in seconds (default 180).')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('--timeout must be between 1 and 600 seconds')
    try:
        result = subprocess.run(['zsh', '-ic', 'whence -f bebotdeploy'],
                                check=True, capture_output=True, text=True)
        route, root = deployment_route(result.stdout)
        command = restart_command(route, root, plan=args.plan, timeout=args.timeout)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, 'Could not prepare bebotrestart: ' + str(error) + '\n')
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
