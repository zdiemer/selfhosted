{{/*
Fully-qualified resource name. Single-resource chart, so no suffix.
*/}}
{{- define "bluemap-ingress.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels. `app.kubernetes.io/name` is this chart, deliberately NOT
minecraft — the Ingress lands in the minecraft namespace beside the upstream
`mc` release, and must not look like it belongs to that release, or a
`helm upgrade` over there could decide it is orphaned and prune it.
*/}}
{{- define "bluemap-ingress.labels" -}}
app.kubernetes.io/name: bluemap-ingress
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
