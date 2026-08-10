# Timers

Four systemd **user** timers keep the plumbing honest without anyone
driving it:

| Timer | When | What |
|---|---|---|
| `selfhosted-secrets-sync` | every 15 min | reconcile every `values.local.yaml` with 1Password, both directions |
| `selfhosted-secrets-backup` | Sun 03:00 | age-encrypted archive + vault dump to `//192.168.4.36/backups/selfhosted-secrets` |
| `selfhosted-submodules-sync` | daily 04:30 | move submodule pins to the commits the cluster is actually running |
| `selfhosted-versions-report` | Sat 07:00 | node-lane staleness report (apt/kernel/k3s/tailscale) → versions-report ConfigMap (status.diemer.codes panel) + pinned GitHub issue |

All fail loudly: `OnFailure=selfhosted-alert@%n.service` texts through
`infra/sms-relay`. A scheduled job that fails silently is worse than no job,
because you stop checking.

## Why these exist

The 1Password migration fixed the real problem — there was no second copy of
the DuckDNS token, the Cloudflare tunnel tokens, or Authelia's OIDC key — but it
replaced "edit a gitignored file" with a ritual: `push` after every edit, `pull`
on every other machine, and an `op signin` in front of each. That is strictly
more work than the files it replaced, and work that gets skipped.

`sync` closes the loop. It runs unattended because
[`scripts/op-session.sh`](../op-session.sh) can now obtain a session with no
terminal, driving `op signin` through a pty
([why](../lib/op-signin-pty.py) — op has not accepted a password on stdin
since CLI 1.0). That same capability is why the weekly backup no longer runs
`--files-only` and has its vault dump back.

## One-time setup

```sh
mkdir -p ~/.config/systemd/user ~/.config/selfhosted
chmod 700 ~/.config/selfhosted

# 1. The account password, so sign-in needs no terminal.
install -m600 /dev/null ~/.config/selfhosted/op-password
printf '%s\n' 'YOUR-1PASSWORD-ACCOUNT-PASSWORD' > ~/.config/selfhosted/op-password
scripts/op-session.sh status          # expect: session valid

# 2. Failure texts (optional but recommended).
install -m600 /dev/null ~/.config/selfhosted/alert.env
cat > ~/.config/selfhosted/alert.env <<'EOF'
SMS_RELAY_URL=https://sms-relay.zachd.duckdns.org
SMS_RELAY_KEY=<a key from sms-relay's SMS_RELAY_API_KEYS map>
ALERT_TO=<your number>
EOF

# 3. Submodules read-only, so work never lands in one by accident.
scripts/submodules-lock.sh

# 4. The timers.
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-secrets-sync.{service,timer} ~/.config/systemd/user/
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-secrets-backup.{service,timer} ~/.config/systemd/user/
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-submodules-sync.{service,timer} ~/.config/systemd/user/
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-alert@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now selfhosted-secrets-sync.timer \
                              selfhosted-secrets-backup.timer \
                              selfhosted-submodules-sync.timer
loginctl enable-linger "$USER"
```

`enable-linger` is **not optional**. Without it systemd tears down the user
manager at logout and the timers never fire — silently, which is the one failure
mode a backup must not have.

### What lives where, and what is deliberately not backed up

| Path | What | In the NAS archive? |
|---|---|---|
| `~/.config/selfhosted/op-password` | 1Password account password | **no** |
| `~/.config/selfhosted/backup-age.key` | decrypts the archives | **no** |
| `~/.config/selfhosted/sync-state/` | sha256 of the last agreed content per item | no (rebuildable) |
| `$XDG_RUNTIME_DIR/selfhosted/op-session` | live session token, tmpfs, gone at logout | no |
| every `values.local.yaml` | the actual secrets | yes |

The first two are the keys to everything else, so they are not put inside the
thing they protect. Their recovery path is the paper sheet — the same one
`backup` refuses to run without on first use. Write the 1Password **Secret Key**
and **account password** on it alongside the age key: `op account add` on a
replacement machine needs both.

## Check on them

```sh
systemctl --user list-timers 'selfhosted-*'
journalctl --user -u selfhosted-secrets-sync.service -n 30
scripts/secrets.sh status                    # what sync thinks, without acting
scripts/secrets.sh sync --dry-run            # what it would do
scripts/secrets.sh restore --verify-only     # decrypt the latest archive, write nothing
```

A backup you have never decrypted is not a backup. `backup` already fetches and
decrypts each archive immediately after upload, but `restore --verify-only` is
the check to run by hand every so often.

## When sync stops

`sync` is deliberately unwilling to guess. Two states make it exit non-zero and
change nothing:

- **CONFLICT** — the file and the vault both changed since they last agreed.
  Pick a side: `secrets.sh pull --force <dir>` or `secrets.sh push <file>`.
- **DRIFT, no marker** — they disagree and there is no record of them ever
  agreeing, so there is no basis for choosing. Same fix; from then on it is
  arbitrated automatically.

Direction is decided against `~/.config/selfhosted/sync-state/`, a sha256 of
what both sides last held — not against mtimes. mtime cannot tell "the vault
moved" from "both moved", and the cost of getting that wrong is a silently
overwritten credential.

## Restoring

```sh
scripts/secrets.sh restore --verify-only   # inspect first
scripts/secrets.sh restore                 # unpack files/ back into the repo
```

Needs `~/.config/selfhosted/backup-age.key`. If that is gone too, transcribe it
from the paper copy — that copy is the primary recovery path.
