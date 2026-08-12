{{/* Release name is already short and unique; nothing to derive. */}}
{{- define "apartment-watch.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "apartment-watch.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "apartment-watch.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels deliberately exclude Chart.Version: they end up in immutable
selectors, and a chart version bump would make the upgrade fail.
*/}}
{{- define "apartment-watch.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
EGRESS PROXY OPT-IN. This is the shared contract from infra/egress-proxy —
copy-pasted rather than shared, following the same convention as the ingress
annotations (there is deliberately no library chart in this repo).

The Secret is rendered into this namespace by the egress-proxy release, so the
name is a constant here and this chart never holds a credential of its own.
`optional: true` so that a chart which has opted in before the proxy release has
caught up degrades to direct egress rather than leaving the pod unschedulable.

MODE IS NOT A STYLE CHOICE.

  explicit  Only EGRESS_PROXY_URL. The application decides what to do with it.
  env       Also sets HTTP_PROXY/HTTPS_PROXY, for a workload where every client
            honours the environment and nothing in-cluster is called.

apartment-watch must use `explicit`, and the reason is in src/egress.py: httpx
and urllib honour HTTP_PROXY, Camoufox does not. `env` would route the cheap
tier and the liveness sweep through the proxy while the browser tier — the one
that actually faces PerimeterX and Akamai — kept leaving direct, which is worse
than either address alone. It would also catch notify.py's post to sms-relay
inside the cluster, which NO_PROXY can exclude but only until someone fat-fingers
the list.
*/}}
{{- define "apartment-watch.egressEnv" -}}
{{- if .Values.egress.proxy.enabled -}}
- name: EGRESS_PROXY_URL
  valueFrom:
    secretKeyRef:
      name: egress-proxy-client
      key: url
      optional: true
{{- if eq .Values.egress.proxy.mode "env" }}
{{- range $n := list "HTTP_PROXY" "http_proxy" "HTTPS_PROXY" "https_proxy" }}
- name: {{ $n }}
  valueFrom:
    secretKeyRef:
      name: egress-proxy-client
      key: url
      optional: true
{{- end }}
{{- /*
  A CONSTANT, not a per-service value. Every in-cluster address has to be here
  or calls that never should have left the cluster get tunnelled out and back.
*/}}
{{- range $n := list "NO_PROXY" "no_proxy" }}
- name: {{ $n }}
  value: "localhost,127.0.0.1,::1,.svc,.svc.cluster.local,.cluster.local,10.42.0.0/16,10.43.0.0/16,192.168.4.0/24"
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
The label the egress-proxy NetworkPolicy selects on. A pod without it cannot
reach the listener at all, so this must be stamped wherever egressEnv is used.

Derived from `egress.proxy.enabled` rather than set by hand, so the proxy's
ingress allowlist cannot drift out of sync with who is actually configured to
use it — the same self-maintaining trick infra/alloy uses for log shipping.
*/}}
{{- define "apartment-watch.egressLabels" -}}
{{- if .Values.egress.proxy.enabled -}}
egress.zachd/proxied: "true"
{{- end }}
{{- end -}}

{{/*
The public origin, for the one place a relative URL will not do: og:image.
Scrapers do not resolve relative image URLs — they drop them and the preview
comes back as a grey box — so the tag has to name a host, and the app cannot
know its own from inside the pod.

Derived from the ingress rather than configured twice. The Cloudflare hostname
wins because that is the one that gets pasted into a text message; the DuckDNS
host is the way in when Cloudflare is not, not the name anyone shares.

This must agree with criteria.yaml's `alerts.web_base_url`, which is what the
SMS link itself is built from. They are separate on purpose — criteria.yaml is
a gitignored secret-ish file and the chart cannot read it — but if they drift,
the symptom is quiet: the link works and the preview points somewhere else.
*/}}
{{- define "apartment-watch.publicUrl" -}}
{{- if .Values.web.publicUrl -}}
{{- .Values.web.publicUrl | trimSuffix "/" -}}
{{- else if .Values.ingress.cloudflareHosts -}}
https://{{ first .Values.ingress.cloudflareHosts }}
{{- else -}}
https://{{ .Values.ingress.host }}
{{- end -}}
{{- end -}}
