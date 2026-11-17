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
        sm="4"
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
        sm="4"
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
        sm="4"
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
    </v-row>
  </v-sheet>
</template>

<script setup lang="ts">
import type { BackupStatusOut } from "~/lib/api/types/admin";

const props = defineProps<{
  status: BackupStatusOut;
}>();

const i18n = useI18n();

const failed = computed(() => props.status.lastRun.outcome === "backup_failed");

const cleanupFailed = computed(() => props.status.lastRun.outcome === "cleanup_failed");

const outcomeText = computed(() =>
  failed.value
    ? i18n.t("settings.backup.last-run-backup-failed")
    : cleanupFailed.value
      ? i18n.t("settings.backup.last-run-cleanup-failed")
      : i18n.t("settings.backup.last-run-succeeded"),
);

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
