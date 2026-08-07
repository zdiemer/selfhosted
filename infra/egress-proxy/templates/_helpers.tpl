{{- define "egress-proxy.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "egress-proxy.labels" -}}
app.kubernetes.io/name: egress-proxy
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels. Never include Chart.Version — a Deployment's selector is
immutable, so a chart version bump would make the release un-upgradeable.
*/}}
{{- define "egress-proxy.selectorLabels" -}}
app.kubernetes.io/name: egress-proxy
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The in-cluster proxy URL for one client, credentials inlined.

This is the single place the URL is assembled, so every consumer — the client
Secret, the README, upgrade.sh's smoke test — agrees on its shape. Scheme is
plain http: this address is only ever dialled from inside the cluster, and the
CONNECT payload is the client's own TLS regardless. The hop that leaves the
house is the one that gets wrapped (see lanes.vps.tls).
*/}}
{{- define "egress-proxy.clientUrl" -}}
{{- $pw := index .root.Values.clientPasswords .client.name | default "" -}}
{{- printf "http://%s:%s@%s.%s.svc.cluster.local:%v" .client.name $pw (include "egress-proxy.fullname" .root) .root.Release.Namespace .root.Values.service.port -}}
{{- end -}}

{{/*
Squid ACL name for a client. Squid ACL names may not contain every character a
Kubernetes name may, so normalise once, here, rather than at each use.
*/}}
{{- define "egress-proxy.aclName" -}}
{{- printf "svc_%s" (. | replace "-" "_" | replace "." "_") -}}
{{- end -}}
