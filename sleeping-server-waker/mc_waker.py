#!/usr/bin/env python3
"""mc_waker.py -- stands in for a SLEEPING Minecraft server on its ports and wakes it on a real join attempt.

Generalized from a waker written for one server to any number of them: all servers sleep when empty, and a real join
attempt wakes the public ones. One instance per server, run by mc-waker@<id>.service.
Everything server-specific comes from the environment (see ENVIRONMENT below / README.md); the code holds no
server literals.

Runs ONLY while the server is stopped, as a systemd DynamicUser with no privileges: it parses packets from the
internet, so it only listens, answers, and on a real join attempt drops a flag file ($RUNTIME_DIRECTORY/wake) and
exits. A root .path unit reacts to the flag and starts the server (see README.md).

  Java  TCP (WAKER_JAVA_PORT):
    status ping  -> "asleep, join to wake" in the server list (echoes the client's protocol so it isn't "outdated")
    login        -> mode=whitelist: wakes ONLY for a whitelisted name.
                    mode=open: wakes for any client that sent a well-formed handshake (next-state=login) followed by a
                    well-formed login-start (name 1-16 of [A-Za-z0-9_], optionally Floodgate-prefixed).
                    Either way the player is disconnected with "waking up, reconnect in about a minute".
  Bedrock UDP (WAKER_BEDROCK_PORT), RakNet offline messages:
    unconnected ping (0x01/0x02)       -> pong with an "asleep" MOTD
    open connection request 1 (0x05)   -> a real join attempt -> wake (Bedrock carries no name this early)
  Bedrock TCP (same port), NetherNet built-in signaling (Bedrock 26.60+ clients speak only NetherNet, which Geyser
  serves as plain HTTP signaling on TCP plus WebRTC on UDP, both on the Bedrock port):
    GET  /v1/join                      -> status JSON with an "asleep" name (what the server list shows)
    POST /v1/join with an SDP offer    -> a real join attempt -> wake. The offer is not read beyond its first bytes and
                                          never logged (it carries the player's Xbox identity token).
    anything else                      -> 404, no wake
    HTTP/2: an "Upgrade: h2c" request is answered as plain HTTP/1.1 (never 101). A client that opens with the HTTP/2
    preface gets a minimal HTTP/2 answer: a body-less request = status, a DATA frame holding an SDP offer = wake.

Scanner guard (on by default in open mode, see README): every wake-qualifying attempt is first counted against a
per-source-IP budget (WAKER_IP_LIMIT per WAKER_IP_WINDOW_S; beyond it the IP is ignored and logged once per window),
then checked against a wake cooldown (WAKER_COOLDOWN_S since the last flag, from a timestamp file in the runtime dir,
which survives restarts via RuntimeDirectoryPreserve=yes). Both state files live only in the runtime dir.

Hardened: every read is size-capped and time-limited, one bad client can't stall the others, and no filesystem
access beyond the runtime dir + read-only credentials (whitelist, server.properties).
"""
import asyncio, json, os, re, socket, struct, sys, time


def env(name, default=None):
    """WAKER_<name> from the environment (empty counts as unset)."""
    v = os.environ.get("WAKER_" + name)
    return default if v in (None, "") else v


# ---------------------------------------------------------------- configuration
RUNDIR_ENV = os.environ.get("RUNTIME_DIRECTORY", "")
SERVER_ID = env("ID")
if not SERVER_ID:
    sys.exit("mc_waker: WAKER_ID is not set (expected from /etc/mc-waker/<id>.env)")

def _port(name):
    v = env(name)
    if v is None:
        sys.exit(f"mc_waker: WAKER_{name} is not set")
    p = int(v)
    if not 0 < p < 65536:
        sys.exit(f"mc_waker: WAKER_{name}={v} is out of range")
    return p

JAVA_PORT = _port("JAVA_PORT")
BEDROCK_PORT = _port("BEDROCK_PORT")
UNIT = env("UNIT", SERVER_ID + ".service")          # informational here; the unit's ExecCondition uses it
MODE = (env("MODE", "whitelist") or "whitelist").lower()
if MODE not in ("whitelist", "open"):
    sys.exit(f"mc_waker: WAKER_MODE must be whitelist or open, not {MODE!r}")
