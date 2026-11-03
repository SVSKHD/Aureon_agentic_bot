# Running the bot on Linux — the Aureon server (mt5linux + systemd user services)

Everything runs on the Aureon server as the user `svskhd8`. The `MetaTrader5` package is
Windows-only, so it lives in **Windows Python under Wine**, next to the MT5 terminal, and is served
to the bot by the **mt5linux** server (an rpyc classic server). The bot itself runs on **native Linux
Python** with `AUREON_MT5_BACKEND=mt5linux` ([aureon_mt5/mt5api.py](../aureon_mt5/mt5api.py)).

```
systemd --user (lingering, so it starts at boot without a login)
  aureon-xvfb            Xvfb :1 (the terminal's display)
  aureon-x11vnc          VNC of :1 on 127.0.0.1:5900 only (SSH tunnel; broker login, looking at the terminal)
  aureon-wineserver      one wineserver for ~/.mt5, owned by its own unit
  aureon-mt5-terminal    wine terminal64.exe on :1 (started by aureon-mt5-terminal.path once installed)
  aureon-mt5linux        wine python.exe -m mt5linux --host 127.0.0.1 -p 18812
  aureon-bot             Linux python main.py → 127.0.0.1:18812; pushes to the Aureon API
  aureon-bot-watch.timer restart the bot when logs/heartbeat.txt is stale (300 s, after a 300 s grace)
```

| What | Where |
|---|---|
| Wine prefix | `~/.mt5` (64-bit; created by MetaQuotes' `mt5linux.sh`). Every `wine` command sets `WINEPREFIX=$HOME/.mt5`: a bare `wine` uses `~/.wine` |
| Windows Python | `C:\Python311` (3.11.9) with `MetaTrader5==5.0.6231`, `mt5linux==0.2.4`, `rpyc==5.2.3`, `numpy==1.26.4` |
| MT5 terminal | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| Bot checkout | `/srv/projects/Aureon_agentic_bot`, settings in its `.env` |
| Bot venv (Linux) | `~/.local/share/aureon-bot/venv` (outside the repo, so auto-update sees a clean tree) |
| Units | `deploy/linux/user/` → `~/.config/systemd/user/` |

## Pins that matter

- **numpy 1.26.4 in Wine.** numpy 2.x calls `ucrtbase.dll.crealf`, which Wine 9.0 (Ubuntu) does not
  implement; the import then hangs in Wine's crash debugger. `MetaTrader5` only needs `numpy>=1.7`.
- **mt5linux 0.2.4 and rpyc 5.2.3 on both sides** (the rpyc protocol must match). mt5linux 1.x needs
  Python 3.12 syntax and its client always starts a Docker container from a third-party image.
- **The bot does not use the mt5linux client** (see mt5api.py), only its server. Arguments go as
  JSON and results come back as local named tuples / numpy arrays.
- **The server binds 127.0.0.1 only.** A classic rpyc server runs any code a client sends;
  `--host ""` would bind every interface.
- **Wine's Python needs a non-file stdout.** With stdout redirected to a regular file it dies with
  `init_sys_streams: Invalid handle`; a pipe or the journal (systemd) is fine. Run it by hand with
  `</dev/null … | cat`.

## Setup (what was run on the server)

```bash
# Windows Python into ~/.mt5 (MD5 from python.org: e8dcd502e34932eebcaf1be056d5cbcd)
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all DISPLAY=:1 wine python-3.11.9-amd64.exe /quiet InstallAllUsers=0 \
  PrependPath=0 Include_test=0 Include_launcher=0 TargetDir='C:\Python311'
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine 'C:\Python311\python.exe' -m pip install \
  MetaTrader5==5.0.6231 mt5linux==0.2.4 rpyc==5.2.3 plumbum==1.7.0 numpy==1.26.4 </dev/null | cat

# The bot's Linux venv
python3 -m venv ~/.local/share/aureon-bot/venv
~/.local/share/aureon-bot/venv/bin/pip install -r /srv/projects/Aureon_agentic_bot/requirements.txt

# Units
cp /srv/projects/Aureon_agentic_bot/deploy/linux/user/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aureon-xvfb aureon-x11vnc aureon-wineserver aureon-mt5linux \
  aureon-mt5-terminal.path aureon-bot aureon-bot-watch.timer
sudo loginctl enable-linger svskhd8      # user services start at boot and survive logout
```

MT5 itself is installed with MetaQuotes' `~/mt5-setup/mt5linux.sh` (or
`WINEPREFIX=$HOME/.mt5 DISPLAY=:1 wine mt5setup.exe`); the `.path` unit starts the terminal as soon
as `terminal64.exe` exists.

## The one GUI step: broker login over VNC

```bash
ssh -L 5900:127.0.0.1:5900 svskhd8@<server>      # on your PC; keep it open
# then point a VNC viewer at 127.0.0.1:5900
```

In the terminal: File → Login to Trade Account → login, password, server → tick **Save password**.
Tools → Options → Expert Advisors → **Allow algorithmic trading** (the bot's order calls need it).
Close the viewer; the terminal stays logged in and logs in by itself after every restart.

## `.env` (repo root)

Your usual bot settings, plus:

```
AUREON_PUSH_ENABLED=1
AUREON_PUSH_URL=http://127.0.0.1:8000/api/trading/mt5/ingest
MT5_INGEST_TOKEN=<the same value as in /opt/private-ai/aureon/.env>
```

`AUREON_MT5_BACKEND=mt5linux` is set in the unit, not here. Leave `AUREON_MT5_PATH` unset: the
terminal belongs to systemd. Restart after a change: `systemctl --user restart aureon-bot`.

## Check it

```bash
systemctl --user status 'aureon-*' --no-pager
ss -ltnp | grep -E ':18812|:5900'                  # both on 127.0.0.1 only
journalctl --user -u aureon-bot -n 50 --no-pager   # preflight, "aureon push: POST every 15 s to http://127.0.0.1:8000/…"
```

The Aureon dashboard (`/trading/mt5`) shows the bot **online** within one poll.

## Differences from Windows

- The terminal is started by systemd, not by the bot's preflight (which only does that on Windows).
- Hangs: `aureon-bot-watch.timer` + `scripts/linux/heartbeat_watch.sh` (the launcher's rule).
- Not on Linux: `scripts/aureon_server.ps1` (Windows Update, auto-logon, power) and the
  `time.windows.com` drift resync; `systemd-timesyncd` keeps the clock.
- `tools/e_strategy_backtest.py` still imports `MetaTrader5` directly (Windows only).
- Logs: `journalctl --user -u aureon-bot`; the bot's own files in `logs/` are unchanged.
