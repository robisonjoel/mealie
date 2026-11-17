export type BackupSource = "local" | "s3";

interface BackupActionResult {
  error: unknown;
  data?: { error?: boolean } | null;
}

export interface BackupActionsApi {
  delete(name: string): Promise<BackupActionResult>;
  deleteS3(name: string): Promise<BackupActionResult>;
  restore(name: string): Promise<BackupActionResult>;
  restoreS3(name: string): Promise<BackupActionResult>;
}

/**
 * Holds the picked backup name together with the source it was picked from, so a later
 * view change cannot send a delete or restore to the other source.
 */
export function useBackupActions(api: BackupActionsApi) {
  const selected = ref("");
  const selectedSource = ref<BackupSource>("local");
  const deleteTarget = ref("");
  const deleteSource = ref<BackupSource>("local");

  function pickRestore(name: string, source: BackupSource) {
    selected.value = name;
    selectedSource.value = source;
  }

  function pickDelete(name: string, source: BackupSource) {
    deleteTarget.value = name;
    deleteSource.value = source;
  }

  async function restore(name: string = selected.value) {
    const source = selectedSource.value;
    const result = source === "s3" ? await api.restoreS3(name) : await api.restore(name);
    return { source, ...result };
  }

  async function remove() {
    const source = deleteSource.value;
    const name = deleteTarget.value;
    const result = source === "s3" ? await api.deleteS3(name) : await api.delete(name);
    return { source, ...result };
  }

  return { selected, selectedSource, deleteTarget, deleteSource, pickRestore, pickDelete, restore, remove };
}
