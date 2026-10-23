# Finish KeePass adoption

Status: KeeWeb and WebDAV are deployed; the owner reports adoption unfinished.
The current review confirms ready Deployments, not vault contents, client sync
or successful recovery. Do not infer that the vault is empty or upload over it.

## Existing work

The [KeePass setup guide](../auth/keepass/README.md) describes the tailnet-only
WebDAV store, Authelia-protected KeeWeb, native clients and backup coverage.
Tracked values use hacdias WebDAV; some introductory README references still
name the older Bytemark image and should be reconciled during completion.

## Remaining work

- [ ] Establish which existing database is authoritative, which devices need it,
  and whether a key file is required. Do not assume this replaces 1Password's
  infrastructure-secret role.
- [ ] Back up the authoritative database before any upload or migration; inspect
  destination existence/metadata without printing vault contents or credentials.
- [ ] Configure the intended desktop and phone clients using supported WebDAV
  or sync workflows for their installed versions. Verify browser access separately.
- [ ] Test an agreed disposable entry across clients, including offline edits,
  conflict handling and reconnection. Confirm no changes are silently lost.
- [ ] Confirm key-file custody and recovery, if used. Keep recovery material
  separate from the database; do not rely solely on the service being recovered.
- [ ] Verify a current backup contains the vault and restore a copy to an isolated
  location. Have the owner unlock it without exposing the master password.
- [ ] Reconcile the README with the deployed image and the client workflows
  actually tested; record device onboarding and recovery instructions.

Done when the owner can use and synchronize the intended clients and recover
an independently restored vault. Preserve existing databases and credentials
until that validation is complete.