RUNDIR = RUNDIR_ENV or f"/run/mc-waker-{SERVER_ID}"
CREDS = os.environ.get("CREDENTIALS_DIRECTORY", "")
BIND_HOST = env("BIND", "") or None                 # default: all interfaces (tests use 127.0.0.1)
JAVA_VERSION = env("JAVA_VERSION", "26.3")
BEDROCK_PROTOCOL = env("BEDROCK_PROTOCOL", "2193")
BEDROCK_VERSION = env("BEDROCK_VERSION", "26.51")
# What the awake Geyser's GET /v1/join reports (copied from a live server's answer); a client compares these.
NETHERNET_PROTOCOL = int(env("NETHERNET_PROTOCOL", "2225"))
NETHERNET_VERSION = env("NETHERNET_VERSION", "26.62")
FLOODGATE_PREFIX = env("FLOODGATE_PREFIX", ".")
# Scanner guard. Defaults: on in open mode, off in whitelist mode (a whitelist already limits who can wake the server;
# set COOLDOWN_S / IP_LIMIT explicitly to add the guard there too).
COOLDOWN_S = float(env("COOLDOWN_S", "600" if MODE == "open" else "0"))
IP_LIMIT = int(env("IP_LIMIT", "5" if MODE == "open" else "0"))          # 0 = no per-IP limit
IP_WINDOW_S = float(env("IP_WINDOW_S", "3600"))
COALESCE_S = float(env("COALESCE_S", "10"))      # attempts from one IP closer than this count as one
IP_TABLE_MAX = 4096                                                         # tracked IPs kept in the state file

MAX_HTTP_HEAD = 8192
MAX_SDP = 65536
MAX_PACKET = 4096
MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
SERVER_GUID = int.from_bytes(os.urandom(8), "big") >> 1
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,16}")
FMT_RE = re.compile(r"§.")

WAKE_FLAG = os.path.join(RUNDIR, "wake")
LAST_WAKE = os.path.join(RUNDIR, "last_wake")
IP_STATE = os.path.join(RUNDIR, "ip_attempts.json")


def log(*a):
    print(*a, flush=True)


def cred(name, default=""):
    if not CREDS:
        return default
    try:
        with open(os.path.join(CREDS, name), encoding="utf-8") as f:
            return f.read(1 << 20)
    except Exception:
        return default


def load_whitelist():
    try:
        return {e["name"].lower() for e in json.loads(cred("whitelist", "[]")) if e.get("name")}
    except Exception:
        return set()


def load_properties():
    props = {}
    for line in cred("server.properties").splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#"):
            props[k.strip()] = v
    return props


PROPS = load_properties()


def load_name():
    if env("NAME"):
        return env("NAME")
    raw = PROPS.get("motd", "").replace("\\:", ":").replace("\\=", "=")
    return raw.split("\\n")[0].strip() or SERVER_ID


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


WHITELIST = load_whitelist()
NAME = load_name()
PLAIN_NAME = FMT_RE.sub("", NAME).strip() or SERVER_ID          # for plain-text disconnect / HTTP bodies
MAX_PLAYERS = _int(env("MAX_PLAYERS"), _int(PROPS.get("max-players"), 20))
WAKING_TEXT = f"⏰ Waking {PLAIN_NAME} up, reconnect in about a minute."


def _text(name, default):
    """Operator-set player-facing text (WAKER_ASLEEP_TEXT / WAKER_DENY_TEXT). Defaults keep the old wording, so units
    that don't set them keep it. Capped so a typo can't build an oversized packet."""
    v = env(name, default)
    if len(v) > 200 or "\n" in v or "\r" in v:
        sys.exit(f"mc_waker: WAKER_{name} must be one line of at most 200 characters")
    return v

ASLEEP_TEXT = _text("ASLEEP_TEXT", "asleep, join to wake it (about a minute)")   # Java server-list 2nd line (§ ok)
DENY_TEXT = _text("DENY_TEXT", "You are not whitelisted on this server.")         # whitelist-mode login refusal
WAKING_BODY = f"{PLAIN_NAME} is waking up, join again in about a minute".encode("utf-8")
_woken = False


