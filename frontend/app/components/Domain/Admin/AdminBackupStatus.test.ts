import { mount } from "@vue/test-utils";
import { describe, expect, test } from "vitest";
import AdminBackupStatus from "./AdminBackupStatus.vue";
import type { BackupStatusOut } from "~/lib/api/types/admin";

function makeStatus(overrides: Partial<BackupStatusOut> = {}): BackupStatusOut {
  return {
    scheduleEnabled: true,
    scheduleTime: "23:45",
    retentionCount: 7,
    lastRun: { outcome: "never_run", finishedAt: null, message: null, fileName: null },
    s3: { enabled: false, state: "disabled", bucket: null, prefix: null, detail: null },
    ...overrides,
  };
}

function makeS3(overrides: Partial<BackupStatusOut["s3"]> = {}): BackupStatusOut["s3"] {
  return { enabled: true, state: "connected", bucket: "my-bucket", prefix: "mealie", detail: null, ...overrides };
}

function render(status: BackupStatusOut) {
  return mount(AdminBackupStatus, { props: { status } });
}

describe("AdminBackupStatus", () => {
  test("shows the daily time and retention count when scheduling is on", () => {
    const text = render(makeStatus()).text();

    expect(text).toContain("Scheduled Backups");
    expect(text).toContain("Daily at 23:45");
    expect(text).toContain("Retention");
    expect(text).toContain("Keep last 7");
  });

  test("shows the off text when scheduling is off", () => {
    const text = render(makeStatus({ scheduleEnabled: false })).text();

    expect(text).toContain("Off. Set BACKUP_SCHEDULE_ENABLED to turn on");
    expect(text).not.toContain("Daily at");
  });

  test("shows Never when no run has happened", () => {
    const text = render(makeStatus()).text();

    expect(text).toContain("Last Run");
    expect(text).toContain("Never");
  });

  test("shows the finish time and Succeeded after a good run", () => {
    const wrapper = render(makeStatus({
      lastRun: { outcome: "success", finishedAt: "2026-11-13T23:45:10Z", message: null, fileName: "a.zip" },
    }));

    expect(wrapper.text()).toContain("Succeeded");
    expect(wrapper.text()).toContain("2026");
    expect(wrapper.text()).not.toContain("·");
    expect(wrapper.find("div.last-run-outcome").text()).toBe("Succeeded");
    expect(wrapper.find(".text-success").exists()).toBe(true);
    expect(wrapper.find(".text-error").exists()).toBe(false);
  });

  test("shows Backup failed and the message after a failed run", () => {
    const wrapper = render(makeStatus({
      lastRun: { outcome: "backup_failed", finishedAt: "2026-11-13T23:45:10Z", message: "disk full", fileName: null },
    }));

    expect(wrapper.text()).toContain("Backup failed");
    expect(wrapper.text()).toContain("disk full");
    expect(wrapper.find(".text-error").exists()).toBe(true);
    expect(wrapper.find("div.last-run-message").classes()).toContain("text-error");
  });

  test("puts an error accent on the container after a failed run", () => {
    const wrapper = render(makeStatus({
      lastRun: { outcome: "backup_failed", finishedAt: "2026-11-13T23:45:10Z", message: "disk full", fileName: null },
    }));

    expect(wrapper.find(".admin-backup-status").attributes("style")).toContain("--v-theme-error");
  });

  test("shows the cleanup warning and the message when the backup was made but cleanup failed", () => {
    const wrapper = render(makeStatus({
      lastRun: {
        outcome: "cleanup_failed",
        finishedAt: "2026-11-13T23:45:10Z",
        message: "retention: could not delete a.zip",
        fileName: "b.zip",
      },
    }));

    const outcome = wrapper.find("div.last-run-outcome");
    expect(outcome.text()).toBe("Backup created, cleanup failed");
    expect(outcome.classes()).toContain("text-warning");
    expect(outcome.classes()).not.toContain("text-success");
    expect(outcome.classes()).not.toContain("text-error");

    const message = wrapper.find("div.last-run-message");
    expect(message.text()).toBe("retention: could not delete a.zip");
    expect(message.classes()).toContain("text-warning");

    expect(wrapper.text()).not.toContain("Succeeded");
    expect(wrapper.text()).not.toContain("·");
    expect(wrapper.find(".text-error").exists()).toBe(false);
    expect(wrapper.find(".admin-backup-status").attributes("style")).toContain("--v-theme-warning");
  });

  test("shows the message of a good run when there is one", () => {
    const wrapper = render(makeStatus({
      lastRun: {
        outcome: "success",
        finishedAt: "2026-11-13T23:45:10Z",
        message: "status: OSError",
        fileName: "a.zip",
      },
    }));

    expect(wrapper.find("div.last-run-outcome").text()).toBe("Succeeded");
    expect(wrapper.find("div.last-run-message").text()).toBe("status: OSError");
    expect(wrapper.find(".text-error").exists()).toBe(false);
    expect(wrapper.find(".text-warning").exists()).toBe(false);
  });

  test("shows no message line when the run has no message", () => {
    const wrapper = render(makeStatus({
      lastRun: { outcome: "success", finishedAt: "2026-11-13T23:45:10Z", message: null, fileName: "a.zip" },
    }));

    expect(wrapper.find("div.last-run-message").exists()).toBe(false);
  });

  test("puts no accent on the container when scheduling is off", () => {
    const wrapper = render(makeStatus({ scheduleEnabled: false }));

    expect(wrapper.find(".admin-backup-status").attributes("style") ?? "").not.toContain("border-left");
  });

  test("hides the S3 cell when S3 is off", () => {
    const text = render(makeStatus()).text();

    expect(text).not.toContain("my-bucket");
    expect(text).not.toContain("Connected");
  });

  test("shows the bucket and Connected for a connected bucket", () => {
    const wrapper = render(makeStatus({ s3: makeS3() }));

    expect(wrapper.text()).toContain("my-bucket/mealie");
    expect(wrapper.text()).toContain("Connected");
    expect(wrapper.find(".s3-state").classes()).not.toContain("text-error");
  });

  test("shows the detail for a misconfigured bucket", () => {
    const detail = "Missing required values for ['BACKUP_S3_BUCKET']";
    const wrapper = render(makeStatus({ s3: makeS3({ state: "misconfigured", bucket: null, prefix: null, detail }) }));

    expect(wrapper.text()).toContain(`Misconfigured: ${detail}`);
    expect(wrapper.find(".s3-state").classes()).toContain("text-error");
  });

  test.each([
    ["access_denied", "Access denied"],
    ["unreachable", "Cannot reach the bucket"],
    ["not_found", "Error. Check the server logs"],
    ["error", "Error. Check the server logs"],
  ])("shows the text for the %s state", (state, expected) => {
    const wrapper = render(makeStatus({ s3: makeS3({ state }) }));

    expect(wrapper.text()).toContain(expected);
    expect(wrapper.find(".s3-state").classes()).toContain("text-error");
  });

  test("shows Upload failed in the error color with an error accent", () => {
    const wrapper = render(makeStatus({
      lastRun: { outcome: "upload_failed", finishedAt: "2026-11-13T23:45:10Z", message: "bucket down", fileName: "a.zip" },
    }));

    expect(wrapper.text()).toContain("Upload failed");
    expect(wrapper.text()).toContain("bucket down");
    expect(wrapper.find(".last-run-outcome").classes()).toContain("text-error");
    expect(wrapper.find(".admin-backup-status").attributes("style")).toContain("--v-theme-error");
  });

  test("shows the message of a successful run in the default color", () => {
    const wrapper = render(makeStatus({
      lastRun: { outcome: "success", finishedAt: "2026-11-13T23:45:10Z", message: "Bucket cleanup failed: unreachable", fileName: "a.zip" },
    }));

    const message = wrapper.find(".last-run-message");
    expect(message.text()).toBe("Bucket cleanup failed: unreachable");
    expect(message.classes()).not.toContain("text-error");
  });
});
