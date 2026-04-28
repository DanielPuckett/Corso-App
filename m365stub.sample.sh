#!/bin/bash
export AZURE_TENANT_ID=change
export AZURE_CLIENT_ID=change
export CORSO_PASSPHRASE=change
export AZURE_CLIENT_SECRET=change
docker run --rm --hostname corso-backup-server --user 1000:1000 --env-file /mnt/corso/corso.env   --volume /mnt/corso:/app/corso --volume /mnt/corso_tui/exports:/mnt/corso_tui/exports ghcr.io/alcionai/corso:v0.19.0 "$@" --log-level=warn
exit
