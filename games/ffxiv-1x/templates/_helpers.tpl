{{- define "ffxiv-1x.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ffxiv-1x.labels" -}}
app.kubernetes.io/name: ffxiv-1x
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "ffxiv-1x.selectorLabels" -}}
app.kubernetes.io/name: ffxiv-1x
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
