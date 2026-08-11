{{- define "authelia.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "authelia.labels" -}}
app.kubernetes.io/name: authelia
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "authelia.selectorLabels" -}}
app.kubernetes.io/name: authelia
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Selector labels for the session store.

A DISTINCT app.kubernetes.io/name, not authelia's with a component label added.
The first version of this chart's redis reused authelia.selectorLabels and put
`component: redis` on top — but the selectors above match on name+instance
only, so an extra label narrows nothing and the redis pods matched Authelia's
own Deployment AND Service selectors.

Nothing broke, and that was luck: the Service left the redis pods out of its
endpoints only because they expose no port named `http`. The Deployment
selector overlap was real — `kubectl logs deploy/authelia` picked the redis
pod, and an orphaned redis pod would have been adoptable by Authelia's
ReplicaSet.

Selectors are immutable, so correcting this means deleting and recreating the
redis Deployment. Cheap, because that pod holds nothing by design.
*/}}
{{- define "authelia.redisSelectorLabels" -}}
app.kubernetes.io/name: authelia-redis
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
