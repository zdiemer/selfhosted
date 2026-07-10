{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "gamedex.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "gamedex.labels" -}}
app.kubernetes.io/name: gamedex
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "gamedex.selectorLabels" -}}
app.kubernetes.io/name: gamedex
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
