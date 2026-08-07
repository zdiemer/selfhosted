{{/*
Fully-qualified resource name. Cluster-scoped objects, so no namespace to lean
on — the release name is the whole identity.
*/}}
{{- define "ingress-policy.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "ingress-policy.labels" -}}
app.kubernetes.io/name: ingress-policy
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