# ---------------------------------------------------------------- scanner guard state (runtime dir only)
def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _load_ip_state():
    try:
        with open(IP_STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_ip_state = _load_ip_state()      # {ip: {"t": [attempt timestamps], "logged": window_start_logged}}


def _save_ip_state():
    try:
        if len(_ip_state) > IP_TABLE_MAX:          # keep the most recently active IPs
            keep = sorted(_ip_state, key=lambda k: max(_ip_state[k].get("t") or [0]), reverse=True)[:IP_TABLE_MAX]
            for k in list(_ip_state):
                if k not in keep:
                    del _ip_state[k]
        _write_atomic(IP_STATE, json.dumps(_ip_state, separators=(",", ":")))
    except Exception as e:
        log("ip state not saved ->", type(e).__name__)


def ip_allowed(ip, path):
    """Count one wake-qualifying attempt from ip. False = over the per-IP budget (logged once per window)."""
    if IP_LIMIT <= 0:
        return True
    now = time.time()
    rec = _ip_state.setdefault(ip, {"t": [], "logged": 0})
    rec["t"] = [t for t in rec.get("t", []) if now - t < IP_WINDOW_S][-(IP_LIMIT + 1):]
    if not rec["t"] or now - rec["t"][-1] >= COALESCE_S:     # a burst (RakNet retries) counts as ONE attempt
        rec["t"].append(now)
    over = len(rec["t"]) > IP_LIMIT
    if over and now - rec.get("logged", 0) >= IP_WINDOW_S:
        rec["logged"] = now
        log(f"IP-LIMIT mode={MODE} ip={ip} path={path}: more than {IP_LIMIT} join attempts in "
            f"{int(IP_WINDOW_S)}s, ignoring this IP (logged once per window)")
    _save_ip_state()
    return not over


def in_cooldown():
    if COOLDOWN_S <= 0:
        return False
    try:
        with open(LAST_WAKE) as f:
            last = float(f.read(64).strip())
    except Exception:
        return False
    return 0 <= time.time() - last < COOLDOWN_S


_cooldown_logged = set()


def try_wake(path, via, ip, name=None):
    """A wake-qualifying attempt arrived. Returns True if it raised the flag (caller then shows the waking text)."""
    if _woken:                       # already raised; the process exits in a moment (RakNet retries land here)
        return False
    if not ip_allowed(ip, path):
        return False
    if in_cooldown():
        if ip not in _cooldown_logged:
            _cooldown_logged.add(ip)
            log(f"COOLDOWN mode={MODE} ip={ip} path={path}: a wake was already raised less than {int(COOLDOWN_S)}s "
                f"ago, not raising another")
        return False
    wake({"via": via, "path": path, "mode": MODE, "server": SERVER_ID, "ip": ip, **({"name": name} if name else {})})
    return True


def wake(info):
    """Write the flag (atomically) once, record the cooldown timestamp, then stop serving."""
    global _woken
    if _woken:
        return
    _woken = True
    now = time.time()
    info["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    _write_atomic(WAKE_FLAG, json.dumps(info))
    try:
        _write_atomic(LAST_WAKE, f"{now:.3f}\n")
    except Exception as e:
        log("cooldown stamp not saved ->", type(e).__name__)
    log(f"WAKE mode={info['mode']} ip={info['ip']} path={info['path']}"
        + (f" name={info['name']!r}" if info.get("name") else ""))
    asyncio.get_running_loop().call_later(1.5, lambda: os._exit(0))   # let the disconnect message flush


# ---------------------------------------------------------------- Java
def vi_enc(n):
    n &= 0xFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def pkt(pid, payload=b""):
    body = vi_enc(pid) + payload
    return vi_enc(len(body)) + body


def jstr(s):
    b = s.encode("utf-8")
    return vi_enc(len(b)) + b


async def read_varint(reader):
    n = shift = 0
    for _ in range(5):
        b = (await reader.readexactly(1))[0]
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n
        shift += 7
    raise ValueError("varint too long")


def vi_dec(buf, i):
    n = shift = 0
    for _ in range(5):
        b = buf[i]; i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
    raise ValueError("varint too long")


async def read_packet(reader):
    length = await read_varint(reader)
    if not 0 < length <= MAX_PACKET:
        raise ValueError(f"bad length {length}")
    data = await reader.readexactly(length)
    pid, i = vi_dec(data, 0)
    return pid, data[i:]


def parse_handshake(data):
    """-> (proto, next_state, well_formed). well_formed = the packet is exactly a valid handshake."""
    pid, i = vi_dec(data, 0)
    if pid != 0:
        return None, None, False
    proto, i = vi_dec(data, i)
    alen, i = vi_dec(data, i)
    addr = data[i:i + alen]
    i += alen + 2                                    # server address + port
    nxt, i = vi_dec(data, i)
    ok = 0 < alen <= 255 and len(addr) == alen and i == len(data)
    if ok:
        try:
            addr.decode("utf-8")
        except UnicodeDecodeError:
            ok = False
    return proto, nxt, ok


def parse_login_name(payload):
    """-> (name, syntactically_valid). Valid: 1-16 of [A-Za-z0-9_], optionally after the Floodgate prefix."""
    nlen, j = vi_dec(payload, 0)
    raw = payload[j:j + min(nlen, 64)]
    name = raw.decode("utf-8", "replace")
    if not 0 < nlen <= 16 + len(FLOODGATE_PREFIX) or len(raw) != nlen:
        return name, False
    core = name[len(FLOODGATE_PREFIX):] if FLOODGATE_PREFIX and name.startswith(FLOODGATE_PREFIX) else name
    return name, bool(NAME_RE.fullmatch(core))


async def java_client(reader, writer):
    peer = writer.get_extra_info("peername")
    ip = peer[0] if peer else "?"
    try:
        async def session():
            first = await reader.readexactly(1)
            if first == b"\xfe":            # legacy (pre-1.7) ping: just close
                return
            # reconstruct the first varint (packet length) now that we've consumed its first byte
            length = first[0] & 0x7F; shift = 7; b = first[0]
            while b & 0x80:
                b = (await reader.readexactly(1))[0]
                length |= (b & 0x7F) << shift; shift += 7
                if shift > 35:
                    raise ValueError("varint too long")
            if not 0 < length <= MAX_PACKET:
                raise ValueError("bad handshake length")
            data = await reader.readexactly(length)
            proto, nxt, hs_ok = parse_handshake(data)
            if proto is None:
                return
            if nxt == 1:                                     # status
                pid, _ = await read_packet(reader)
                if pid != 0:
                    return
                status = {"version": {"name": f"{JAVA_VERSION} (asleep)", "protocol": proto},
                          "players": {"max": MAX_PLAYERS, "online": 0},
                          "description": {"text": f"\U0001F4A4 {NAME}\n§7{ASLEEP_TEXT}"}}
                writer.write(pkt(0, jstr(json.dumps(status)))); await writer.drain()
                try:
                    pid, payload = await read_packet(reader)
                    if pid == 1:
                        writer.write(pkt(1, payload[:8])); await writer.drain()
                except Exception:
                    pass
            elif nxt in (2, 3):                              # login / transfer
                pid, payload = await read_packet(reader)
                name, name_ok = parse_login_name(payload) if pid == 0 else ("", False)
                if MODE == "whitelist":
                    qualifies = name.lower() in WHITELIST
                    reason = "not whitelisted"
                else:
                    qualifies = hs_ok and nxt == 2 and pid == 0 and name_ok
                    reason = "malformed handshake/login-start"
                if not qualifies:
                    log(f"ignored java login ({reason}): {name[:20]!r} from {ip}")
                    if MODE == "whitelist":
                        writer.write(pkt(0, jstr(json.dumps({"text": DENY_TEXT}))))
                        await writer.drain()
                    return
                # a qualifying attempt: show the waking text even if the guard swallows it (cooldown = already waking)
                try_wake("java-login", "java", ip, name)
                writer.write(pkt(0, jstr(json.dumps({"text": WAKING_TEXT})))); await writer.drain()
        await asyncio.wait_for(session(), timeout=8)
    except Exception as e:
        log("java client", ip, "->", type(e).__name__)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Bedrock NetherNet signaling (TCP, plain HTTP/1.1)
H2_PREFACE_HEAD = b"PRI * HTTP/2.0\r\n\r\n"          # readuntil(\r\n\r\n) stops here; "SM\r\n\r\n" follows


def status_json():
    return json.dumps({"dataVersion": 7, "name": f"\U0001F4A4 {NAME} (asleep, join to wake it)",
                       "protocol": NETHERNET_PROTOCOL, "version": NETHERNET_VERSION, "level": NAME,
                       "players": 0, "maxPlayers": MAX_PLAYERS, "gameType": 0, "editor": False, "hardcore": False,
                       "onlineAuth": True, "selfSignedAuth": False, "nonce": os.urandom(8).hex(),
                       "transportLayer": 2, "connection": 4}, separators=(",", ":")).encode("utf-8")


def h2_frame(ftype, flags, stream, payload=b""):
    return len(payload).to_bytes(3, "big") + bytes([ftype, flags]) + (stream & 0x7FFFFFFF).to_bytes(4, "big") + payload


def h2_respond(writer, stream, status_index, body, ctype):
    # HPACK: :status as an indexed static-table entry (8 = 200, 14 = 500; 503 has no static entry), then content-type
    # as a literal without indexing on static name index 31.
    ct = ctype.encode("ascii")
    headers = bytes([0x80 | status_index]) + b"\x0f\x10" + bytes([len(ct)]) + ct
    writer.write(h2_frame(0x1, 0x4, stream, headers) + h2_frame(0x0, 0x1, stream, body))


async def h2_session(reader, writer, ip):
    if await reader.readexactly(6) != b"SM\r\n\r\n":
        return
    writer.write(h2_frame(0x4, 0x0, 0))                         # our (empty) SETTINGS
    await writer.drain()
    for _ in range(64):                                         # a handful of frames is all a status or a join needs
        hdr = await reader.readexactly(9)
        length, ftype, flags = int.from_bytes(hdr[:3], "big"), hdr[3], hdr[4]
        stream = int.from_bytes(hdr[5:9], "big") & 0x7FFFFFFF
        if length > 16384:
            return
        payload = await reader.readexactly(length)
        if ftype == 0x4 and not flags & 0x1:                    # client SETTINGS -> ACK
            writer.write(h2_frame(0x4, 0x1, 0))
        elif ftype == 0x1 and flags & 0x1:                      # HEADERS ending the stream = no body = status request
            log("signaling h2 status from", ip)
            h2_respond(writer, stream, 8, status_json(), "application/json")      # 8 = :status 200
        elif ftype == 0x0:                                      # DATA: an SDP offer = a real join
            if flags & 0x8 and payload:
                payload = payload[1:len(payload) - payload[0]]  # strip padding
            if payload.lstrip().startswith(b"v=0"):
                log("signaling h2 join (SDP offer) from", ip)
                h2_respond(writer, stream, 14, WAKING_BODY, "text/plain")         # 14 = 500
                await writer.drain()
                try_wake("nethernet-offer", "bedrock-nethernet-h2", ip)
                return
        elif ftype == 0x7:                                      # GOAWAY
            return
        await writer.drain()


def http_reply(writer, status, body=b"", ctype="application/json"):
    head = f"HTTP/1.1 {status}\r\ncontent-type: {ctype}\r\ncontent-length: {len(body)}\r\nconnection: close\r\n\r\n"
    writer.write(head.encode("ascii") + body)


async def signaling_client(reader, writer):
    peer = writer.get_extra_info("peername")
    ip = peer[0] if peer else "?"
    try:
        async def session():
            head = await reader.readuntil(b"\r\n\r\n")            # LimitOverrunError past the stream limit
            if len(head) > MAX_HTTP_HEAD:
                return
            if head == H2_PREFACE_HEAD:
                await h2_session(reader, writer, ip)
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                return
            method, path = parts[0], parts[1].split("?", 1)[0]
            headers = {}
            for line in lines[1:]:
                k, sep, v = line.partition(":")
                if sep:
                    headers[k.strip().lower()] = v.strip()
            if path not in ("/v1/join", "/v1/join/"):
                http_reply(writer, "404 Not Found", ctype="text/plain")
                await writer.drain()
                return
            log(f"signaling h1 {method[:8]} /v1/join from {ip}"
                f"{' (asked for h2c upgrade, answered as 1.1)' if 'h2c' in headers.get('upgrade', '').lower() else ''}"
                f" ua={headers.get('user-agent', '')[:60]!r}")
            if method == "GET":
                http_reply(writer, "200 OK", status_json())
            elif method == "POST":
                try:
                    length = int(headers.get("content-length", "0"))
                except ValueError:
                    length = 0
                if not 0 < length <= MAX_SDP:
                    http_reply(writer, "400 Bad Request", ctype="text/plain")
                    await writer.drain()
                    return
                body = await reader.readexactly(min(length, 64))   # only enough to recognise an SDP offer
                if body.lstrip().startswith(b"v=0"):
                    http_reply(writer, "503 Service Unavailable", WAKING_BODY, ctype="text/plain")
                    await writer.drain()
                    try_wake("nethernet-offer", "bedrock-nethernet", ip)
                else:
                    http_reply(writer, "400 Bad Request", ctype="text/plain")
            else:
                http_reply(writer, "405 Method Not Allowed", ctype="text/plain")
            await writer.drain()
        await asyncio.wait_for(session(), timeout=8)
    except Exception as e:
        log("signaling client", ip, "->", type(e).__name__)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Bedrock (RakNet offline messages)
class Bedrock(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.t = transport

    def datagram_received(self, data, addr):
        if len(data) > MAX_PACKET or len(data) < 1:
            return
        pid = data[0]
        if pid in (0x01, 0x02) and len(data) >= 25 and data[9:25] == MAGIC:
            ping_time = data[1:9]
            motd = (f"MCPE;\U0001F4A4 {NAME} (asleep);{BEDROCK_PROTOCOL};{BEDROCK_VERSION};0;{MAX_PLAYERS};"
                    f"{SERVER_GUID};join to wake it (about a minute);Survival;1;{BEDROCK_PORT};{BEDROCK_PORT};")
            m = motd.encode("utf-8")
            self.t.sendto(b"\x1c" + ping_time + struct.pack(">Q", SERVER_GUID) + MAGIC + struct.pack(">H", len(m)) + m, addr)
        elif pid == 0x05 and len(data) >= 18 and data[1:17] == MAGIC:
            try_wake("raknet-open", "bedrock", addr[0])


async def main():
    os.makedirs(RUNDIR, exist_ok=True)
    loop = asyncio.get_running_loop()
    srv = await asyncio.start_server(java_client, host=BIND_HOST, port=JAVA_PORT, reuse_address=True,
                                     limit=MAX_PACKET * 2)
    sig = await asyncio.start_server(signaling_client, host=BIND_HOST, port=BEDROCK_PORT, reuse_address=True,
                                     limit=MAX_HTTP_HEAD)
    await loop.create_datagram_endpoint(Bedrock, local_addr=(BIND_HOST or "0.0.0.0", BEDROCK_PORT),
                                        family=socket.AF_INET)
    log(f"mc-waker[{SERVER_ID}]: sleeping for {UNIT} on tcp/{JAVA_PORT} + tcp/{BEDROCK_PORT} (NetherNet signaling) + "
        f"udp/{BEDROCK_PORT}; mode={MODE}; name={NAME!r}; cooldown={int(COOLDOWN_S)}s; "
        f"ip_limit={IP_LIMIT}/{int(IP_WINDOW_S)}s"
        + (f"; whitelist={sorted(WHITELIST)}" if MODE == "whitelist" else ""))
    async with srv, sig:
        await asyncio.gather(srv.serve_forever(), sig.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
