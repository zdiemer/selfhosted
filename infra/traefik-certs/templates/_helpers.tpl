{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "traefik-certs.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The Secret Traefik reads as its default certificate. Named separately from the
release because infra/traefik references it by name across a namespace boundary
and across a chart boundary — renaming it here silently serves Traefik's
self-signed default to every host in the cluster.
*/}}
{{- define "traefik-certs.certSecretName" -}}
{{- .Values.certSecret.name -}}
{{- end -}}

{{/*
The Secret holding lego's own state: its ACME account key plus the issued
certificate, as a tarball of lego's data directory.

This is what replaces the acme.json PVC. It exists because the account key MUST
survive between runs — losing it means re-registering with Let's Encrypt on
every renewal, and the rate limits that follow — and because keeping it in a
Secret rather than a volume is what stops this job being pinned to a node.
*/}}
{{- define "traefik-certs.stateSecretName" -}}
{{- printf "%s-lego-state" (include "traefik-certs.fullname" .) -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "traefik-certs.labels" -}}
app.kubernetes.io/name: traefik-certs
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "traefik-certs.selectorLabels" -}}
app.kubernetes.io/name: traefik-certs
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
