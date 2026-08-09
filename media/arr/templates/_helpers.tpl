{{/*
Fully-qualified resource name.
*/}}
{{- define "arr.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "arr.labels" -}}
app.kubernetes.io/name: arr
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels for one app in the stack. Call with (dict "root" . "app" "sonarr").
*/}}
{{- define "arr.appSelectorLabels" -}}
app.kubernetes.io/name: arr
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .app }}
{{- end -}}
