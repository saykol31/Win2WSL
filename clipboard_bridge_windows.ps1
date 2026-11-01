# Clipboard Bridge - lado Windows (PowerShell, sin instalar nada)
# Implementa el mismo protocolo que clipboard_bridge.py (puerto
# 45831, JSON por linea, token compartido). El lado WSL sigue
# siendo el script de Python:
#
#   WSL:     python3 clipboard_bridge.py wsl
#   Windows: powershell -ExecutionPolicy Bypass -File clipboard_bridge_windows.ps1
#
# Arranque: primero Windows. Si no existe el token, lo crea en
# %USERPROFILE%\.clipbridge_token; el lado WSL lo encuentra solo
# a traves de /mnt/c/Users/<usuario>/.clipbridge_token.
#
# Version 2 (fondo): la salida va con Write-Output para que
# quede en el log al correr oculto, y acepta el mensaje de
# apagado remoto {"type": "shutdown"} por el canal con token
# (lo envia el comando win2wsl al detenerse).
#
# Mismas reglas que la version Python:
#  - Sin token valido no se sirve ni se acepta texto.
#  - "Sin texto" (imagen, archivo, portapapeles ocupado) no se
#    transmite y nunca borra el portapapeles del otro lado.
#  - Deduplicacion por UUID con ventana anti-eco de 1 segundo.

$PORT = 45831
$MAX_MESSAGE = 1000000
$POLL_MS = 200

$script:LastText = $null
$script:LastId = $null
$script:SuppressUntil = [DateTime]::MinValue

# ---------------- Token compartido ----------------
function Get-BridgeToken {
    if ($env:CB_TOKEN) { return $env:CB_TOKEN.Trim() }
    $path = Join-Path $env:USERPROFILE ".clipbridge_token"
    if (Test-Path $path) {
        $t = (Get-Content $path -Raw).Trim()
        if ($t) { return $t }
    }
    $token = [guid]::NewGuid().ToString("N")
    Set-Content -Path $path -Value $token -Encoding Ascii
    Write-Output "Token creado en $path"
    Write-Output "Token: $token"
    Write-Output "El lado WSL lo lee solo desde tu perfil de Windows."
    return $token
}

# ---------------- Portapapeles ----------------
function Read-Clip {
    try {
        $t = Get-Clipboard -Raw -Format Text -ErrorAction Stop
        if ([string]::IsNullOrEmpty($t)) { return $null }
        return $t
    } catch {
        return $null
    }
}

function Apply-Remote($msg) {
    if ($msg.id -and $msg.id -eq $script:LastId) { return }
    $text = [string]$msg.text
    if (-not $text) { return }
    if ($text -eq $script:LastText) {
        $script:LastId = $msg.id
        return
    }
    try {
        Set-Clipboard -Value $text -ErrorAction Stop
    } catch {
        return
    }
    $script:LastText = $text
    $script:LastId = $msg.id
    $script:SuppressUntil = [DateTime]::UtcNow.AddSeconds(1)
    Write-Output "Portapapeles actualizado desde WSL."
}

# ---------------- Servidor ----------------
$TOKEN = Get-BridgeToken
$script:LastText = Read-Clip

$listener = New-Object System.Net.Sockets.TcpListener `
    ([System.Net.IPAddress]::Any, $PORT)
$listener.Start()
Write-Output "Windows escuchando en el puerto $PORT"
Write-Output "Si el firewall pregunta, permite PowerShell solo en red privada."

while ($true) {
    Write-Output "Esperando a WSL..."
    $client = $listener.AcceptTcpClient()
    $stream = $client.GetStream()
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $writer = New-Object System.IO.StreamWriter($stream, $encoding)
    $writer.AutoFlush = $true
    $byteBuf = New-Object byte[] 4096
    $buffer = ""
    $authed = $false
    Write-Output "Conexion entrante aceptada."

    while ($client.Connected) {
        # --- mensajes entrantes ---
        while ($stream.DataAvailable) {
            $n = $stream.Read($byteBuf, 0, $byteBuf.Length)
            if ($n -le 0) { break }
            $buffer += [System.Text.Encoding]::UTF8.GetString($byteBuf, 0, $n)
            if ($buffer.Length -gt $MAX_MESSAGE -and $buffer.IndexOf("`n") -lt 0) {
                $buffer = ""
            }
        }
        while ($buffer.IndexOf("`n") -ge 0) {
            $idx = $buffer.IndexOf("`n")
            $line = $buffer.Substring(0, $idx)
            $buffer = $buffer.Substring($idx + 1)
            if (-not $line) { continue }
            try { $msg = $line | ConvertFrom-Json } catch { continue }
            if ($msg.token -ne $TOKEN) { continue }
            if ($msg.type -eq "hello") {
                $authed = $true
                Write-Output "WSL autenticado."
            } elseif ($msg.type -eq "update" -and $authed) {
                Apply-Remote $msg
            } elseif ($msg.type -eq "shutdown" -and $authed) {
                Write-Output "Apagado remoto recibido."
                $client.Close()
                $listener.Stop()
                exit 0
            }
        }

        # --- deteccion de cierre por el otro lado ---
        if ($client.Client.Poll(0, [System.Net.Sockets.SelectMode]::SelectRead) `
            -and $client.Available -eq 0) {
            break
        }

        # --- vigilante del portapapeles ---
        # Diagnóstico de parpadeo: si existe nowatch.flag en
        # %USERPROFILE%\Win2WSL, el vigilante local no sondea
        # (la recepción sigue viva).
        # Silencio permanente (v2.2): el vigilante local solo
        # trabaja si existe watch.flag en %USERPROFILE%\Win2WSL.
        $flagPath = Join-Path $env:USERPROFILE "Win2WSL\watch.flag"
        $watchEnabled = Test-Path $flagPath
        $current = $null
        if ($watchEnabled) { $current = Read-Clip }
        if ($null -ne $current -and $current -ne $script:LastText) {
            if ([DateTime]::UtcNow -lt $script:SuppressUntil) {
                $script:LastText = $current  # eco: adoptar en silencio
            } else {
                $id = [guid]::NewGuid().ToString()
                $obj = @{
                    type   = "update"
                    id     = $id
                    origin = "windows"
                    token  = $TOKEN
                    text   = $current
                }
                $json = $obj | ConvertTo-Json -Compress
                if ($json.Length -le $MAX_MESSAGE) {
                    try {
                        $writer.WriteLine($json)
                        $script:LastText = $current
                        $script:LastId = $id
                        Write-Output "Cambio local enviado."
                    } catch {
                        break  # no se confirma el envio: se reintenta
                    }
                }
            }
        }

        Start-Sleep -Milliseconds $POLL_MS
    }

    Write-Output "WSL desconectado."
    $client.Close()
}
