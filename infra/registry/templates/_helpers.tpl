{{/*
Fully-qualified resource name.
*/}}
{{- define "registry.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "registry.labels" -}}
app.kubernetes.io/name: registry
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "registry.selectorLabels" -}}
app.kubernetes.io/name: registry
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The LAN endpoint nodes pull from. Plain HTTP on purpose: it never leaves the
cluster network, and the alternative — TLS on the registry itself — means a
second certificate to rotate for a hop that the ingress already covers.
*/}}
{{- define "registry.lanEndpoint" -}}
http://{{ .Values.service.clusterIP }}:{{ .Values.service.port }}
{{- end -}}

{{- define "registry.password" -}}
{{- required "registry.auth.password is empty — see values.local.yaml.example" .Values.registry.auth.password -}}
{{- end -}}
