{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "traefik-config.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.

Deliberately NOT `app.kubernetes.io/name: traefik` — that is the label the
k3s-managed Traefik release puts on its own Deployment/Service/Pods, and
reusing it here would make this config object answer selectors meant for the
actual proxy. This chart configures Traefik; it is not Traefik.
*/}}
{{- define "traefik-config.labels" -}}
app.kubernetes.io/name: traefik-config
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
