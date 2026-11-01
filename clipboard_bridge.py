#!/usr/bin/env python3
"""
Clipboard Bridge: Windows <-> WSL
Sincronización bidireccional de texto entre el portapapeles de
Windows y el de WSL (WSLg), con verificación de frescura.

Windows (una sola vez: pip install pyperclip):
    python clipboard_bridge.py windows

WSL (una sola vez: sudo apt install wl-clipboard):
    python3 clipboard_bridge.py wsl

Arranque: primero el lado Windows. En la primera ejecución crea
el token compartido en ~/.clipbridge_token y lo muestra; el lado
WSL lo lee de ahí o, si no lo encuentra, lo busca en el perfil de
Windows a través de /mnt/c/Users/*/.clipbridge_token.

Correcciones respecto al borrador original:
 1. CONEXIÓN: el cliente WSL prueba primero la IP del host
    (puerta de enlace) y después 127.0.0.1 (el reenvío de
    localhost de WSL). El servidor escucha en 0.0.0.0 para
    poder aceptar la ruta de la puerta de enlace.
 2. AUTENTICACIÓN: cada mensaje lleva un token compartido.
    Sin token válido no se recibe el flujo ni se aceptan
    textos: el puerto no sirve el portapapeles a la LAN.
 3. TRI-ESTADO: "sin texto" (imagen, archivo, portapapeles
    ocupado o error de lectura) NO es lo mismo que texto
    vacío. Lo que no es texto no se transmite y nunca borra
    el portapapeles del otro lado.
 4. DEDUP POR ID: cada cambio lleva un UUID que SÍ se usa.
    El receptor recuerda el último id aplicado y el watcher
    respeta ese estado bajo el mismo candado; tras aplicar
    un cambio remoto hay una ventana anti-eco de 1 segundo.
 5. Un solo watcher por proceso (el del servidor difunde a
    todos los clientes autenticados); si un envío falla, el
    cambio no se da por sincronizado y se reintenta.

Versión 2 (fondo): el comando `win2wsl` gobierna ambos
lados. El servidor acepta además un mensaje de apagado
{"type": "shutdown", "token": ...} por el mismo canal
autenticado, para poder detenerse sin tocar su consola.
"""

import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

PORT = 45831
POLL_INTERVAL = 0.25
MAX_MESSAGE = 1_000_000
ECHO_WINDOW = 1.0  # segundos de silencio tras aplicar un cambio remoto

ROLE = None  # "windows" o "wsl", fijado en main

# ----------------------------------------------------------------
# Estado compartido (siempre bajo state_lock)
# ----------------------------------------------------------------
state_lock = threading.Lock()
last_text = None        # último texto conocido (local o aplicado)
last_id = None          # id del último cambio conocido
suppress_until = 0.0    # ventana anti-eco (time.monotonic)

send_lock = threading.Lock()  # sendall no es seguro entre hilos


# ----------------------------------------------------------------
# Token compartido
# ----------------------------------------------------------------
def token_file():
    return os.path.join(os.path.expanduser("~"), ".clipbridge_token")


