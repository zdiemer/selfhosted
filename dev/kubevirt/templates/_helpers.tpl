{{/*
Standard labels applied to every resource this chart owns.
*/}}
{{- define "kubevirt.labels" -}}
app.kubernetes.io/name: kubevirt
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Annotations that keep a CR alive through `helm uninstall`. Deleting either CR
tears down the component it describes — for the KubeVirt CR that means every
running VM on the cluster. See the warning at the top of values.yaml.
*/}}
{{- define "kubevirt.protect" -}}
{{- if .Values.protectCRs }}
helm.sh/resource-policy: keep
{{- end }}
{{- end -}}
