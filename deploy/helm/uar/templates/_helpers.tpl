{{- define "uar.labels" -}}
app.kubernetes.io/part-of: uar
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "uar.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{- define "uar.pgPassword" -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (printf "%s-postgres" .Release.Name) -}}
{{- if and $existing $existing.data -}}
{{- index $existing.data "password" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}

{{- define "uar.restricted" -}}
runAsNonRoot: true
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: ["ALL"]}
seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{/* Runtime environment, shared by the migrate init container and the runtime container.
     PGPASSWORD is declared before UAR_DATABASE_URL so $(PGPASSWORD) expands. */}}
{{- define "uar.runtimeEnv" -}}
- {name: UAR_CONFIG, value: /config/uar.yaml}
- {name: UAR_BIND_HOST, value: 0.0.0.0}
{{- if .Values.postgres.enabled }}
- name: PGPASSWORD
  valueFrom: {secretKeyRef: {name: {{ .Release.Name }}-postgres, key: password}}
- {name: UAR_DATABASE_URL, value: "postgresql://uar:$(PGPASSWORD)@{{ .Release.Name }}-postgres:5432/uar"}
{{- else }}
- {name: UAR_DATABASE_URL, value: {{ required "postgres.externalUrl is required when postgres.enabled=false" .Values.postgres.externalUrl | quote }}}
{{- end }}
{{- range $k, $v := .Values.runtime.env }}
- {name: {{ $k }}, value: {{ $v | quote }}}
{{- end }}
{{- end -}}

{{- define "uar.runtimeEnvFrom" -}}
{{- if .Values.providerSecrets }}
- secretRef: {name: {{ .Release.Name }}-provider-secrets}
{{- end }}
{{- end -}}

{{- define "uar.runtimeMounts" -}}
- {name: config, mountPath: /config, readOnly: true}
- {name: tmp, mountPath: /tmp}
{{- end -}}
