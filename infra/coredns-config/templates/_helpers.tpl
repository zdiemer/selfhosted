{{/*
Fully-qualified resource name. Single-component chart, so no suffix.

Note that the ConfigMap this chart owns does NOT use this — its name is fixed
at `coredns-custom` by the CoreDNS Deployment's volume spec. This is here for
labels and for anything added later.
*/}}
{{- define "coredns-config.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.

Deliberately NOT `app.kubernetes.io/name: coredns` — that is the label the
k3s-managed CoreDNS Addon puts on its own Deployment and Pods, and reusing it
here would make this config object answer selectors meant for the actual
resolver (including infra/alloy's, which selects CoreDNS pods by name). This
chart configures CoreDNS; it is not CoreDNS.
*/}}
{{- define "coredns-config.labels" -}}
app.kubernetes.io/name: coredns-config
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels. Never include Chart.Version: a Deployment's selector is
immutable, so a chart version bump would make the release un-upgradeable.
*/}}
{{- define "coredns-config.selectorLabels" -}}
app.kubernetes.io/name: coredns-config
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
