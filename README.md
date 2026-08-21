# wakeproxy

Wakes your PC on demand and forwards the request to Ollama once it's up.
Runs on the Pi, reachable over Tailscale from anywhere.

```
request  ->  Pi (wakeproxy)  ->  is PC's Ollama port open?
                                    no  -> send WoL, poll until it is (or time out)
                                    yes -> forward + stream response back
```

## 1. Find your PC's info

**MAC address** (Windows, PowerShell or cmd):
```
ipconfig /all
```
Look under the **wired** Ethernet adapter (not WiFi) for "Physical Address" --
that's `PC_MAC`, format `AA:BB:CC:DD:EE:FF`.

**IP address** -- same output, "IPv4 Address". Set a **DHCP reservation** for
this in your router's admin page so it never changes; if it drifts, the proxy
silently talks to nothing and every request times out with no obvious cause.

**Broadcast address** -- almost always your IP's subnet with `.255` at the
end (e.g. IP `192.168.1.50` -> broadcast `192.168.1.255`).

## 2. On the PC

- BIOS/UEFI: enable Wake-on-LAN
- Device Manager -> your Ethernet adapter -> Power Management -> check
  "Allow this device to wake the computer"
- Power settings -> disable Fast Startup (blocks WoL from a full shutdown --
  moot if you're using sleep, but disable it anyway)
- Set your normal sleep timer (Settings -> Power -> Sleep). Don't build
  anything custom for auto-sleep: Windows' idle timer is based on
  keyboard/mouse input, not network activity, so it naturally sleeps the
  PC after you stop using it even while Ollama served requests minutes
  earlier. WoL wakes it again next time regardless of sleep state.
- Install Ollama, pull a model: `ollama pull qwen2.5` (or whatever you land
  on) -- by default it listens on `11434` on all interfaces once running.
- Enable Remote Desktop (Settings -> System -> Remote Desktop) if you want
  full desktop access once it's awake, not just API calls -- reach it at
  the PC's Tailscale IP once it's up.

### Boot time, since sleep != shutdown

The PC sleeps (S3), it never shuts down -- Ollama's process stays resident
in RAM the whole time, it doesn't restart. Waking from S3 typically takes
**5-15 seconds** to be network-reachable on modern hardware, nothing like a
cold boot. First request after idle just sits there for that long before
streaming starts (`wake_and_wait()` polling in the background) -- expected,
not a bug.

### Auto-sleep during a batch

If you set `SLEEP_AFTER_IDLE_MINUTES`, the proxy tracks every request as
"in flight" from the moment it starts until its response is fully
streamed back -- including overlapping ones. It will not sleep the PC
while *anything* is in flight, no matter how long a batch has been
running; the idle clock only starts once the count drops to zero. Check
`/status` any time to see `active_requests` and `idle_seconds` live.

To actually enable auto-sleep, run the small agent on the PC that carries
out the sleep command (the proxy itself can't -- it's on the Pi):

```powershell
# On the PC, in PowerShell:
$env:PC_AGENT_TOKEN = "<same random string as PC_AGENT_TOKEN in .env>"
python pc_agent.py
```

Use the *same* token on both sides (`.env` on the Pi, `$env:PC_AGENT_TOKEN`
on the PC) -- this endpoint puts your machine to sleep on request, so
without a real shared secret anyone on your LAN could trigger it. To keep
it running automatically: Task Scheduler -> Create Task -> trigger "At log
on" -> action `python.exe pc_agent.py` with the token set in the task's
environment, or wrap it as a scheduled action that sets the env var first.

## 3. On the Pi

```bash
cd ~/wakeproxy
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env    # fill in PC_MAC, PC_IP, BROADCAST_IP
```

Run it directly to test:
```bash
./.venv/bin/python app.py
```

From your Mac/phone (once on the same tailnet):
```bash
curl -X POST http://<pi-tailscale-ip>:8000/wake        # just wakes it, no proxying
curl http://<pi-tailscale-ip>:8000/status               # {"pc_reachable": true/false}

# actual inference, wakes automatically if asleep:
curl -X POST http://<pi-tailscale-ip>:8000/api/generate \
  -d '{"model": "qwen2.5", "prompt": "hello"}'
```

First request after the PC's been asleep will hang for however long it
takes to boot (usually 15-30s) before streaming starts -- that's expected,
it's waiting on `wake_and_wait()`.

## 4. Run as a service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/wakeproxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wakeproxy
journalctl --user -u wakeproxy -f
```

## Notes

- **WoL is LAN-only.** The magic packet doesn't route over Tailscale --
  that's why this proxy has to physically live on the Pi, on your home
  network, not on a cloud box. The *request* to trigger a wake can come
  from anywhere over Tailscale; the packet itself only ever travels the
  last hop on your LAN.
- **Wired ethernet on the PC is required.** WiFi wake (WoWLAN) is flaky
  enough not to build around.
- If `/wake` reliably times out, check in order: DHCP reservation still
  matches `PC_IP`, WoL still enabled in Device Manager (some driver
  updates silently reset this), Fast Startup actually off, PC and Pi on
  the same broadcast domain (same router, not different VLANs/guest
  network).
