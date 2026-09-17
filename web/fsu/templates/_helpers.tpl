{{/*
Fully-qualified resource name. Multi-component chart, so every resource appends
its own component suffix: {{ include "fsu.fullname" . }}-web, -terminal, -pybank.
*/}}
{{- define "fsu.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "fsu.labels" -}}
app.kubernetes.io/name: fsu
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels, per component. Call as:

    include "fsu.selectorLabels" (dict "root" . "component" "web")

Three Deployments in one chart means the component label is load-bearing rather
than decorative, and getting it wrong is the easiest way to fail
scripts/ci-lint-availability.sh: check-availability.py matches a PDB to a
Deployment by comparing spec.selector.matchLabels as a sorted tuple, EXACTLY. A
PDB selector missing the component label matches nothing, and the check reports
"replicas=2 but no PodDisruptionBudget" while the PDB sits right there.

Never include Chart.Version — selectors are immutable across upgrades.
*/}}
{{- define "fsu.selectorLabels" -}}
app.kubernetes.io/name: fsu
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}
