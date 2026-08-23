# Timers

Five systemd **user** timers keep the plumbing honest without anyone
driving it:

| Timer | When | What |
|---|---|---|
| `selfhosted-secrets-check` | every 15 min | assert every `op://` reference still resolves AND is non-empty |
| `selfhosted-secrets-backup` | Sun 03:00 | age-encrypted archive + vault dump to `//192.168.4.36/backups/selfhosted-secrets` |
| `selfhosted-submodules-sync` | daily 04:30 | move submodule pins to the commits the cluster is actually running |
| `selfhosted-versions-report` | Sat 07:00 | node-lane staleness report (apt/kernel/k3s/tailscale) → versions-report ConfigMap (status.diemer.codes panel) + pinned GitHub issue |
| `selfhosted-trading-watchdog` | Mon–Fri 17:00 | dead-man's switch: fail (→ SMS) if the trading agent's journal repo stopped moving (see `scripts/trading-watchdog.sh`) |

All fail loudly: `OnFailure=selfhosted-alert@%n.service` texts through
`infra/sms-relay`. A scheduled job that fails silently is worse than no job,
because you stop checking.

## Why these exist

The 1Password migration fixed the real problem — there was no second copy of
the DuckDNS token, the Cloudflare tunnel tokens, or Authelia's OIDC key — but it
replaced "edit a gitignored file" with a ritual: `push` after every edit, `pull`
on every other machine, and an `op signin` in front of each. That is strictly
more work than the files it replaced, and work that gets skipped.

`check` is what remains of that. The files are gone entirely now — every
`upgrade.sh` resolves its secrets into memory at deploy time (see
[`scripts/lib/secret-values.sh`](../lib/secret-values.sh)) — so there is no
second copy to drift, nothing to push, and nothing to reconcile. What can still
go wrong is quieter: a vault item renamed or deleted, a field emptied, a
reference that stops resolving. None of it surfaces until a deploy renders a
Secret with a blank value, which installs perfectly and fails at runtime.
`check` asserts every reference resolves and is non-empty, so the failure
arrives on a schedule with an SMS attached instead.

It runs unattended because
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
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-secrets-check.{service,timer} ~/.config/systemd/user/
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-secrets-backup.{service,timer} ~/.config/systemd/user/
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-submodules-sync.{service,timer} ~/.config/systemd/user/
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-alert@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now selfhosted-secrets-check.timer \
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
journalctl --user -u selfhosted-secrets-check.service -n 30
scripts/secrets.sh check                     # every reference resolves and is non-empty
scripts/secrets.sh verify                   # and renders the same manifests
scripts/secrets.sh restore --verify-only     # decrypt the latest archive, write nothing
```

A backup you have never decrypted is not a backup. `backup` already fetches and
decrypts each archive immediately after upload, but `restore --verify-only` is
the check to run by hand every so often.

## When check fails

`check` exits non-zero when any `op://` reference fails to resolve, or resolves
to nothing. Both mean the same thing in practice: the next deploy of that chart
renders a Secret with a missing or blank value, installs cleanly, and breaks at
runtime. Usual causes, in rough order of likelihood:

- **A field was emptied** while editing the item in the 1Password app. This is
  the one worth having a timer for — `op inject` does not fail on an empty
  field, it substitutes nothing.
- **An item was renamed.** The template names it by title
  (`op://homelab/<chart-path-with-dashes>/values.local.yaml`), so a rename in
  the app breaks the reference. Fix the title, or repoint the template.
- **No session.** `scripts/op-session.sh status` first.

Fix it with `secrets.sh edit <chart>`, then re-run `check`. There is no
CONFLICT state any more, and no `sync-state/` markers: those existed to arbitrate
between a file and the vault, and there is no longer a file.

## Restoring

```sh
scripts/secrets.sh restore --verify-only   # inspect first
scripts/secrets.sh restore                 # unpack files/ back into the repo
```

Needs `~/.config/selfhosted/backup-age.key`. If that is gone too, transcribe it
from the paper copy — that copy is the primary recovery path.
