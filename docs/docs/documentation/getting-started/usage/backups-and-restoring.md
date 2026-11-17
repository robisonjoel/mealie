# Backups and Restores

Mealie provides an integrated mechanic for doing full installation backups of the database.

Navigate to Settings > Admin Settings > Backups or manually by adding `/admin/backups` to your instance URL.

From this page, you will be able to:

- See a list of available backups
- Create a backup
- Upload a backup
- Delete a backup (Confirmation Required)
- Download a backup
- Perform a restore

!!! tip
    If you're using Mealie with SQLite all your data is stored in the /app/data/ folder in the container. You can easily perform entire site backups by stopping the container, and backing up this folder with your chosen tool. This is the **best** way to backup your data.

## Scheduled Backups

Mealie can create a backup every day on its own. Set these variables in your [backend configuration](../installation/backend-config.md):

| Variable                  | Default | Description                                    |
| ------------------------- | :-----: | ---------------------------------------------- |
| `BACKUP_SCHEDULE_ENABLED` | `False` | Create a backup every day                      |
| `BACKUP_RETENTION_COUNT`  |   `7`   | Number of scheduled backups to keep. Minimum 1 |

The backup runs once a day at `DAILY_SCHEDULE_TIME`. Scheduled backups are named `mealie_scheduled_{version}_{timestamp}.zip`. The timestamp is in UTC.

Mealie manages every file with such a name in the backup folder. After each run, Mealie keeps the newest scheduled backups, up to the retention count. Mealie removes the older ones. Mealie finds the age of a scheduled backup from the timestamp in its name.

!!! warning
    Do not give other files a name of the form `mealie_scheduled_*.zip`. Mealie can remove a file with such a name from the backup folder. This includes a scheduled backup that you upload again.

!!! note
    Mealie never removes a backup with another name. The backups that you create with the button have another name.

The Backups page shows the schedule, the retention count, and the result of the last run above the backup list.

## Backing up to S3

Mealie can move each scheduled backup to an S3-compatible bucket. The scheduled backups in the bucket survive the loss of the server. Set `BACKUP_S3_ENABLED` to `True` and set the three required variables:

- `BACKUP_S3_BUCKET`
- `BACKUP_S3_ACCESS_KEY_ID`
- `BACKUP_S3_SECRET_ACCESS_KEY`

