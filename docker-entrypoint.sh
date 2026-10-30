#!/bin/sh
set -eu

credentials_file=/run/secrets/telegram_credentials
if [ ! -r "$credentials_file" ]; then
    printf 'Telegram credentials file is missing or unreadable: %s\n' \
        "$credentials_file" >&2
    exit 1
fi

set -a
. "$credentials_file"
set +a

exec gosu app python3 /app/trade_alerts.py "$@"
