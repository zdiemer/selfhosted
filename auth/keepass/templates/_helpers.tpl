{{- define "keepass.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "keepass.labels" -}}
app.kubernetes.io/name: keepass
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "keepass.webdav.selectorLabels" -}}
app.kubernetes.io/name: keepass
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: webdav
{{- end -}}

{{- define "keepass.keeweb.selectorLabels" -}}
app.kubernetes.io/name: keepass
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: keeweb
{{- end -}}
