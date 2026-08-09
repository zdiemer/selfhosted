{{/*
Fully-qualified resource name.
*/}}
{{- define "jellyseerr.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "jellyseerr.labels" -}}
app.kubernetes.io/name: jellyseerr
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "jellyseerr.selectorLabels" -}}
app.kubernetes.io/name: jellyseerr
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
