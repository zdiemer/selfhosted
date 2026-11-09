# open-webui

A chat UI for [`ai/ollama`](../ollama). It lives at
**https://chat.zachd.duckdns.org** and is reachable over the tailnet only
(there are no `cloudflareHosts`).

## How login works

Authelia **is** the login. There is no OIDC client and no second password:

1. Traefik runs Authelia forward-auth on every request (`templates/ingress.yaml`).
2. On success, Authelia's `Remote-Email` / `Remote-Name` response headers
   **overwrite** whatever the client sent.
3. Open WebUI's trusted-header mode (`WEBUI_AUTH_TRUSTED_EMAIL_HEADER`) signs in
   as that email, creating the account the first time it sees one.

The NetworkPolicy admits Traefik **only**. That is load-bearing: any other pod
that could reach :8080 could send its own `Remote-Email` and become anyone,
including the admin. `upgrade.sh` checks both halves from outside: anonymous
requests and forged headers must both bounce.

The first account created becomes **admin**. Every account after that is
`pending` (`auth.defaultUserRole`) until the admin approves it in
*Admin Panel → Users*.

## Talking to Ollama

`OLLAMA_BASE_URL` is the in-cluster Service, so this skips the basic-auth gate
on `ollama.zachd.duckdns.org`. That gate is for the public host. Three things
have to agree:

| Where | What |
|---|---|
| `ai/ollama/templates/networkpolicy.yaml` | ingress from `app.kubernetes.io/name: open-webui` |
| `infra/application-isolation/values.yaml` | `open-webui` app + `ai/open-webui → ollama:11434` dependency |
| this chart | `ollamaBaseUrl` |

Ollama holds **one** model at a time (`maxLoadedModels: 1`, 16Gi limit, CPU
only). Picking a different model in the UI evicts the current one and
cold-loads the new one (~15–30s). Tag and follow-up generation are off, so a
chat doesn't evict its own model to run a side task. Title generation reuses
the chat model.

Pull new models through Ollama (`ai/ollama/values.yaml ollama.models`) or
from *Admin Panel → Settings → Models*. Either way they land on Ollama's PVC.

## Data and backups

`open-webui-data` (10Gi, `truenas-iscsi`) holds the SQLite DB (users, chats,
settings), uploaded files and the RAG vector store. infra/k8up backs up the
`ai` namespace on Fridays at 03:00. Ollama's model PVC opts out
(`k8up.io/backup=false`) because it is a re-pullable cache.

Many settings (Ollama URL, default role and others) are **PersistentConfig**:
the env var seeds the database on first boot, and after that the Admin Panel
value wins. Changing them in `values.yaml` later has no effect unless you
also set `ENABLE_PERSISTENT_CONFIG=false` under `env`.

## Deploy

```bash
# once: the session-signing key
printf 'secretKey: "%s"\n' "$(openssl rand -hex 32)" | secrets edit ai-open-webui --from-stdin

../ollama/upgrade.sh     # first install only, for the NetworkPolicy rule
../../infra/application-isolation/upgrade.sh
./upgrade.sh
```

Then browse to the host. The first login creates the admin.

## Security context

The container runs as the image's root user with **all capabilities dropped**
and no privilege escalation. The image writes under `/app` at start and does
not support an arbitrary UID. The `ai` namespace is PSA *baseline*, which
allows this.
