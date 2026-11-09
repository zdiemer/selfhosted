{{- define "adguard.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "adguard.labels" -}}
app.kubernetes.io/name: adguard
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "adguard.selectorLabels" -}}
app.kubernetes.io/name: adguard
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
