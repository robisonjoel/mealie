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
    ...overrides,
  };
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
});
