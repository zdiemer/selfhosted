# ollama

Local LLM inference for the cluster: [Ollama](https://ollama.com) serving its
native API and OpenAI-compatible `/v1` endpoints at
`https://ollama.zachd.duckdns.org`, behind Traefik basic auth.

## Why it looks the way it does

**CPU-only.** No node in this fleet has a discrete GPU (checked, not assumed:
zachd-ubuntu-5 is a Ryzen 9 9955HX with the 2-CU Radeon 610M iGPU,
zachd-ubuntu-4 an i9-13900H with Iris Xe — neither is worth the driver
plumbing). CPU token generation is memory-*bandwidth*-bound, which drives two
choices here:

- **Pinned to zachd-ubuntu-4**, not the faster-on-paper ubuntu-5. Both are
  dual-channel DDR5, so they decode at nearly the same speed; ubuntu-4 simply
  had ~15Gi of unrequested memory where ubuntu-5 was already 66% committed.
- **Small models.** 8B at Q4 is the sweet spot (~8–15 tok/s); 14B fits the
  16Gi limit but is noticeably slower. If a bigger brain is ever wanted at CPU
  speeds, `qwen3:30b-a3b` (MoE, ~3B active) is the thing to try — it needs the
  memory limit raised to ~20Gi, which means renegotiating with the node's
  other tenants first.

**Basic auth, not Authelia.** Ollama has no authentication of its own, and
every client of this host is an API client that cannot complete an interactive
forward-auth login (the ntfy reasoning). The credential lives in
`op://homelab/ai-ollama` and the Ingress carries the
`public-unauthenticated` annotation only because infra/ingress-policy's
middleware marker doesn't recognize basicAuth — the host is gated.

## Using it

```bash
# Credential: `secrets show ai/ollama --reveal` (auth.basicAuth.smoke)
curl -u "ollama:$PASS" https://ollama.zachd.duckdns.org/api/generate \
  -d '{"model": "qwen3:8b", "prompt": "why is the sky blue?", "stream": false}'
```

OpenAI SDKs work against `/v1`, but send `Authorization: Bearer <key>` — which
Traefik's basicAuth won't accept. Override the header instead:

```python
import base64, openai
client = openai.OpenAI(
    base_url="https://ollama.zachd.duckdns.org/v1",
    api_key="unused",
    default_headers={"Authorization": "Basic " + base64.b64encode(b"ollama:PASS").decode()},
)
client.chat.completions.create(model="qwen3:8b", messages=[...])
```

In-cluster clients skip the gate entirely: `http://ollama.ai.svc:11434`, no
auth (ClusterIP is not published anywhere).

## Models

`ollama.models` in values.yaml is pulled in the background on every pod start;
the pod is Ready while pulls run, so a fresh install 404s on generate until
the first pull lands (watch `kubectl -n ai logs deploy/ollama` for `[pull]`
lines). One-off pulls without a deploy:

```bash
kubectl -n ai exec deploy/ollama -- ollama pull llama3.1:8b
```

Blobs live on a 60Gi truenas-iscsi PVC — a cache, not data; safe to delete and
re-pull.

## Deploy

```bash
./upgrade.sh
```

Waits for rollout, then checks the public host both ways: anonymous must get
401, the vault credential must get `/api/version`. Model presence is reported
but not asserted.
