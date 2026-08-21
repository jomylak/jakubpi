"""Tiny agent that runs ON THE PC (not the Pi). Its only job is to accept
one authenticated request and put Windows to sleep. Deliberately stdlib-only
-- no pip install needed on the PC, no dependency to keep patched.

Run it, then leave it running (see the scheduled-task note in the README
for making it start automatically at login).
"""
import os
import json
import subprocess
import http.server

TOKEN = os.environ.get("PC_AGENT_TOKEN", "")
PORT = int(os.environ.get("PC_AGENT_PORT", 8100))

if not TOKEN:
    raise SystemExit(
        "Set PC_AGENT_TOKEN before running -- this endpoint puts your PC to "
        "sleep on request; it must not be callable by anyone else on the LAN.")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/sleep":
            self.send_response(404)
            self.end_headers()
            return

        if self.headers.get("X-Auth-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "bad token"}')
            return

        # SetSuspendState(hibernate=0, forceCritical=1, disableWakeEvent=0)
        # forceCritical=1 skips the "apps might lose data" prompt some
        # drivers throw up, so this doesn't hang waiting for a click that
        # will never come on an unattended machine.
        subprocess.Popen(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "sleeping"}).encode())

    def log_message(self, fmt, *args):
        print(f"[pc_agent] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"pc_agent listening on :{PORT} (sleep endpoint is token-gated)")
    server.serve_forever()
