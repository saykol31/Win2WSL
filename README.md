# Win2WSL Clipboard Bridge

Bidirectional **text** clipboard sync between Windows and WSL2,
for when the built-in WSLg clipboard stops updating (a known
WSLg quirk: the first copy syncs, later copies never replace
it, so pasting in WSL keeps giving you the previous text).

The bridge is two small scripts that talk over a local TCP
socket, plus one command — `win2wsl` — that runs both sides
in the background and controls them. No third-party software,
no services, nothing leaves your machine.

- Windows side: pure PowerShell (built into Windows) — no
  Python or extra installs needed on Windows.
- WSL side: Python 3 + `wl-clipboard`.
- Changes are verified before they are applied: a clipboard
  that holds an image, a file or nothing is never mistaken
  for empty text, so the other side is never erased by
  accident. Every message carries a shared token, so other
  devices on your LAN cannot read or inject clipboard text.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `win2wsl` | WSL | The command: starts, stops and inspects both sides |
| `clipboard_bridge.py` | WSL | The WSL half of the bridge |
| `clipboard_bridge_windows.ps1` | Windows | The Windows half (listens on TCP 45831) |

## Requirements

- Windows 10/11 with WSL2 (Debian/Ubuntu) and PowerShell 5.1
  (built into Windows).
- In WSL: Python 3 and the `wl-clipboard` package:

```bash
sudo apt install wl-clipboard
```

## Install

1. Put the three files in one folder inside WSL, for example
   `~/Home/Win2WSL`, and go there:

   ```bash
   mkdir -p ~/Home/Win2WSL
   cd ~/Home/Win2WSL
   ```

2. Make the command executable and put it on your PATH with
   a symbolic link (these folders and the link are created
   by hand, once):

   ```bash
   chmod +x win2wsl
   mkdir -p ~/.local/bin
   ln -s ~/Home/Win2WSL/win2wsl ~/.local/bin/win2wsl
   ```

   Open a new terminal so `~/.local/bin` is on your PATH
   (or run `~/Home/Win2WSL/win2wsl` directly the first time).

3. Start the bridge:

   ```bash
   win2wsl start
   ```

   The first start copies the Windows half into
   `%USERPROFILE%\Win2WSL` on the Windows side and launches
   it hidden. If Windows Firewall asks, allow PowerShell on
   the **Private** network only. A shared token is created
   automatically at `%USERPROFILE%\.clipbridge_token`; both
   sides find it on their own.

## Usage

```bash
win2wsl start     # start both sides in the background
win2wsl stop      # stop both sides
win2wsl restart   # stop, then start again
win2wsl status    # is each side alive?
win2wsl logs      # last lines of both logs
win2wsl           # (no arguments) same as status
```

Stopping is done through the bridge itself: `win2wsl stop`
sends an authenticated shutdown message over the same
channel, so the hidden Windows server closes itself — no
console to hunt down and no process to kill by hand.

Useful places:

- WSL client log: `~/.win2wsl/client.log`
- Windows server log: `%USERPROFILE%\Win2WSL\bridge.log`
- Windows error log: `%USERPROFILE%\Win2WSL\bridge.err.log`

## Manual mode (for debugging)

Both halves can still be run in the foreground, with their
output on screen — handy while setting things up:

1. Windows, in PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\clipboard_bridge_windows.ps1
   ```

2. WSL, in a terminal:

   ```bash
   python3 clipboard_bridge.py wsl
   ```

Leave both running. Ctrl+C closes whichever one you are
looking at. Do not mix modes: close the foreground consoles
before `win2wsl start`, because the port cannot be shared.

## Notes

- Text only, in both directions.
- Windows may re-send a copied text once with its CRLF line
  endings; both sides recognize it as the same content and
  settle. It is not a loop.
- The bridge listens on your LAN interface so WSL can reach
  it; the token is what keeps the clipboard private. Treat
  `%USERPROFILE%\.clipbridge_token` like a password.

## License

GPL-3.0 — see `LICENSE`. Free to use, study and share; if
you distribute a modified version, it stays free too.
