# Working in ~/Code

Source of truth: `~/Code/selfhosted/docs/code-tree-CLAUDE.md`, symlinked to
`~/Code/CLAUDE.md`. Claude Code reads `CLAUDE.md` from the working directory and
every parent, so one file there is inherited by every repo in this tree without
any of them tracking a copy. Edit it here, in git — not through the symlink's
directory listing.

It exists because the repos under `~/Code` are siblings that share one
deployment convention, and an agent that only sees `~/Code/gamedex` cannot infer
that convention from anything inside it.

## Secrets live in 1Password. `values.local.yaml` is not a file you write.

Every repo here that deploys to the homelab tracks a `values.local.tpl.yaml`
containing **nothing but `op://` references** — no values. `./upgrade.sh`
resolves that template at deploy time and hands helm the result on a pipe or a
tmpfs file that is removed on exit. There is no plaintext copy between deploys,
which is the entire point: secrets used to sit on ext4 next to each chart.

So, in any repo in this tree:

- Do **not** create, edit, or commit a `values.local.yaml`.
- Do **not** paste a resolved secret into `values.yaml` or a chart template.
- Do **not** invent an `op://` path. `secrets show <chart>` prints the real key
  paths with the values elided.

`.example` files are the human-readable record of shape and provenance — a
template of references cannot carry that. Read those, not the vault, to learn
what a chart expects.

## `./upgrade.sh` died at `op inject` — that is a missing session

The error is `FAIL: op inject failed. Signed in?`. It means there is no
1Password session in the environment. It does **not** mean a file is missing,
and the fix is never to reconstruct one by hand.

```bash
eval "$(~/Code/selfhosted/scripts/op-session.sh ensure)"
./upgrade.sh
```

`ensure` prints an eval-able `export OP_SESSION_…` line, reusing the cached
token when it is still valid and otherwise signing in unattended through a pty
using `~/.config/selfhosted/op-password`. The token is cached in tmpfs
(`$XDG_RUNTIME_DIR`), so this is once per login, not once per command. Prefer it
over interactive `op signin`, which cannot run in a hook, a timer, or an agent.

If `ensure` itself fails, `~/Code/selfhosted/scripts/op-session.sh status` says
whether the problem is a dead session or a missing password file. Stop there and
report it — do not route around it.

## The `secrets` CLI

`secrets` is on `$PATH` (bundled from `selfhosted/scripts/secrets.sh`). Vault
work needs no checkout of anything:

```
secrets status [path...]     per-chart: in-sync / drift / not-materialized
secrets show <chart>         key paths only, values elided
secrets edit <chart>         open the vault's copy in $EDITOR on tmpfs
secrets check                assert every reference still resolves, non-empty
```

`edit` and `new` take `--from-stdin`, which is how a non-interactive agent
should write a secret. `new` and `verify` are the two verbs that do need the
selfhosted checkout, and they say so.

If `secrets` warns that it was built from different sources than
`scripts/secrets.sh`, the copy on PATH is a stale bundle:
`~/Code/selfhosted/scripts/build-secrets-cli.sh --install`. The git hooks in
selfhosted now do this automatically, so seeing that warning means the hooks are
not enabled (`git config core.hooksPath .githooks`).

## Work in `~/Code/<repo>`, never in the submodule checkout

`gamedex`, `money`, `sms-relay`, `smitele-bot`, `whatnowgg`, `talaria`,
`diemer-codes` and `old-diemer-codes/site` exist **twice**: as a standalone
clone here, and as a submodule inside `~/Code/selfhosted`. The submodule pins
record *what the cluster is running*.

Do the work in `~/Code/<repo>`, push it, and only then bump the pointer in
selfhosted. A commit made inside a submodule worktree is pinned to a sha that
exists on no remote, and selfhosted's `pre-commit` hook rejects it.

## Deploying

Prefer each repo's `./upgrade.sh` over a raw `helm upgrade` — several do real
pre-flight work (Minecraft flushes the world and takes a backup first). If you
changed anything the image is built from, `./build.sh` first and bump the tag:
`imagePullPolicy: IfNotPresent` means reusing a tag will not re-pull.
