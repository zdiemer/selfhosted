{{- define "vmlab.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vmlab.labels" -}}
app.kubernetes.io/name: vmlab
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
The `component: ui` term is load-bearing, not decoration.

Session pods deliberately share app.kubernetes.io/name and /instance with the
UI so that they group together in kubectl and in cluster-status. But they also
expose a container port NAMED `http` (the console, on 8006), and this Service
targets `targetPort: http` by name. Without a term that separates them, session
pods become endpoints of the UI Service, and a request to
vmlab.zachd.duckdns.org round-robins between the real UI and a raw VM console —
bypassing the session proxy entirely. Observed in exactly that form before this
term was added.

Changing this means changing an immutable Deployment selector, so it needs a
delete-and-recreate rather than a plain upgrade.
*/}}
{{- define "vmlab.selectorLabels" -}}
app.kubernetes.io/name: vmlab
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: ui
{{- end -}}

{{/*
The ServiceAccount session pods run as. Deliberately distinct from the UI's,
holds no Role of any kind, and is mounted with automountServiceAccountToken:
false. A guest that escapes QEMU should find no credential waiting for it.
*/}}
{{- define "vmlab.vmServiceAccountName" -}}
{{ include "vmlab.fullname" . }}-vm
{{- end -}}

{{/*
Environment for the UI. Everything the app needs to build a session pod comes
from here rather than from the request, so the chart is the review surface for
what a VM may be.
*/}}
{{- define "vmlab.env" -}}
- name: VMLAB_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: VMLAB_LOG_LEVEL
  value: {{ .Values.log.level | quote }}
- name: VMLAB_SEED_CONFIGMAP
  value: {{ include "vmlab.fullname" . }}-catalog-seed
- name: VMLAB_USER_CONFIGMAP
  value: {{ include "vmlab.fullname" . }}-catalog-user
- name: VMLAB_ISO_PVC
  value: {{ include "vmlab.fullname" . }}-isos
- name: VMLAB_ISOS_ENABLED
  value: {{ .Values.isos.enabled | quote }}
- name: VMLAB_FETCH_IMAGE
  value: {{ .Values.isos.fetchImage | quote }}
- name: VMLAB_FETCH_TIMEOUT_SECONDS
  value: {{ .Values.isos.fetchTimeoutSeconds | quote }}
- name: VMLAB_FETCH_RESOURCES
  value: {{ .Values.isos.fetchResources | toJson | quote }}
- name: VMLAB_VM_SERVICE_ACCOUNT
  value: {{ include "vmlab.vmServiceAccountName" . }}
- name: VMLAB_VM_IMAGE
  value: {{ .Values.vm.image | quote }}
- name: VMLAB_VM_IMAGE_ARM
  value: {{ .Values.vm.imageArm | quote }}
- name: VMLAB_VM_TAG
  value: {{ .Values.vm.tag | quote }}
- name: VMLAB_VM_PULL_POLICY
  value: {{ .Values.vm.imagePullPolicy | quote }}
- name: VMLAB_DEFAULT_TTL_SECONDS
  value: {{ .Values.vm.defaultTtlSeconds | quote }}
- name: VMLAB_MAX_TTL_SECONDS
  value: {{ .Values.vm.maxTtlSeconds | quote }}
- name: VMLAB_IDLE_TIMEOUT_SECONDS
  value: {{ .Values.vm.idleTimeoutSeconds | quote }}
- name: VMLAB_MAX_CORES
  value: {{ .Values.vm.maxCores | quote }}
- name: VMLAB_MAX_MEMORY_MIB
  value: {{ .Values.vm.maxMemoryMib | quote }}
- name: VMLAB_MAX_DISK_GIB
  value: {{ .Values.vm.maxDiskGib | quote }}
- name: VMLAB_MAX_CONCURRENT_VMS
  value: {{ .Values.quota.maxConcurrentVms | quote }}
- name: VMLAB_TERMINAL_GRACE_SECONDS
  value: {{ .Values.vm.terminalGraceSeconds | quote }}
- name: VMLAB_SNAPSHOT_MOUNT
  value: "/snapshots"
- name: VMLAB_QMP_PORT
  value: {{ .Values.savepoints.qmpPort | quote }}
- name: VMLAB_PERSIST_STORAGE_CLASS
  value: {{ .Values.vm.persistStorageClass | quote }}
- name: VMLAB_CPU_REQUEST
  value: {{ .Values.vm.resources.cpuRequest | quote }}
- name: VMLAB_OVERHEAD_MEMORY_MIB
  value: {{ .Values.vm.resources.overheadMemoryMib | quote }}
- name: VMLAB_NETWORK_PROFILES
  value: {{ .Values.networkProfiles | toJson | quote }}
- name: VMLAB_EXCLUDE_NODE_LABELS
  value: {{ .Values.vm.excludeNodeLabels | toJson | quote }}
- name: VMLAB_PREFER_NODES
  value: {{ .Values.vm.preferNodes | toJson | quote }}
{{- end -}}
