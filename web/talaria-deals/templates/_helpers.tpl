{{/*
Fully-qualified resource name. Single-resource chart, so no suffix.
*/}}
{{- define "talaria-deals.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels. `app.kubernetes.io/name` is this chart, deliberately NOT
talaria — the Ingress lands in talaria's namespace and must not look like it
belongs to talaria's release, or a `helm upgrade` over there could think it's
orphaned.
*/}}
{{- define "talaria-deals.labels" -}}
app.kubernetes.io/name: talaria-deals
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
