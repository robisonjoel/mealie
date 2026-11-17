<template>
  <v-container fluid>
    <section>
      <!-- Delete Dialog -->
      <BaseDialog
        v-model="state.deleteDialog"
        bottom-sheet
        :title="$t('settings.backup.delete-backup')"
        color="error"
        :icon="$globals.icons.alertCircle"
        can-confirm
        @confirm="deleteBackup()"
      >
        <v-card-text>
          {{ $t("general.confirm-delete-generic") }}
        </v-card-text>
      </BaseDialog>

      <!-- Import Dialog -->
      <BaseDialog
        v-model="state.importDialog"
        bottom-sheet
        color="error"
        :title="$t('settings.backup.backup-restore')"
        :icon="$globals.icons.database"
      >
        <v-divider />
        <v-card-text>
          <i18n-t keypath="settings.backup.back-restore-description">
            <template #cannot-be-undone>
              <b> {{ $t('settings.backup.cannot-be-undone') }} </b>
            </template>
          </i18n-t>

          <p
            v-if="selectedSource === 's3'"
            class="mt-3 s3-restore-note"
          >
            {{ $t("settings.backup.s3-restore-note") }}
          </p>

          <p class="mt-3">
            <i18n-t keypath="settings.backup.postgresql-note">
              <template #backup-restore-process>
                <a class="text-primary" href="https://nightly.mealie.io/documentation/getting-started/usage/backups-and-restoring/">{{
                  $t('settings.backup.backup-restore-process-in-the-documentation') }}</a>
              </template>
            </i18n-t>
          </p>

          <v-checkbox
            v-model="state.confirmImport"
            class="checkbox-top"
            color="error"
            hide-details
            :label="$t('settings.backup.irreversible-acknowledgment')"
          />
        </v-card-text>
        <template #custom-card-action>
          <BaseButton
            delete
            :disabled="!state.confirmImport || state.runningRestore"
            @click="restoreBackup(selected)"
          >
            <template #icon>
              {{ $globals.icons.database }}
            </template>
            {{ $t('settings.backup.restore-backup') }}
          </BaseButton>
        </template>
        <p class="caption pb-0 mb-1 text-center">
          {{ selected }}
        </p>
        <v-progress-linear
          v-if="state.runningRestore"
          indeterminate
        />
      </BaseDialog>

      <section>
        <BaseCardSectionTitle :title="$t('settings.backup-and-exports')">
          <v-card-text class="py-0 px-1">
            <i18n-t keypath="settings.backup.experimental-description" />
          </v-card-text>
        </BaseCardSectionTitle>
        <AdminBackupStatus
          v-if="status"
          :status="status"
        />
        <v-toolbar
          color="transparent"
          flat
          class="justify-between"
        >
          <BaseButton
            class="mr-2"
            :loading="state.runningBackup"
            @click="createBackup"
          >
            {{ $t("settings.backup.create-heading") }}
          </BaseButton>
          <AppButtonUpload
            :text-btn="false"
            url="/api/admin/backups/upload"
            accept=".zip"
            color="info"
            @uploaded="refreshBackups()"
          />
          <v-spacer />
          <v-btn-toggle
            v-if="showToggle"
            v-model="view"
            mandatory
            divided
            variant="outlined"
            color="primary"
            density="comfortable"
            class="ml-2 flex-shrink-0 backup-view-toggle"
          >
            <v-btn value="local">
              {{ $t("settings.backup.on-this-server") }}
            </v-btn>
            <v-btn value="s3">
              {{ $t("settings.backup.s3-bucket") }}
            </v-btn>
          </v-btn-toggle>
        </v-toolbar>

        <v-alert
          v-if="viewingBucket && bucketListFailed"
          type="error"
          variant="tonal"
          class="my-2"
        >
          {{ $t("settings.backup.s3-list-failed") }}
        </v-alert>

        <v-data-table
          :headers="state.headers"
          :items="rows"
          class="elevation-0"
          :items-per-page="-1"
          hide-default-footer
          disable-pagination
          :search="state.search"
          @click:row="setSelected"
        >
          <template #[`item.date`]="{ item }">
            {{ $d(Date.parse(item.date)) }}
          </template>
          <template #[`item.actions`]="{ item }">
            <v-btn
              icon
              class="mx-1"
              color="error"
              variant="text"
              @click.stop="
                state.deleteDialog = true;
                pickDelete(item.name, currentSource);
              "
            >
              <v-icon> {{ $globals.icons.delete }} </v-icon>
            </v-btn>
            <BaseButton
              v-if="!viewingBucket"
              small
              download
              :download-url="backupsFileNameDownload(item.name)"
              class="mx-1"
              @click.stop="() => { }"
            />
            <BaseButton
              small
              @click.stop="setSelected(item); state.importDialog = true"
            >
              <template #icon>
                {{ $globals.icons.backupRestore }}
              </template>
              {{ $t("settings.backup.backup-restore") }}
            </BaseButton>
          </template>
        </v-data-table>
        <v-divider />
        <div class="d-flex justify-end mt-6">
          <div />
        </div>
      </section>
    </section>
    <v-container class="mt-4 d-flex justify-center text-center">
      <nuxt-link class="text-primary" :to="`/group/migrations`"> {{ $t('recipe.looking-for-migrations') }} </nuxt-link>
    </v-container>
  </v-container>
