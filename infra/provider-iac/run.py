#!/usr/bin/env python3
"""Run pinned OpenTofu with vault credentials, encrypted state, and guarded plans."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
PROVIDERS = {'digitalocean': 'DIGITALOCEAN_TOKEN', 'cloudflare': 'CLOUDFLARE_API_TOKEN',
             'tailscale': 'TAILSCALE_API_KEY'}


def encryption(passphrase):
    # HCL interpolation must not interpret vault passphrases as expressions.
    literal = json.dumps(passphrase).replace('${', '$${').replace('%{', '%%{')
    return (f'key_provider "pbkdf2" "vault" {{ passphrase = {literal} }}\n'
            'method "aes_gcm" "vault" { keys = key_provider.pbkdf2.vault }\n'
            'state {\n method = method.aes_gcm.vault\n enforced = true\n}\n'
            'plan {\n method = method.aes_gcm.vault\n enforced = true\n}\n')


def check_plan(plan, stack, adoption=False):
    """Reject replacement/deletion and accidental new production resources."""
    if plan.get('errored') or plan.get('complete') is False:
        raise ValueError('Plan is incomplete or errored')
    for resource in plan.get('resource_changes', []):
        if resource.get('mode') == 'data':
            continue
        action = resource['change']['actions']
        address = resource['address']
        if action == ['no-op']:
            continue
        if adoption:
            raise ValueError('Adoption must be import/no-op only: ' + address)
        if action not in [['update'], ['create']]:
            raise ValueError('Deletion/replacement/unsupported action refused: ' + address)
        kind = resource['type']
        allowed = {'digitalocean': {'digitalocean_droplet', 'digitalocean_firewall'},
                   'cloudflare': {'cloudflare_dns_record'}, 'tailscale': {'tailscale_acl'}}
        if kind not in allowed[stack]:
            raise ValueError('Unexpected resource type: ' + address)
        if action == ['create'] and not (stack == 'digitalocean' and kind == 'digitalocean_firewall'
                                        and address in ['digitalocean_firewall.managed["whatnow"]',
                                                        'digitalocean_firewall.managed["egress"]']):
            raise ValueError('Existing resources must be imported, never recreated: ' + address)
        if kind == 'digitalocean_droplet':
            before, after = resource['change']['before'], resource['change']['after']
            changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            if changed - {'backups', 'backup_policy', 'resize_disk'} or after.get('backups') is not True:
                raise ValueError('Only backup policy and non-destructive resize preference may change: ' + address)
            if after.get('resize_disk') is not False:
                raise ValueError('Permanent disk resize must stay disabled: ' + address)
    return True


def vault_read(reference):
    if not reference.startswith('op://'):
        raise ValueError('Credentials must be 1Password references, never literal tokens')
    result = subprocess.run(['op', 'read', reference], capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        raise ValueError('Unable to read required vault reference: ' + reference)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stack', choices=PROVIDERS)
    parser.add_argument('action', choices=['validate', 'plan', 'adopt', 'apply'])
    parser.add_argument('--vars', type=Path)
    parser.add_argument('--credential-ref')
    parser.add_argument('--state-key-ref', default='op://homelab/provider-iac-state/passphrase')
    parser.add_argument('--tofu', default='tofu')
    args = parser.parse_args()
    os.umask(0o077)
    env = os.environ.copy()
    # Explicit log flags and inherited CLI arguments can leak or bypass guardrails.
    for key in list(env):
        if key.startswith(('TF_LOG', 'TF_CLI_ARGS')) or key in ['TF_WORKSPACE', 'TF_ENCRYPTION']:
            env.pop(key)
    env['TF_IN_AUTOMATION'] = '1'
    state_dir = Path.home() / '.local/state/selfhosted-provider-iac' / args.stack
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    env['TF_DATA_DIR'] = str(state_dir / 'plugins')
    # One writer per local state. Use this workspace only until a shared backend exists.
    with (state_dir / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        key = secrets.token_urlsafe(48) if args.action == 'validate' else vault_read(args.state_key_ref)
        if len(key) < 32:
            raise ValueError('State encryption passphrase must contain at least 32 characters')
        env['TF_ENCRYPTION'] = encryption(key)
        if args.action != 'validate':
            reference = args.credential_ref or f'op://homelab/provider-iac-{args.stack}/token'
            for name in PROVIDERS.values():
                env.pop(name, None)
            if args.stack == 'tailscale':
                for name in ['TAILSCALE_OAUTH_CLIENT_ID', 'TAILSCALE_OAUTH_CLIENT_SECRET', 'TAILSCALE_IDENTITY_TOKEN']:
                    env.pop(name, None)
            env[PROVIDERS[args.stack]] = vault_read(reference)
        def run(*arguments, capture=False):
            return subprocess.run([args.tofu, '-chdir=' + str(ROOT / args.stack), *arguments],
                                  env=env, check=True, text=True, capture_output=capture)
        version = json.loads(run('version', '-json', capture=True).stdout)['terraform_version']
        if version != '1.13.2':
            raise ValueError('Use the pinned OpenTofu 1.13.2 binary')
        if args.action == 'validate':
            run('init', '-backend=false', '-input=false', '-lockfile=readonly')
            run('validate')
            return
        run('init', '-input=false', '-lockfile=readonly', '-reconfigure',
            '-backend-config=path=' + str(state_dir / 'terraform.tfstate'))
        plan = state_dir / 'reviewed.tfplan'
        if args.action in ['plan', 'adopt']:
            # Never leave an earlier plan available after a failed new plan.
            plan.unlink(missing_ok=True)
            if not args.vars or not args.vars.is_file():
                raise ValueError('--vars must name reviewed, explicit inventory inputs')
            try:
                run('plan', '-input=false', '-lock-timeout=30s', '-var-file=' + str(args.vars.resolve()),
                    '-out=' + str(plan))
                parsed = json.loads(run('show', '-json', str(plan), capture=True).stdout)
                check_plan(parsed, args.stack, adoption=args.action == 'adopt')
                if args.action == 'adopt':
                    run('apply', '-input=false', '-lock-timeout=30s', str(plan))
                    plan.unlink()
                else:
                    print('Guarded encrypted plan saved; review the displayed diff, then run apply within 15 minutes.')
            except BaseException:
                plan.unlink(missing_ok=True)
                raise
        else:
            if not plan.is_file() or time.time() - plan.stat().st_mtime > 900:
                raise ValueError('Plan absent or older than 15 minutes; generate and review a fresh plan')
            check_plan(json.loads(run('show', '-json', str(plan), capture=True).stdout), args.stack)
            try:
                run('apply', '-input=false', '-lock-timeout=30s', str(plan))
            finally:
                plan.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Do not print captured provider responses, command environments or tokens.
        print('provider-iac: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