def get_token(generate=False):
    """Orden: variable CB_TOKEN, archivo local, perfil de Windows."""
    token = os.environ.get("CB_TOKEN", "").strip()
    if token:
        return token

    try:
        with open(token_file(), encoding="utf-8") as fh:
            token = fh.read().strip()
            if token:
                return token
    except OSError:
        pass

    for path in sorted(glob.glob("/mnt/c/Users/*/.clipbridge_token")):
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
                if token:
                    return token
        except OSError:
            continue

    if generate:
        token = uuid.uuid4().hex
        try:
            with open(token_file(), "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
            print("Token creado en", token_file())
            print("Token:", token)
            print("El lado WSL lo lee solo desde tu perfil de Windows;")
            print("si no, cópialo a ~/.clipbridge_token en WSL.")
        except OSError as exc:
            print("No pude guardar el token:", exc)
        return token

    return None


# ----------------------------------------------------------------
# Portapapeles local: leer devuelve str, o None si NO hay texto
# (imagen, archivo, ocupado o error). None nunca se transmite.
# ----------------------------------------------------------------
def read_clipboard():
    try:
        if sys.platform == "win32":
            import pyperclip
            text = pyperclip.paste()
            return text if text else None

        result = subprocess.run(
            ["wl-paste", "--no-newline"],
            capture_output=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        text = result.stdout.decode("utf-8", errors="replace")
        return text if text else None

    except Exception:
        return None


def write_clipboard(text):
    try:
        if sys.platform == "win32":
            import pyperclip
            pyperclip.copy(text)
            return True

        result = subprocess.run(
            ["wl-copy"],
            input=text.encode("utf-8"),
            timeout=2,
        )
        return result.returncode == 0

    except Exception as exc:
        print("Error escribiendo portapapeles:", exc)
        return False


# ----------------------------------------------------------------
# Mensajes
# ----------------------------------------------------------------
def send_obj(sock, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        print("Mensaje demasiado grande, no enviado.")
        return False
    try:
        with send_lock:
            sock.sendall(data + b"\n")
        return True
    except OSError:
        return False


def apply_remote(msg):
    """Aplica un cambio remoto: dedup por id, sin retransmitirlo."""
    global last_text, last_id, suppress_until

    text = msg.get("text")
    msg_id = msg.get("id")
    if not isinstance(text, str) or not text:
        return

    with state_lock:
        if msg_id is not None and msg_id == last_id:
            return
        if text == last_text:
            last_id = msg_id
            return
        if write_clipboard(text):
            last_text = text
            last_id = msg_id
            suppress_until = time.monotonic() + ECHO_WINDOW
            print("Portapapeles actualizado desde el otro sistema.")


def clipboard_watcher(send_fn):
    """Vigila cambios locales; solo da el cambio por enviado si
    send_fn confirma. send_fn(obj) -> bool."""
    global last_text, last_id

    # Silencio permanente (v2.2): el vigilante queda apagado
    # por defecto. Sondear el contenido cuando el dueño de la
    # selección es el proxy de WSLg provoca el parpadeo de las
    # ventanas (diagnóstico 2026-10-31). Solo se enciende si
    # existe la bandera watch.flag; la recepción sigue viva.
    if not os.path.exists(os.path.expanduser("~/.win2wsl/watch.flag")):
        print("Vigilante local en silencio (sin watch.flag).")
        return

    while True:
        time.sleep(POLL_INTERVAL)
        current = read_clipboard()
        if current is None:
            continue

        with state_lock:
            if current == last_text:
                continue
            if time.monotonic() < suppress_until:
                # Ventana anti-eco: adoptar en silencio lo que se
                # lee (puede ser el eco de lo recién aplicado).
                last_text = current
                continue
            new_id = str(uuid.uuid4())
            msg = {
                "type": "update",
                "id": new_id,
                "origin": ROLE,
                "token": TOKEN,
                "text": current,
            }
            prev_text, prev_id = last_text, last_id
            last_text, last_id = current, new_id

        if not send_fn(msg):
            with state_lock:
                if last_id == new_id:  # nadie aplicó otra cosa
                    last_text, last_id = prev_text, prev_id


def receive_loop(sock, on_update, on_hello=None):
    """Lee mensajes JSON por línea. Devuelve al cerrar la conexión."""
    buffer = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Conexión cerrada")
        buffer += chunk
        if len(buffer) > MAX_MESSAGE and b"\n" not in buffer:
            buffer = b""  # línea imposible: descartar y seguir
            continue
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line or len(line) > MAX_MESSAGE:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                print("Mensaje inválido recibido.")
                continue
            if not isinstance(msg, dict) or msg.get("token") != TOKEN:
                continue  # sin token válido no existe
            kind = msg.get("type")
            if kind == "hello" and on_hello is not None:
                on_hello()
            elif kind == "update":
                on_update(msg)
            elif kind == "shutdown" and ROLE == "windows":
                # Apagado remoto por el canal autenticado:
                # lo usa el comando win2wsl para detener el
                # servidor que vive en segundo plano.
                print("Apagado remoto recibido.")
                os._exit(0)


# ----------------------------------------------------------------
# Lado Windows: servidor + un watcher que difunde a los
# clientes autenticados.
# ----------------------------------------------------------------
clients = {}            # sock -> {"authed": bool}
clients_lock = threading.Lock()


def broadcast(msg):
    with clients_lock:
        targets = [s for s, info in clients.items() if info["authed"]]
    ok = True
    for sock in targets:
        if not send_obj(sock, msg):
            ok = False
    return ok or not targets


def run_windows_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen(5)
    print(f"Windows escuchando en el puerto {PORT}")
    print("Si el firewall pregunta, permite Python solo en red privada.")

    threading.Thread(
        target=clipboard_watcher, args=(broadcast,), daemon=True
    ).start()

    while True:
        conn, addr = server.accept()
        print("Conexión entrante:", addr)
        with clients_lock:
            clients[conn] = {"authed": False}
        threading.Thread(
            target=handle_client, args=(conn,), daemon=True
        ).start()


def handle_client(conn):
    def mark_authed():
        with clients_lock:
            if conn in clients:
                clients[conn]["authed"] = True
        print("WSL autenticado.")

    def guarded_update(msg):
        with clients_lock:
            authed = clients.get(conn, {}).get("authed", False)
        if authed:
            apply_remote(msg)

    try:
        receive_loop(conn, guarded_update, mark_authed)
    except Exception as exc:
        print("Cliente desconectado:", exc)
    finally:
        with clients_lock:
            clients.pop(conn, None)
        conn.close()


# ----------------------------------------------------------------
# Lado WSL: cliente con reconexión; un watcher que envía por la
# conexión vigente.
# ----------------------------------------------------------------
current = {"sock": None}


def send_via_current(msg):
    sock = current["sock"]
    if sock is None:
        return False
    return send_obj(sock, msg)


def get_windows_host():
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=2,
        )
        parts = result.stdout.split()
        for i, part in enumerate(parts):
            if part == "via" and i + 1 < len(parts):
                return parts[i + 1]
    except Exception:
        pass
    return None


def run_wsl_client():
    threading.Thread(
        target=clipboard_watcher, args=(send_via_current,), daemon=True
    ).start()

    candidates = []
    gateway = get_windows_host()
    if gateway:
        candidates.append(gateway)
    candidates.append("127.0.0.1")

    while True:
        sock = None
        for host in candidates:
            try:
                print("Conectando a Windows:", host, PORT)
                sock = socket.create_connection((host, PORT), timeout=4)
                break
            except OSError:
                sock = None
        if sock is None:
            print("Windows no responde; reintento en 3 s.")
            time.sleep(3)
            continue

        try:
            sock.settimeout(None)
            current["sock"] = sock
            if not send_obj(sock, {"type": "hello", "token": TOKEN}):
                raise ConnectionError("No pude saludar")
            print("Conectado a Windows.")
            receive_loop(sock, apply_remote)
        except Exception as exc:
            print("Conexión perdida:", exc)
        finally:
            current["sock"] = None
            sock.close()
        time.sleep(3)


# ----------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1].lower() not in ("windows", "wsl"):
        print("Uso:")
        print("  Windows: python clipboard_bridge.py windows")
        print("  WSL:     python3 clipboard_bridge.py wsl")
        sys.exit(1)

    ROLE = sys.argv[1].lower()

    global TOKEN
    if ROLE == "windows":
        TOKEN = get_token(generate=True)
    else:
        TOKEN = get_token(generate=False)
        if not TOKEN:
            print("No encontré el token. Arranca primero el lado Windows")
            print("(crea ~/.clipbridge_token) o crea el archivo en WSL:")
            print("  ~/.clipbridge_token  con el mismo token.")
            sys.exit(1)

    last_text = read_clipboard()  # línea base: no sincronizar lo viejo

    if ROLE == "windows":
        run_windows_server()
    else:
        run_wsl_client()
