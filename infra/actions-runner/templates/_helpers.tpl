{{- define "actions-runner.labels" -}}
app.kubernetes.io/name: actions-runner
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* The runner settings for one repo: runners.* with that repo's overrides on top. */}}
{{- define "actions-runner.repoSettings" -}}
{{- $repo := index . 0 -}}{{- $root := index . 1 -}}
{{- toYaml (mergeOverwrite (deepCopy $root.Values.runners) ($repo.overrides | default dict)) -}}
{{- end -}}
