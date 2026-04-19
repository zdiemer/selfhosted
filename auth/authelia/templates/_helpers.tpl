{{- define "authelia.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "authelia.labels" -}}
app.kubernetes.io/name: authelia
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "authelia.selectorLabels" -}}
app.kubernetes.io/name: authelia
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
