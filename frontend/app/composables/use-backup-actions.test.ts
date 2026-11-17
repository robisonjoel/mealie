import { describe, expect, test, vi } from "vitest";
import { useBackupActions } from "./use-backup-actions";

function makeApi() {
  const ok = () => vi.fn().mockResolvedValue({ error: null, data: { error: false } });
  return { delete: ok(), deleteS3: ok(), restore: ok(), restoreS3: ok() };
}

describe("useBackupActions", () => {
  test("restores a picked bucket row from the bucket, whatever the view does afterward", async () => {
    const api = makeApi();
    const actions = useBackupActions(api);
    let view = "s3";

    actions.pickRestore("b.zip", view as "s3");
    view = "local";
    const result = await actions.restore();

    expect(view).toBe("local");
    expect(result.source).toBe("s3");
    expect(api.restoreS3).toHaveBeenCalledWith("b.zip");
    expect(api.restore).not.toHaveBeenCalled();
  });

  test("restores a picked local row locally after the view changes", async () => {
    const api = makeApi();
    const actions = useBackupActions(api);

    actions.pickRestore("a.zip", "local");
    const result = await actions.restore();

    expect(result.source).toBe("local");
    expect(api.restore).toHaveBeenCalledWith("a.zip");
    expect(api.restoreS3).not.toHaveBeenCalled();
  });

  test("deletes a picked bucket row from the bucket", async () => {
    const api = makeApi();
    const actions = useBackupActions(api);

    actions.pickDelete("b.zip", "s3");
    const result = await actions.remove();

    expect(result.source).toBe("s3");
    expect(api.deleteS3).toHaveBeenCalledWith("b.zip");
    expect(api.delete).not.toHaveBeenCalled();
  });

  test("deletes a picked local row locally", async () => {
    const api = makeApi();
    const actions = useBackupActions(api);

    actions.pickDelete("a.zip", "local");
    await actions.remove();

    expect(api.delete).toHaveBeenCalledWith("a.zip");
    expect(api.deleteS3).not.toHaveBeenCalled();
  });

  test("keeps delete and restore picks independent", async () => {
    const api = makeApi();
    const actions = useBackupActions(api);

    actions.pickRestore("a.zip", "local");
    actions.pickDelete("b.zip", "s3");
    await actions.restore();
    await actions.remove();

    expect(api.restore).toHaveBeenCalledWith("a.zip");
    expect(api.deleteS3).toHaveBeenCalledWith("b.zip");
  });

  test("passes the error through", async () => {
    const api = makeApi();
    api.restoreS3.mockResolvedValue({ error: new Error("boom"), data: null });
    const actions = useBackupActions(api);

    actions.pickRestore("b.zip", "s3");
    const result = await actions.restore();

    expect(result.error).toBeInstanceOf(Error);
  });
});
