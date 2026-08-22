{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "cloud-game.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "cloud-game.labels" -}}
app.kubernetes.io/name: cloud-game
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "cloud-game.selectorLabels" -}}
app.kubernetes.io/name: cloud-game
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Install prefix inside the image (upstream Dockerfile WORKDIR).
*/}}
{{- define "cloud-game.home" -}}/usr/local/share/cloud-game{{- end -}}
