#!/bin/bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
working_dir="$PWD"
credentials_file="$HOME/.config/metavalve/credentials.env"
if [[ ! -r "$credentials_file" ]]; then
    printf 'Credentials file is missing or unreadable: %s\n' "$credentials_file" >&2
    exit 1
fi

set -a
. "$credentials_file"
set +a

export MARKET_BASE_URL='https://metaforge.app/arc-raiders/market'
export MARKET_ITEM_ID="${MARKET_ITEM_ID:-propellant-valve-assembly}"
export MARKET_LISTING_TYPE="${MARKET_LISTING_TYPE:-sell}"
export HTTP_TIMEOUT_SECONDS='20'
export POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-60}"
export TRADE_ALERTS_DB="$working_dir/trade-alerts.sqlite3"
export TRADE_ALERTS_LOG_FILE="$working_dir/valve.log"

exec python3 "$script_dir/trade_alerts.py" "$@"
