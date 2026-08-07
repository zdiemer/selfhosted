# Weekly secrets backup timer

A systemd **user** timer that runs `scripts/secrets.sh backup --files-only`
every Sunday at 03:00, uploading an age-encrypted archive of every
`values.local.yaml` to `//192.168.4.36/backups/selfhosted-secrets`.

## Install

```sh
mkdir -p ~/.config/systemd/user
ln -sf ~/Code/selfhosted/scripts/systemd/selfhosted-secrets-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now selfhosted-secrets-backup.timer
loginctl enable-linger "$USER"
```

`enable-linger` is **not optional**. Without it systemd tears down the user
manager at logout and the timer never fires — silently, which is the one
failure mode a backup must not have.

## Check on it

```sh
systemctl --user list-timers selfhosted-secrets-backup.timer
journalctl --user -u selfhosted-secrets-backup.service -n 30
scripts/secrets.sh restore --verify-only     # decrypt the latest, list it, write nothing
```

A backup you have never decrypted is not a backup. `backup` already fetches and
decrypts each archive immediately after upload, but `restore --verify-only` is
the check to run by hand every so often.

## Why `--files-only`

The vault dump needs a live `op` session. An unattended run will never have one:
tokens expire after 30 minutes of inactivity, and Service Accounts are a
Teams/Business feature this account does not have. Files-only still restores
**every secret** — it only loses the vault's structure, which is a convenience
for rebuilding 1Password itself, not the secrets.

Without the flag the weekly run would emit a loud failure every single week and
train you to ignore the one alert that matters.

For a full archive including the vault dump, run it by hand:

```sh
eval $(op signin) && scripts/secrets.sh backup
```

Worth doing after any batch of vault changes. Archives are named with an
ISO-8601 UTC timestamp and the newest 20 are kept (`--keep N` to change);
pruning happens only *after* the new archive is verified, so a failed run never
costs an old good copy.

## Restoring

```sh
scripts/secrets.sh restore --verify-only   # inspect first
scripts/secrets.sh restore                 # unpack files/ back into the repo
```

Needs `~/.config/selfhosted/backup-age.key`. If that is gone too, transcribe it
from the paper copy — that copy is the primary recovery path, and the reason
`backup` refuses to run the first time until you confirm you have written it
down. The same sheet should carry your 1Password Secret Key and account
password, because `op account add` on a replacement machine needs them.
