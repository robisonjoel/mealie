import { BaseAPI } from "../base/base-clients";
import type { AllBackups, BackupStatusOut, BucketBackupsOut } from "~/lib/api/types/admin";
import type { ErrorResponse, FileTokenResponse, SuccessResponse } from "~/lib/api/types/response";

const prefix = "/api";

const routes = {
  base: `${prefix}/admin/backups`,
  status: `${prefix}/admin/backups/status`,
  item: (name: string) => `${prefix}/admin/backups/${name}`,
  s3: `${prefix}/admin/backups/s3`,
  s3Item: (name: string) => `${prefix}/admin/backups/s3/${name}`,
  s3Restore: (name: string) => `${prefix}/admin/backups/s3/${name}/restore`,
  restore: (name: string) => `${prefix}/admin/backups/${name}/restore`,
};

export class AdminBackupsApi extends BaseAPI {
  async getAll() {
    return await this.requests.get<AllBackups>(routes.base);
  }

  async getStatus() {
    return await this.requests.get<BackupStatusOut>(routes.status);
  }

  async create() {
    return await this.requests.post<SuccessResponse | ErrorResponse>(routes.base, {});
  }

  async get(fileName: string) {
    return await this.requests.get<FileTokenResponse>(routes.item(fileName));
  }

  async delete(fileName: string) {
    return await this.requests.delete<SuccessResponse | ErrorResponse>(routes.item(fileName));
  }

  async restore(fileName: string) {
    return await this.requests.post<SuccessResponse | ErrorResponse>(routes.restore(fileName), {});
  }

  async getS3() {
    return await this.requests.get<BucketBackupsOut>(routes.s3);
  }

  async deleteS3(fileName: string) {
    return await this.requests.delete<SuccessResponse | ErrorResponse>(routes.s3Item(fileName));
  }

  async restoreS3(fileName: string) {
    return await this.requests.post<SuccessResponse | ErrorResponse>(routes.s3Restore(fileName), {});
  }
}