</template>

<script setup lang="ts">
import { useAdminApi } from "~/composables/api";
import type { AllBackups, BackupFile, BackupStatusOut } from "~/lib/api/types/admin";
import { alert } from "~/composables/use-toast";
import { useBackupActions, type BackupSource } from "~/composables/use-backup-actions";

definePageMeta({
  layout: "admin",
});

const i18n = useI18n();

const adminApi = useAdminApi();

const backups = ref<AllBackups>({
  imports: [],
  templates: [],
});

const status = ref<BackupStatusOut | null>(null);

const { selected, selectedSource, pickRestore, pickDelete, restore, remove } = useBackupActions(adminApi.backups);

async function refreshStatus() {
  try {
    const { data } = await adminApi.backups.getStatus();
    status.value = data ?? null;
  }
  catch {
    status.value = null;
  }
}

const view = ref<"local" | "s3">("local");
const bucketBackups = ref<BackupFile[]>([]);
const bucketListFailed = ref(false);

const showToggle = computed(() => !!status.value?.s3.enabled && status.value.s3.state !== "misconfigured");
const viewingBucket = computed(() => showToggle.value && view.value === "s3");
const currentSource = computed<BackupSource>(() => (viewingBucket.value ? "s3" : "local"));
const rows = computed(() => (viewingBucket.value ? bucketBackups.value : backups.value.imports || []));

async function refreshBucketBackups() {
  try {
    const { data, error } = await adminApi.backups.getS3();
    bucketListFailed.value = !!error || !data;
    bucketBackups.value = data?.backups ?? [];
  }
  catch {
    bucketListFailed.value = true;
    bucketBackups.value = [];
  }
}

async function refreshBackups() {
  const [{ data }] = await Promise.all([adminApi.backups.getAll(), refreshStatus()]);
  if (data) {
    backups.value = data;
  }
  if (viewingBucket.value) {
    await refreshBucketBackups();
  }
}

watch(view, (value) => {
  state.deleteDialog = false;
  state.importDialog = false;
  if (value === "s3" && showToggle.value) {
    refreshBucketBackups();
  }
});

watch(showToggle, (value) => {
  if (!value) {
    view.value = "local";
  }
});

function errorDetail(error: unknown, fallback: string): string {
  const message = (error as { response?: { data?: { message?: unknown } } } | null)?.response?.data?.message;
  return typeof message === "string" && message ? message : fallback;
}

async function createBackup() {
  state.runningBackup = true;
  const { data } = await adminApi.backups.create();

  if (data?.error === false) {
    refreshBackups();
    alert.success(i18n.t("settings.backup.backup-created"));
  }
  else {
    alert.error(i18n.t("settings.backup.error-creating-backup-see-log-file"));
  }
  state.runningBackup = false;
}

async function restoreBackup(fileName: string) {
  state.runningRestore = true;
  const { error, source } = await restore(fileName);

  if (error) {
    console.log(error);
    state.importDialog = false;
    state.runningRestore = false;
    alert.error(source === "s3" ? errorDetail(error, i18n.t("settings.backup.restore-fail")) : i18n.t("settings.backup.restore-fail"));
  }
  else {
    alert.success(i18n.t("settings.backup.restore-success"));
    setTimeout(() => {
      window.location.reload();
    }, 500);
  }
}

async function deleteBackup() {
  const { data, error, source } = await remove();

  if (source === "s3") {
    if (error || data?.error) {
      alert.error(errorDetail(error, i18n.t("settings.backup.s3-error")));
      return;
    }
    alert.success(i18n.t("settings.backup.backup-deleted"));
    await refreshBackups();
    return;
  }

  if (!data?.error) {
    alert.success(i18n.t("settings.backup.backup-deleted"));
    refreshBackups();
  }
}

const state = reactive({
  confirmImport: false,
  deleteDialog: false,
  createDialog: false,
  importDialog: false,
  runningBackup: false,
  runningRestore: false,
  search: "",
  headers: [
    { title: i18n.t("general.name"), value: "name" },
    { title: i18n.t("general.created"), value: "date" },
    { title: i18n.t("export.size"), value: "size" },
    { title: "", value: "actions", align: "right" },
  ],
});

function setSelected(data: { name: string; date: string }) {
  if (!data.name) {
    return;
  }
  pickRestore(data.name, currentSource.value);
}

const backupsFileNameDownload = (fileName: string) => `api/admin/backups/${fileName}`;

useSeoMeta({
  title: i18n.t("sidebar.backups"),
});

onMounted(refreshBackups);

useHead({
  title: i18n.t("sidebar.backups"),
});
</script>

<style>
.v-input--selection-controls__input {
  margin-bottom: auto;
}
</style>
