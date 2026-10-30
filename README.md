# MetaForge ARC Raiders trade alerts

## Docker Compose (recommended)

Docker Compose is the preferred way to run the watcher: it runs continuously,
restarts after failures or host reboots, and keeps its SQLite history in a
persistent Docker volume. Clone the project and enter its directory:

```sh
git clone https://github.com/allmass-effect/metavalve
cd metavalve
```

Create a Telegram bot with BotFather, start a chat with it, and obtain the bot
token and destination chat ID. Create a private credentials file from the
example and replace the placeholders:

```sh
cp telegram.env.example telegram.env
chmod 600 telegram.env
```

Edit `telegram.env`:

```dotenv
TELEGRAM_BOT_TOKEN='your-bot-token'
TELEGRAM_CHAT_ID='your-chat-id'
# Optional HTTP or HTTPS proxy for Telegram API requests:
# TELEGRAM_PROXY='http://proxy-host:8080'
```

The file is mounted into the container as a read-only Compose secret. Do not
commit it or share it, its filename is ignored by Git and excluded from the
Docker build context. If your credentials file has a different path, set
`TELEGRAM_ENV_FILE` when starting Compose.

Configure the item, direction, and polling frequency in
`docker-compose.yml`, under `services.trade-alerts.environment`:

```yaml
environment:
  MARKET_ITEM_ID: propellant-valve-assembly
  MARKET_LISTING_TYPE: sell
  POLL_INTERVAL_SECONDS: "300"
  TRADE_ALERTS_DB: /data/trade-alerts.sqlite3
```

* `MARKET_ITEM_ID` is the item's URL slug - the value after `item_id=` in a
  MetaForge market URL. Change it to monitor another item, for example
  `herbal-bandage`.
* `MARKET_LISTING_TYPE` must be `buy` or `sell`. Use `buy` to watch listings
  where traders want to buy the selected item and offer something in return.
  Use `sell` to watch traders offering the selected item and wanting something
  in return.
* `POLL_INTERVAL_SECONDS` is the delay between checks, in seconds. For example,
  `"300"` checks every five minutes.

Compose runs the script with `--watch` by default, so no additional option is
needed for continuous polling. The first successful run records current
listings as a baseline without notifying, later checks send alerts for newly
seen listings. The `trade-alerts-data` named volume preserves that database
across container restarts and upgrades.

Build and start the watcher, then follow its logs:

```sh
docker compose up --build -d
docker compose logs -f trade-alerts
```

Stop the service with `docker compose down`. This keeps the database volume.
`docker compose down -v` also deletes it, clearing the saved baseline and
listing history.

### Windows with Docker Desktop

Install Docker Desktop, enable its WSL 2 backend, and start Docker Desktop.
Then run these commands from PowerShell (the repository's SSH URL requires
Git and an authorized SSH key):

```powershell
git clone https://github.com/allmass-effect/metavalve
Set-Location metavalve
Copy-Item telegram.env.example telegram.env
notepad telegram.env
docker compose up --build -d
docker compose logs -f trade-alerts
```

In Notepad, replace the Telegram token and chat ID placeholders and save the
file before starting Compose. To change the monitored item, direction, or poll
interval, edit `docker-compose.yml` as described above, then apply the changes
with `docker compose up -d`. Stop the watcher with `docker compose down`.

## Local run with `valve.sh`

Docker is preferred. For a local run, create
`~/.config/metavalve/credentials.env` with `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, and optionally `TELEGRAM_PROXY`, then launch:

```sh
./valve.sh --item-id herbal-bandage --listing-type buy --watch --interval 300
```

The local database and log are written to the current working directory.

### Windows local run

The Windows launcher uses PowerShell and requires Python 3 to be installed and
available as `python` in `PATH`. Create
`%USERPROFILE%\.config\metavalve\credentials.env` and add the same settings as
in the Linux credentials file. In PowerShell, create the directory and open the
file:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\metavalve"
notepad "$env:USERPROFILE\.config\metavalve\credentials.env"
```

Add these lines, replacing the placeholders:

```dotenv
TELEGRAM_BOT_TOKEN='your-bot-token'
TELEGRAM_CHAT_ID='your-chat-id'
# Optional:
# TELEGRAM_PROXY='http://proxy-host:8080'
```

From Command Prompt or PowerShell, run `run_windows.bat` from the project
directory. It starts continuous polling by default. Item ID, listing type, and
interval can be passed as PowerShell-style named options:

```powershell
.\run_windows.bat -ItemId herbal-bandage -ListingType buy -Interval 300
```

`-ListingType` accepts `buy` or `sell` defaults are
`propellant-valve-assembly`, `sell`, and 60 seconds. The local SQLite database
and log are saved in the current working directory. `valve.sh` remains
available for Linux, WSL, and Git Bash.

## Access note

The parser reads the listing cards and embedded market records from the page
HTML without Chromium. If MetaForge returns a Cloudflare challenge to a
non-browser request, the watcher reports the fetch failure rather than
attempting to bypass the challenge.

## Tests

```sh
python3 -m unittest -v
```
