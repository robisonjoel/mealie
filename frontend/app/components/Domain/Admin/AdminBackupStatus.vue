<template>
  <v-sheet
    border
    rounded
    class="my-2 px-4 py-2 admin-backup-status"
    :style="{ borderLeft: accent ? `4px solid ${accent}` : undefined }"
  >
    <v-row>
      <v-col
        cols="12"
        :sm="cellWidth"
      >
        <div class="text-caption text-medium-emphasis">
          {{ $t("settings.backup.scheduled-backups") }}
        </div>
        <div>
          {{ scheduleText }}
        </div>
      </v-col>
      <v-col
        cols="12"
        :sm="cellWidth"
      >
        <div class="text-caption text-medium-emphasis">
          {{ $t("settings.backup.retention") }}
        </div>
        <div>
          {{ $t("settings.backup.retention-keep-last", { count: status.retentionCount }) }}
        </div>
      </v-col>
      <v-col
        cols="12"
        :sm="cellWidth"
      >
        <div class="text-caption text-medium-emphasis">
          {{ $t("settings.backup.last-run") }}
        </div>
        <div v-if="status.lastRun.outcome === 'never_run'">
          {{ $t("settings.backup.last-run-never") }}
        </div>
        <div v-else>
          <div v-if="status.lastRun.finishedAt">
            {{ $d(new Date(status.lastRun.finishedAt), "long") }}
          </div>
          <div
            class="last-run-outcome text-caption"
            :class="outcomeClass"
          >
            {{ outcomeText }}
          </div>
          <div
            v-if="status.lastRun.message"
            class="last-run-message text-caption"
            :class="messageClass"
          >
            {{ status.lastRun.message }}
          </div>
        </div>
      </v-col>
      <v-col
        v-if="status.s3.enabled"
        cols="12"
        :sm="cellWidth"
      >
        <div class="text-caption text-medium-emphasis">
          {{ $t("settings.backup.s3-bucket") }}
        </div>
        <div v-if="bucketText">
          {{ bucketText }}
        </div>
        <div
          class="s3-state text-caption"
          :class="s3Connected ? 'text-success' : 'text-error'"
        >
          {{ s3StateText }}
        </div>
      </v-col>
    </v-row>
  </v-sheet>
</template>

<script setup lang="ts">
import type { BackupStatusOut } from "~/lib/api/types/admin";

const props = defineProps<{
  status: BackupStatusOut;
}>();

const i18n = useI18n();

const failed = computed(() => ["backup_failed", "upload_failed"].includes(props.status.lastRun.outcome));

const cellWidth = computed(() => (props.status.s3.enabled ? 3 : 4));

const s3Connected = computed(() => props.status.s3.state === "connected");

const bucketText = computed(() => [props.status.s3.bucket, props.status.s3.prefix].filter(Boolean).join("/"));

const s3StateText = computed(() => {
  switch (props.status.s3.state) {
    case "connected":
      return i18n.t("settings.backup.s3-connected");
    case "misconfigured":
      return i18n.t("settings.backup.s3-misconfigured", { detail: props.status.s3.detail ?? "" });
    case "access_denied":
      return i18n.t("settings.backup.s3-access-denied");
    case "unreachable":
      return i18n.t("settings.backup.s3-unreachable");
    default:
      return i18n.t("settings.backup.s3-error");
  }
});

const cleanupFailed = computed(() => props.status.lastRun.outcome === "cleanup_failed");

const outcomeText = computed(() => {
  switch (props.status.lastRun.outcome) {
    case "backup_failed":
      return i18n.t("settings.backup.last-run-backup-failed");
    case "upload_failed":
      return i18n.t("settings.backup.last-run-upload-failed");
    case "cleanup_failed":
      return i18n.t("settings.backup.last-run-cleanup-failed");
    default:
      return i18n.t("settings.backup.last-run-succeeded");
  }
});

const messageClass = computed(() => (failed.value ? "text-error" : cleanupFailed.value ? "text-warning" : undefined));

const outcomeClass = computed(() => messageClass.value ?? "text-success");

const accent = computed(() => {
  if (failed.value) {
    return "rgb(var(--v-theme-error))";
  }
  if (cleanupFailed.value) {
    return "rgb(var(--v-theme-warning))";
  }
  if (props.status.scheduleEnabled) {
    return "rgb(var(--v-theme-success))";
  }
  return undefined;
});

const scheduleText = computed(() =>
  props.status.scheduleEnabled
    ? i18n.t("settings.backup.scheduled-daily-at", { time: props.status.scheduleTime })
    : i18n.t("settings.backup.scheduled-off"),
);
</script>
