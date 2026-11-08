{{- define "feedreader.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "feedreader.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "feedreader.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "feedreader.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "feedreader.selectorLabels" -}}
app.kubernetes.io/name: {{ include "feedreader.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "feedreader.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{/* Built-in PostgreSQL */}}
{{- define "feedreader.postgresql.fullname" -}}
{{- printf "%s-postgresql" (include "feedreader.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "feedreader.postgresql.secretName" -}}
{{- default (include "feedreader.postgresql.fullname" .) .Values.postgresql.auth.existingSecret }}
{{- end }}

{{/* Connection settings the app uses, for both database modes. */}}
{{- define "feedreader.db.host" -}}
{{- if .Values.postgresql.enabled }}
{{- include "feedreader.postgresql.fullname" . }}
{{- else }}
{{- required "database.host is required (or set postgresql.enabled=true for the built-in test database)" .Values.database.host }}
{{- end }}
{{- end }}

{{- define "feedreader.db.port" -}}
{{- if .Values.postgresql.enabled }}5432{{ else }}{{ .Values.database.port }}{{ end }}
{{- end }}

{{- define "feedreader.db.name" -}}
{{- if .Values.postgresql.enabled }}{{ .Values.postgresql.auth.database }}{{ else }}{{ .Values.database.name }}{{ end }}
{{- end }}

{{/* Secret with the app's database credentials and the keys inside it. */}}
{{- define "feedreader.db.secretName" -}}
{{- if .Values.postgresql.enabled }}
{{- include "feedreader.postgresql.secretName" . }}
{{- else if .Values.database.existingSecret }}
{{- .Values.database.existingSecret }}
{{- else }}
{{- printf "%s-db" (include "feedreader.fullname" .) }}
{{- end }}
{{- end }}

{{- define "feedreader.db.env" -}}
- name: DB_HOST
  value: {{ include "feedreader.db.host" . | quote }}
- name: DB_PORT
  value: {{ include "feedreader.db.port" . | quote }}
- name: DB_NAME
  value: {{ include "feedreader.db.name" . | quote }}
{{- if .Values.postgresql.enabled }}
- name: DB_USER
  value: {{ .Values.postgresql.auth.username | quote }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "feedreader.db.secretName" . }}
      key: password
{{- else }}
- name: DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ include "feedreader.db.secretName" . }}
      key: {{ if .Values.database.existingSecret }}{{ .Values.database.userKey }}{{ else }}username{{ end }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "feedreader.db.secretName" . }}
      key: {{ if .Values.database.existingSecret }}{{ .Values.database.passwordKey }}{{ else }}password{{ end }}
{{- end }}
{{- end }}
