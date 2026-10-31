# Clipboard Bridge for WSL

Bidirectional text clipboard synchronization between Windows and WSL (WSLg).

## The problem

The WSLg clipboard bridge is unreliable: the first copy from a Linux GUI app
reaches the Windows clipboard, but later copies often don't replace it — you
paste and get the *previous* text. The bug lives in the seam between the
Wayland compositor (Weston), the RDP clipboard channel, and the Windows
clipboard, and it has been reported for years.

This tool adds a small, verified channel of its own next to the broken one:
two watchers, one on each system, that forward every clipboard change to the
other side over a private TCP connection, with freshness checks so stale
content is never served.

## How it works

- The Windows side is a PowerShell server (no Python and no third-party
  software needed on Windows). It listens on port 45831.
- The WSL side is a Python 3 client. It connects to the Windows host
  (it tries the default gateway first, then 127.0.0.1).
- Both sides poll their local clipboard every 200–250 ms. When the text
  changes, it is sent to the other side as a JSON line carrying a UUID.
- Every change is identified by its UUID: the receiver remembers the last
  applied change and never re-applies or bounces it back. A short anti-echo
  window after applying a remote change prevents feedback loops.
- "No text" (an image, a file, a busy clipboard, a read error) is **not**
  the same as empty text. Non-text content is never transmitted and never
  clears the other side's clipboard.
- Both ends require a shared token. The Windows side creates it on first
  run in `%USERPROFILE%\.clipbridge_token`; the WSL side finds it
  automatically through `/mnt/c/Users/<user>/.clipbridge_token`. Without
  the token, the port neither serves clipboard data nor accepts text —
  the clipboard is not exposed to the LAN.
- One known cosmetic behavior: Windows normalizes line endings its own
  way, so a text arriving from WSL may be sent back **once** in its
  Windows flavor. Both sides then settle on identical content. It is a
  single normalization bounce, not a loop.

Text only, by design. Images and files are not synchronized.

## Files

| File | Side | Runs with |
|---|---|---|
| `clipboard_bridge_windows.ps1` | Windows | PowerShell 5.1 (built into Windows) |
| `clipboard_bridge.py` | WSL | Python 3 + `wl-clipboard` |

`clipboard_bridge.py` also has a `windows` mode for Windows machines that
do have Python (see "Alternative Windows side" below).

## Requirements

- Windows 10 or 11 with WSL2 and WSLg.
- PowerShell 5.1 (already on Windows — nothing to install).
- In WSL: Python 3 and the `wl-clipboard` package (step 1 below).

## Installation, step by step

### Step 0 — Put the files in place

- `clipboard_bridge_windows.ps1` → a folder on Windows (e.g. Downloads).
- `clipboard_bridge.py` → your WSL home or any WSL folder.

### Step 1 — One-time dependency in WSL

```bash
sudo apt install -y wl-clipboard
```

### Step 2 — Start the Windows side FIRST

Open PowerShell and run:

```powershell
powershell -ExecutionPolicy Bypass -File $HOME\Downloads\clipboard_bridge_windows.ps1
```

(Adjust the path if you placed the file elsewhere.)

On the first run it creates the token file and prints it. If Windows
Firewall asks about PowerShell, allow it on **Private networks only**.

Expected output:

```
Windows escuchando en el puerto 45831
Esperando a WSL...
```

### Step 3 — Start the WSL side

In your WSL terminal, from the folder where you put the script:

```bash
python3 clipboard_bridge.py wsl
```

Expected output:

```
Conectando a Windows: 172.x.x.x 45831
Conectado a Windows.
```

The Windows console answers with `WSL autenticado.`

### Step 4 — Use it

Just copy and paste normally on both systems. The bridge runs in the
foreground on purpose: both consoles print every synchronization
(`Portapapeles actualizado desde...` / `Cambio local enviado.`), so you
can watch it work while you trust it. A silent background mode can be
added later.

Quick smoke tests:

```bash
# Windows -> WSL: copy any text in Windows, then in WSL:
wl-paste

# WSL -> Windows:
echo "hello from WSL" | wl-copy
# then press Ctrl+V in any Windows window
```

### Stopping

Close either console (or Ctrl+C). Start again in the same order:
Windows first, then WSL.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `wl-paste` / `wl-copy`: command not found | Step 1 was skipped: `sudo apt install -y wl-clipboard` |
| WSL prints "No encontré el token" | The Windows side has never run. Start it once (Step 2) so it creates the token, or copy the token into `~/.clipbridge_token` in WSL. |
| WSL keeps retrying the connection | Windows side not running, or the firewall blocked PowerShell. Start Windows first; allow Private networks only. |
| "Address already in use" / port busy | A bridge instance is already running. Close the old console. |
| Images or copied files don't sync | By design: text only. Non-text never overwrites the other side. |
| A text bounces back once | Windows line-ending normalization (see "How it works"). It settles immediately. |

## Security notes

- The shared token travels in every message and is required both to
  receive the clipboard stream and to inject text. Keep
  `.clipbridge_token` private, like a password.
- The server listens on all interfaces so WSL can reach it through the
  gateway address. Keep the firewall permission on Private networks
  only, and treat the token as the real lock.

## Alternative Windows side (Python)

If the Windows machine already has Python 3:

```bat
pip install pyperclip
python clipboard_bridge.py windows
```

Same protocol, same token, same port — the WSL side doesn't know or
care which Windows implementation is running.

## Design note

The bridge follows a staged-transfer design, the way hardware moves a
wide word over a narrow bus: stage the data, hold it steady, verify it
is the fresh value, then deliver. Announcements are treated as hints,
never as proof — freshness is proven by the change UUID, not by the
notification channel, because the notification channel is exactly what
is broken in WSLg.
