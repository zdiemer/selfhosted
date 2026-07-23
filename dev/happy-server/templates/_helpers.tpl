{{/*
Fully-qualified resource name.
*/}}
{{- define "happy-server.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "happy-server.labels" -}}
app.kubernetes.io/name: happy-server
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "happy-server.selectorLabels" -}}
app.kubernetes.io/name: happy-server
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
