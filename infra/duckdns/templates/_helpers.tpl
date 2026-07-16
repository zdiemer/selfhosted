{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "duckdns.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Name of the token Secret. Rendered into both the release namespace (for the
updater CronJob) and kube-system (for Traefik) — a secretKeyRef cannot cross
namespaces, so each reader needs a copy in its own.

Traefik's HelmChartConfig names this secret, and the live one predates this
chart, so the default must keep resolving to `duckdns-token`.
*/}}
{{- define "duckdns.secretName" -}}
{{- printf "%s-token" (include "duckdns.fullname" .) -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "duckdns.labels" -}}
app.kubernetes.io/name: duckdns
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "duckdns.selectorLabels" -}}
app.kubernetes.io/name: duckdns
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
