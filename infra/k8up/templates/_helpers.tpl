{{- define "k8up-wrapper.labels" -}}
app.kubernetes.io/name: k8up-backups
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/*
The backend block, identical for every Schedule: point restic at the mounted
repository and give it the password. Rendered per-namespace because the secret
reference is namespaced.
*/}}
{{- define "k8up-wrapper.backend" -}}
repoPasswordSecretRef:
  name: k8up-restic-password
  key: password
local:
  mountPath: {{ .Values.repository.mountPath }}
volumeMounts:
  - name: restic-repo
    mountPath: {{ .Values.repository.mountPath }}
{{- end }}

{{/*
The repository volume, declared on each Schedule's backup spec so the backup pod
actually gets it. k8up mounts it via backend.volumeMounts above.
*/}}
{{- define "k8up-wrapper.repoVolume" -}}
- name: restic-repo
  persistentVolumeClaim:
    claimName: k8up-restic-repo
{{- end }}
