# Running the bot on Linux (Wine + systemd) — the Aureon server

The `MetaTrader5` Python package is Windows-only: it talks to a running `terminal64.exe`. On Linux
both the MT5 terminal **and** the bot therefore run under Wine, with Windows Python inside the
same Wine prefix. systemd takes the place of the two Windows logon tasks
([SERVICE.md](SERVICE.md) v2.15.0):

```
aureon-xvfb.service   Xvfb :99 (virtual display; the terminal needs one)
aureon-mt5.service    wine terminal64.exe              (= Windows task "Aureon-MT5")
aureon-bot.service    +30 s: wine python.exe main.py   (= Windows task "Aureon", restart on every exit)
aureon-bot-watch.timer  every 60 s: restart the bot if logs/heartbeat.txt is > 300 s old
                        (after a 300 s start grace) — the Windows launcher's hang rule
```

Layout on the Aureon server (user `svskhd8`):

| What | Where |
|---|---|
| Wine prefix | `/home/svskhd8/.mt5` (64-bit, `wine` 9.0 + `wine32` from Ubuntu). Not `~/.wine`: every `wine` command below sets `WINEPREFIX`, because a bare `wine` uses `~/.wine` |
| Windows Python | `C:\Python311` = `~/.mt5/drive_c/Python311` |
| MT5 terminal | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| Bot checkout | `/srv/projects/Aureon_agentic_bot` (settings in its `.env`) |
| Units | `deploy/linux/*.service`, `*.timer` → `/etc/systemd/system/` |

The bot pushes to the Aureon API on the same box: `AUREON_PUSH_URL=http://127.0.0.1:8000/api/trading/mt5/ingest`
(Wine programs use the host's network, and the API publishes only on `127.0.0.1:8000`; no Tailscale, no TLS hop).

## 1. Packages (sudo, once)

```bash
sudo dpkg --add-architecture i386
sudo apt install -y wine wine64 wine32 xvfb x11vnc     # wine/xvfb are already on the Aureon server
```

`x11vnc` is only for the one-time broker login in step 4.

## 2. Windows Python 3.11 in the prefix

```bash
export WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all
cd /tmp && curl -fLO https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
md5sum python-3.11.9-amd64.exe          # must be e8dcd502e34932eebcaf1be056d5cbcd (python.org release page)
xvfb-run -a wine python-3.11.9-amd64.exe /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 TargetDir='C:\Python311'
WINEPREFIX=$HOME/.mt5 wine 'C:\Python311\python.exe' -c "import sys; print(sys.version)"      # 3.11.9 ...
```

## 3. The bot and its dependencies

```bash
cd /srv/projects/Aureon_agentic_bot && git checkout master && git pull --ff-only
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine 'C:\Python311\python.exe' -m pip install -r requirements.txt
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine 'C:\Python311\python.exe' -c "import MetaTrader5, pandas, discord; print('deps ok')"
```

`MetaTrader5 ; platform_system == "Windows"` in `requirements.txt` is true under Wine's Python, so it installs.

## 4. MT5 terminal + one-time broker login

Use your broker's MT5 installer if it has one (it pre-fills the server list); otherwise MetaQuotes':

```bash
cd /tmp && curl -fLO https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
sudo systemctl start aureon-xvfb          # after step 5 installs the units; or: Xvfb :99 &
DISPLAY=:99 WINEPREFIX=$HOME/.mt5 wine mt5setup.exe /auto
```

Log in once with **Save password** ticked, so the terminal logs in by itself after every restart.
The terminal draws on the virtual display; look at it over VNC, bound to localhost and reached
through an SSH tunnel:

```bash
x11vnc -display :99 -localhost -once -nopw          # on the server
ssh -L 5900:127.0.0.1:5900 svskhd8@<server>         # on your PC, then VNC to 127.0.0.1:5900
```

File → Login to Trade Account → login, password, server → Save password. Tools → Options → Expert
Advisors → **Allow algorithmic trading** (the bot's order calls need it; the push bridge does not).
Close the VNC viewer; `-once` ends x11vnc.

## 5. systemd units (sudo, once)

```bash
cd /srv/projects/Aureon_agentic_bot
sudo cp deploy/linux/aureon-xvfb.service deploy/linux/aureon-mt5.service \
        deploy/linux/aureon-bot.service deploy/linux/aureon-bot-watch.service \
        deploy/linux/aureon-bot-watch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aureon-xvfb aureon-mt5 aureon-bot aureon-bot-watch.timer
```

Do **not** set `AUREON_MT5_PATH` on Linux: under Wine, Python reports Windows, so the bot's preflight
would try to start the terminal through PowerShell, which Wine does not have. With it unset, the
preflight skips that step (a warning in `logs/aureon.log`) and `aureon-mt5.service` owns the terminal.

## 6. `.env` (repo root, read by `load_dotenv()`)

Your existing bot settings (Discord, symbols, mode, …) stay as they were. For the push bridge add:

```
AUREON_PUSH_ENABLED=1
AUREON_PUSH_URL=http://127.0.0.1:8000/api/trading/mt5/ingest
MT5_INGEST_TOKEN=<the same value as MT5_INGEST_TOKEN in /opt/private-ai/aureon/.env>
```

`chmod 600 .env`. Restart after any `.env` change:

```bash
sudo systemctl restart aureon-bot
```

## 7. Check it

```bash
systemctl status aureon-xvfb aureon-mt5 aureon-bot --no-pager
journalctl -u aureon-bot -n 50 --no-pager      # "aureon push: POST every 15 s to http://127.0.0.1:8000/..."
journalctl -u aureon-bot | grep "aureon push failed"   # nothing
cat logs/push_cursor.json                        # journal_offset grows
```

The Aureon dashboard (`/trading/mt5`) shows the bot as **online** within one poll.

## Differences from Windows

- **Terminal start**: systemd, not the bot's preflight (see step 5).
- **Hang restart**: `aureon-bot-watch.timer` + `scripts/linux/heartbeat_watch.sh` (same 300 s / 300 s rule).
- **Not on Linux**: `scripts/aureon_server.ps1` (Windows Update, auto-logon, power settings) and the
  `time.windows.com` drift resync. The host's `systemd-timesyncd` keeps the clock; Ubuntu updates
  are your own maintenance window.
- **Logs**: stdout/stderr go to the journal (`journalctl -u aureon-bot`) instead of `logs\service.log`;
  the bot's own files in `logs/` are unchanged.