The optional variables are `BACKUP_S3_ENDPOINT_URL`, `BACKUP_S3_REGION`, `BACKUP_S3_PREFIX`, and `BACKUP_S3_VIRTUAL_HOSTED_STYLE`. See the [backend configuration](../installation/backend-config.md#backups) for all variables and their defaults.

!!! warning
    Use a different `BACKUP_S3_PREFIX`, or a different bucket, for each Mealie install. An install removes the old scheduled backups under its prefix. It cannot tell which install made them.

!!! note
    `BACKUP_S3_ENDPOINT_URL` must be a full `http://` or `https://` URL. Leave it unset for AWS.

!!! note
    You can give a value through a `_FILE` variable, for example `BACKUP_S3_SECRET_ACCESS_KEY_FILE`. The user that Mealie runs as (`PUID`) must be able to read the file. See [Docker Secrets](../installation/backend-config.md#docker-secrets).

### What Mealie moves and removes

- Mealie makes each scheduled backup on the server first. Then Mealie uploads the zip to the bucket.
- Mealie asks the bucket for the uploaded backup and compares its size with the file on the server. If the sizes are the same, Mealie removes the backup from the server.
- A backup that Mealie cannot upload stays on the server. Mealie moves it on the next run.
- Mealie moves only scheduled backups. Set `BACKUP_SCHEDULE_ENABLED` to `True`, or Mealie moves nothing.
- Backups with other names stay on the server only, for example the backups that you create with the button. Mealie never moves or removes them.
- If you set `BACKUP_S3_ENABLED` on an install that already has scheduled backups on the server, Mealie moves them to the bucket on the next run.
- Mealie manages every object named `mealie_scheduled_*.zip` directly under `BACKUP_S3_PREFIX` in the bucket. Mealie keeps the newest ones, up to `BACKUP_RETENTION_COUNT`. Mealie removes the older ones.
- `BACKUP_RETENTION_COUNT` also applies to the scheduled backups that wait on the server for an upload.
- Mealie never removes objects with other names. Do not give other objects a name of the form `mealie_scheduled_*.zip`.
- If you change the bucket or the prefix, the objects at the old location stay there. Mealie does not remove them.

!!! warning
    When S3 is on, the bucket holds the scheduled backups. You can restore a scheduled backup only when Mealie can reach the bucket.

### Checking the result

The status strip on Settings > Admin Settings > Backups shows the last run.

- A failed upload shows "Upload failed". Mealie keeps the backup on the server and moves it on the next run.
- "Backup created, cleanup failed" means the backup was made, and uploaded if S3 is on, but Mealie could not remove it from the server or could not remove old backups. The line below it names the step.
- The S3 cell shows one of these states:

| State                   | Meaning                                                                                      |
| ----------------------- | -------------------------------------------------------------------------------------------- |
| Connected               | Mealie can reach the bucket                                                                  |
| Misconfigured           | A required variable is missing. The cell names it                                            |
| Access denied           | The bucket rejected the credentials, or the credentials do not have permission on the bucket |
| Cannot reach the bucket | The endpoint or the network is wrong                                                         |
| Error                   | Something else failed. Check the server logs                                                 |

### Viewing the bucket

When `BACKUP_S3_ENABLED` is `True` and the three required variables are set, a switch on the Backups page changes the list between "On This Server" and "S3 Bucket". The bucket view shows the backups in the bucket. Each one has Delete and Restore. The scheduled backups are in the bucket view. The server view shows a scheduled backup only while it waits for an upload.

### Restoring from the bucket

1. Open Settings > Admin Settings > Backups.
2. Switch to "S3 Bucket".
3. Restore the backup you want.

Mealie downloads the backup to a temporary file on the server. Then Mealie restores from that file and deletes it. The restore does not leave a copy of the backup on the server. The same warnings as in [Restoring from a Backup](#restoring-from-a-backup) apply.

!!! tip
    If you cannot log in after the restore and no "Invalid credentials" error is shown, restart the Mealie container. Then try again.

## Restoring on a new install from S3

If you lose the server, the scheduled backups are still in the bucket. A new install that uses the same bucket and prefix restores from the bucket. It then continues the retention of the scheduled backups there. No other step is necessary.

1. Start the new install with the same S3 variables as the old install.
2. Open Settings > Admin Settings > Backups.
3. Switch to "S3 Bucket".
4. Restore the backup you want.
5. If you cannot log in after the restore, restart the Mealie container. Then try again.

Mealie restores the backup as in [Restoring from the bucket](#restoring-from-the-bucket). The same warnings as in [Restoring from a Backup](#restoring-from-a-backup) apply.

!!! warning
    Stop the old install before you start the new one, or give the new install a different prefix. Two installs must not use the same bucket and prefix at the same time.

## Restoring from a Backup

To restore from a backup it needs to be uploaded to your instance which can be done through the web portal. On the top left of the page you'll see an upload button. Click this button and select the backup file you want to upload and it will be available to import shortly. You can alternatively use one of the backups you see on the screen, if one exists.

Before importing it's critical that you understand the following:

- This is a destructive action and will delete all data in the database
- This action cannot be undone
- If this action is successful you will be logged out and you will need to log back in to complete the restore.

!!! tip
    If you are unable to log in after restoring and no "Invalid credentials" error is shown, restart the Mealie container and try again.

!!! tip
    If for some reason the restore does not succeed, you can review the logs of what the issue may be, download the backup .ZIP and edit the contents of database.json to potentially resolve the issue. For example, if you receive an error restoring 'shopping-list' you can edit out the contents of that list while allowing other sections to restore. If you would like any assistance on this, reach out over Discord.

!!! warning
    Prior to beta-v5 using a mis-matched version of the database backup will result in an error that will prevent you from using the instance of Mealie requiring you to remove all data and reinstall. Post beta-v5 performing a mismatched restore will throw an error and alert the user of the issue.

### Postgres Note

Restoring the Database when using Postgres requires Mealie to be configured with a postgres **superuser** account. This is due to our usage of massive deleting of data in the database and temporarily setting roles to perform the restore. To perform a restoration on Postgres you will need to _temporarily_ set the Mealie user to a superuser account.

```sql
ALTER USER mealie WITH SUPERUSER;

-- Run restore from Mealie

ALTER USER mealie WITH NOSUPERUSER;
```

For more information see [GitHub Issue #1500](https://github.com/mealie-recipes/mealie/issues/1500)
