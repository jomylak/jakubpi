"""Wake-on-LAN proxy: wakes the PC on demand, waits for Ollama to come up,
then forwards the request through and streams the response back.

Run on the Pi. Reachable over Tailscale from anywhere -- point requests at
this instead of the PC directly.
"""
import os
import time
import socket
import logging
import threading
from pathlib import Path

from flask import Flask, request, Response, jsonify
import requests
from dotenv import load_dotenv

from wol import send_magic_packet

load_dotenv(Path(__file__).resolve().parent / ".env")

PC_MAC = os.environ["PC_MAC"]
PC_IP = os.environ["PC_IP"]
BROADCAST_IP = os.environ.get("BROADCAST_IP", "255.255.255.255")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", 11434))
WAKE_TIMEOUT = int(os.environ.get("WAKE_TIMEOUT_SECONDS", 90))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", 2))

# Auto-sleep: 0 or unset disables it entirely (you sleep the PC yourself).
SLEEP_AFTER_IDLE_MINUTES = float(os.environ.get("SLEEP_AFTER_IDLE_MINUTES", 0))
PC_AGENT_PORT = int(os.environ.get("PC_AGENT_PORT", 8100))
PC_AGENT_TOKEN = os.environ.get("PC_AGENT_TOKEN", "")

log = logging.getLogger("wakeproxy")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

OLLAMA_BASE = f"http://{PC_IP}:{OLLAMA_PORT}"

# --- activity tracking, so we never sleep the PC mid-batch -----------------
_lock = threading.Lock()
_active_requests = 0
_last_activity = time.time()
_sleep_already_sent = False


def _request_started():
    global _active_requests, _last_activity, _sleep_already_sent
    with _lock:
        _active_requests += 1
        _last_activity = time.time()
        _sleep_already_sent = False  # new activity cancels any pending sleep


def _request_finished():
    global _active_requests, _last_activity
    with _lock:
        _active_requests = max(0, _active_requests - 1)
        _last_activity = time.time()


def _activity_snapshot():
    with _lock:
        return _active_requests, _last_activity


def _sleep_pc():
    """Ask the small agent running on the PC to sleep it. Does nothing
    useful if PC_AGENT_TOKEN isn't set on both sides."""
    try:
        requests.post(
            f"http://{PC_IP}:{PC_AGENT_PORT}/sleep",
            headers={"X-Auth-Token": PC_AGENT_TOKEN},
            timeout=5,
        )
        log.info("sent sleep command to PC")
    except requests.RequestException as e:
        log.warning("couldn't reach sleep agent on PC: %s", e)


def _idle_watcher():
    """Background loop: sleeps the PC only when zero requests are in
    flight AND it's been idle past the threshold. Never fires mid-batch --
    active_requests>0 blocks it unconditionally no matter how long the
    batch has been running."""
    global _sleep_already_sent
    if SLEEP_AFTER_IDLE_MINUTES <= 0:
        log.info("auto-sleep disabled (SLEEP_AFTER_IDLE_MINUTES=0)")
        return
    while True:
        time.sleep(30)
        active, last = _activity_snapshot()
        idle_for = time.time() - last
        if (active == 0
                and idle_for > SLEEP_AFTER_IDLE_MINUTES * 60
                and not _sleep_already_sent
                and is_up()):
            log.info("idle %.0fs with 0 in-flight requests -- sleeping PC",
                     idle_for)
            _sleep_pc()
            with _lock:
                _sleep_already_sent = True


threading.Thread(target=_idle_watcher, daemon=True).start()


def is_up(timeout=1.5) -> bool:
    """True if Ollama's port is accepting connections."""
    try:
        with socket.create_connection((PC_IP, OLLAMA_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def wake_and_wait() -> bool:
    """Sends WoL, polls until Ollama answers or we time out.
    Returns False if the PC never came up in time."""
    log.info("PC not reachable -- sending wake packet")
    send_magic_packet(PC_MAC, BROADCAST_IP)

    deadline = time.time() + WAKE_TIMEOUT
    while time.time() < deadline:
        if is_up():
            log.info("PC is up")
            return True
        time.sleep(POLL_INTERVAL)
    log.warning("PC did not come up within %ss", WAKE_TIMEOUT)
    return False


@app.route("/status")
def status():
    active, last = _activity_snapshot()
    return jsonify({
        "pc_reachable": is_up(),
        "active_requests": active,
        "idle_seconds": round(time.time() - last, 1),
        "auto_sleep_enabled": SLEEP_AFTER_IDLE_MINUTES > 0,
    })


@app.route("/wake", methods=["POST"])
def wake_only():
    """Fire-and-forget wake, no proxying. Useful for pre-warming."""
    if is_up():
        return jsonify({"status": "already_up"})
    ok = wake_and_wait()
    return jsonify({"status": "up" if ok else "timeout"}), (200 if ok else 504)


@app.route("/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy(subpath):
    """Everything else gets forwarded to Ollama, waking the PC first if
    it's asleep. Streaming responses (which Ollama uses by default) are
    passed through chunk-by-chunk rather than buffered."""
    if not is_up():
        if not wake_and_wait():
            return jsonify({"error": "PC did not wake up in time"}), 504

    # Counted as "active" from here until the streamed response is fully
    # drained below -- this is what stops the idle watcher from sleeping
    # the PC mid-request, and mid-batch if you're firing many in a row.
    _request_started()

    url = f"{OLLAMA_BASE}/{subpath}"
    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            headers={k: v for k, v in request.headers if k.lower() != "host"},
            data=request.get_data(),
            params=request.args,
            stream=True,
            timeout=(10, 300),  # (connect, read) -- generation can be slow
        )
    except requests.RequestException as e:
        log.exception("upstream request failed")
        _request_finished()
        return jsonify({"error": f"proxy failed: {e}"}), 502

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            # Only counts as "finished" once fully streamed, not when the
            # HTTP call returns -- a long generation still holds the slot.
            _request_finished()

    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    headers = [(k, v) for k, v in upstream.raw.headers.items()
              if k.lower() not in excluded]
    return Response(generate(), status=upstream.status_code, headers=headers)


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PROXY_PORT", 8000))
    log.info("wakeproxy listening on :%s -> %s", port, OLLAMA_BASE)
    serve(app, host="0.0.0.0", port=port)
