#!/usr/bin/env python3
"""test_mc_waker.py -- self-contained protocol test for mc_waker.py (stdlib only, no root, no systemd).

Starts mc_waker.py on free high ports bound to 127.0.0.1 with a temp RUNTIME_DIRECTORY / CREDENTIALS_DIRECTORY and
plays the client side of each protocol path. Run:  python3 deploy/files/waker/test_mc_waker.py
"""
import json, os, socket, struct, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
WAKER = os.path.join(HERE, "mc_waker.py")
MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
RESULTS = []


# ---------------------------------------------------------------- helpers
def free_port():
    """A port free on BOTH tcp and udp."""
    while True:
        t = socket.socket(); t.bind(("127.0.0.1", 0)); p = t.getsockname()[1]
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            u.bind(("127.0.0.1", p)); return p
        except OSError:
            continue
        finally:
            t.close(); u.close()


def vi(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def pkt(pid, payload=b""):
    body = vi(pid) + payload
    return vi(len(body)) + body


def jstr(s):
    b = s.encode() if isinstance(s, str) else s
    return vi(len(b)) + b


def handshake(port, nxt, proto=775):
    return pkt(0, vi(proto) + jstr("localhost") + struct.pack(">H", port) + vi(nxt))


def read_vi(sock):
    n = shift = 0
    for _ in range(5):
        c = sock.recv(1)
        if not c:
            raise EOFError
        n |= (c[0] & 0x7F) << shift
        if not c[0] & 0x80:
            return n
        shift += 7


def read_pkt(sock):
    length = read_vi(sock)
    data = b""
    while len(data) < length:
        c = sock.recv(length - len(data))
        if not c:
            raise EOFError
        data += c
    return data


class Waker:
    def __init__(self, envvars, tmp, creds=True, whitelist=("Alice",), motd="Test Realm"):
        self.java, self.bedrock = free_port(), free_port()
        self.rundir = os.path.join(tmp, "run"); os.makedirs(self.rundir, exist_ok=True)
        self.creds = os.path.join(tmp, "creds"); os.makedirs(self.creds, exist_ok=True)
        if creds:
            with open(os.path.join(self.creds, "whitelist"), "w") as f:
                json.dump([{"uuid": "00000000-0000-0000-0000-000000000000", "name": n} for n in whitelist], f)
            with open(os.path.join(self.creds, "server.properties"), "w") as f:
                f.write(f"#Minecraft server properties\nmotd={motd}\\nsecond line\nmax-players=7\n")
        e = {k: v for k, v in os.environ.items() if not k.startswith("WAKER_")}
        e.update({"RUNTIME_DIRECTORY": self.rundir, "CREDENTIALS_DIRECTORY": self.creds, "WAKER_BIND": "127.0.0.1",
                  "WAKER_COALESCE_S": "0"})
        e.update({k: v.format(java=self.java, bedrock=self.bedrock) for k, v in envvars.items()})
        self.p = subprocess.Popen([sys.executable, "-I", WAKER], env=e, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 5
        while time.time() < deadline:                    # wait until the TCP port answers
            try:
                socket.create_connection(("127.0.0.1", self.java), timeout=0.2).close(); break
            except OSError:
                if self.p.poll() is not None:
                    raise RuntimeError("waker exited early:\n" + self.p.stdout.read())
                time.sleep(0.05)
        self.log = ""

    @property
    def flag(self):
        return os.path.join(self.rundir, "wake")

    def flag_json(self):
        with open(self.flag) as f:
            return json.load(f)

    def wait_flag(self, timeout=2.0):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(self.flag):
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
        try:
            out, _ = self.p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill(); out, _ = self.p.communicate()
        self.log += out or ""
        return self.log

    def exited(self, timeout=3.0):
        try:
            self.p.wait(timeout=timeout); return True
        except subprocess.TimeoutExpired:
            return False

    # ---- clients
    def java_status(self):
        s = socket.create_connection(("127.0.0.1", self.java), timeout=3)
        s.sendall(handshake(self.java, 1) + pkt(0))
        data = read_pkt(s); s.close()
        assert data[0] == 0
        n, i = data[1], 2                     # short JSON fits a 2-byte varint; decode properly
        length = 0; shift = 0; i = 1
        while True:
            b = data[i]; i += 1; length |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        return json.loads(data[i:i + length])

    def java_login(self, name, raw=None):
        s = socket.create_connection(("127.0.0.1", self.java), timeout=3)
        s.sendall(raw if raw is not None else handshake(self.java, 2) + pkt(0, jstr(name) + os.urandom(16)))
        try:
            data = read_pkt(s)
            return data[1:].decode("utf-8", "replace")
        except (EOFError, ConnectionResetError, socket.timeout):
            return ""
        finally:
            s.close()

    def raknet(self, payload):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(2)
        u.sendto(payload, ("127.0.0.1", self.bedrock))
        try:
            return u.recvfrom(4096)[0]
        except socket.timeout:
            return b""
        finally:
            u.close()

    def http(self, raw):
        s = socket.create_connection(("127.0.0.1", self.bedrock), timeout=3)
        s.sendall(raw)
        out = b""
        try:
            while True:
                c = s.recv(4096)
                if not c:
                    break
                out += c
        except (socket.timeout, ConnectionResetError):
            pass
        s.close()
        return out


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def scenario(title, envvars, fn, **kw):
    print(f"\n== {title}")
    with tempfile.TemporaryDirectory() as tmp:
        w = Waker(envvars, tmp, **kw)
        try:
            fn(w, tmp)
        finally:
            log = w.stop()
            if os.environ.get("VERBOSE"):
                print("   --- waker log ---\n   " + log.strip().replace("\n", "\n   "))


SDP = b"v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n" + b"a=x\r\n" * 20
BASE = {"WAKER_ID": "mctest", "WAKER_JAVA_PORT": "{java}", "WAKER_BEDROCK_PORT": "{bedrock}"}


# ---------------------------------------------------------------- scenarios
def t_status_and_whitelist(w, tmp):
    st = w.java_status()
    desc = st["description"]["text"]
    check("java status answers asleep with the server name (from server.properties motd)",
          "Test Realm" in desc and "asleep" in desc, desc)
    check("java status echoes the client protocol + max-players from server.properties",
          st["version"]["protocol"] == 775 and st["players"]["max"] == 7, st)
    r = w.java_login("Mallory")
    check("whitelist: non-whitelisted login gets 'not whitelisted', no wake",
          "not whitelisted" in r and not w.wait_flag(0.5), r)
    r = w.java_login("alice")
    check("whitelist: whitelisted login (case-insensitive) gets the waking text", "Waking Test Realm up" in r, r)
    check("whitelist: whitelisted login raises the wake flag", w.wait_flag())
    fj = w.flag_json() if os.path.exists(w.flag) else {}
    check("flag JSON records via=java + name + path/mode/server",
          fj.get("via") == "java" and fj.get("name") == "alice" and fj.get("path") == "java-login"
          and fj.get("mode") == "whitelist" and fj.get("server") == "mctest", fj)
    check("waker exits after a wake", w.exited())
    log = w.stop()
    check("wake is logged with mode, ip, path, name", "WAKE mode=whitelist ip=127.0.0.1 path=java-login name='alice'" in log)


def t_open_java(w, tmp):
    # garbage: random bytes
    w.java_login("", raw=os.urandom(40))
    # half handshake: length says 30 bytes, only 5 sent, then close
    s = socket.create_connection(("127.0.0.1", w.java), timeout=3); s.sendall(b"\x1e\x00\x83\x06\x09"); s.close()
    # valid handshake, no login-start at all
    s = socket.create_connection(("127.0.0.1", w.java), timeout=3); s.sendall(handshake(w.java, 2)); s.close()
    # valid handshake + an invalid name (17 chars / bad chars)
    w.java_login("ThisNameIsWayTooLong")
    w.java_login("bad name!")
    # handshake with trailing junk (not exactly a handshake) + valid name
    hs = handshake(w.java, 2)
    junk_hs = pkt(0, hs[2:] + b"\x00\x00")          # same body + 2 extra bytes, re-framed
    w.java_login("", raw=junk_hs + pkt(0, jstr("Steve") + os.urandom(16)))
    time.sleep(0.3)
    check("open: garbage / half handshake / no login-start / bad names / junk handshake do not wake",
          not w.wait_flag(0.5))
    r = w.java_login("Steve_01")
    check("open: a valid handshake + login-start from any name wakes", "Waking" in r and w.wait_flag(), r)
    log = w.stop()
    check("open: rejected logins logged as malformed", "malformed handshake/login-start" in log)
    check("open: wake logged", "WAKE mode=open ip=127.0.0.1 path=java-login name='Steve_01'" in log)


def t_open_floodgate(w, tmp):
    r = w.java_login(".BedrockGuy")
    check("open: a Floodgate-prefixed name ('.' + valid) is accepted", "Waking" in r and w.wait_flag(), r)


def t_raknet(w, tmp):
    pong = w.raknet(b"\x01" + struct.pack(">Q", 1234) + MAGIC + struct.pack(">Q", 99))
    motd = pong[35:].decode("utf-8", "replace") if pong else ""
    check("bedrock: unconnected ping answers a pong with an asleep MOTD + the name",
          pong[:1] == b"\x1c" and "Test Realm (asleep)" in motd and motd.startswith("MCPE;"), motd)
    w.raknet(b"\x05" + b"\x00" * 20)                  # no magic -> ignored
    check("bedrock: open-connection request without the magic does not wake", not w.wait_flag(0.4))
    w.raknet(b"\x05" + MAGIC + b"\x0b" + b"\x00" * 40)
    check("bedrock: open connection request 1 wakes", w.wait_flag())
    fj = w.flag_json() if os.path.exists(w.flag) else {}
    check("bedrock: flag via=bedrock path=raknet-open", fj.get("via") == "bedrock" and fj.get("path") == "raknet-open", fj)


def t_nethernet(w, tmp):
    r = w.http(b"GET /v1/join HTTP/1.1\r\nHost: x\r\nUpgrade: h2c\r\nUser-Agent: test\r\n\r\n")
    head, _, body = r.partition(b"\r\n\r\n")
    ok = head.startswith(b"HTTP/1.1 200")
    try:
        js = json.loads(body); ok = ok and "asleep" in js["name"] and "Test Realm" in js["name"]
    except Exception:
        ok = False
    check("nethernet: GET /v1/join (with h2c upgrade asked) returns 200 status JSON as HTTP/1.1", ok, r[:200])
    r = w.http(b"GET /other HTTP/1.1\r\n\r\n")
    check("nethernet: other paths 404", r.startswith(b"HTTP/1.1 404"), r[:60])
    r = w.http(b"POST /v1/join HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world")
    check("nethernet: POST without an SDP body is 400 and does not wake",
          r.startswith(b"HTTP/1.1 400") and not w.wait_flag(0.4), r[:60])
    r = w.http(b"POST /v1/join HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(SDP) + SDP)
    check("nethernet: POST with an SDP offer gets 503 waking + raises the flag",
          r.startswith(b"HTTP/1.1 503") and w.wait_flag(), r[:80])
    log = w.stop()
    check("nethernet: the SDP offer content is never logged", b"4611731400430051336".decode() not in log
          and "v=0" not in log)


def t_nethernet_h2(w, tmp):
    def frame(t, fl, sid, p=b""):
        return len(p).to_bytes(3, "big") + bytes([t, fl]) + sid.to_bytes(4, "big") + p
    pre = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + frame(4, 0, 0)
    r = w.http(pre + frame(1, 0x5, 1, b"\x82"))       # HEADERS END_STREAM|END_HEADERS (body-less)
    check("nethernet h2: body-less request gets a 200 status answer", b"asleep" in r and not w.wait_flag(0.3))
    r = w.http(pre + frame(1, 0x4, 3, b"\x83") + frame(0, 0x1, 3, SDP[:200]))
    check("nethernet h2: DATA with an SDP offer wakes", w.wait_flag())


def t_cooldown(w, tmp):
    # pre-seed a recent wake stamp, as a previous (restarted) waker would have left it
    with open(os.path.join(w.rundir, "last_wake"), "w") as f:
        f.write(f"{time.time() - 30:.3f}\n")
    r = w.java_login("Steve")
    check("cooldown: a valid open-mode login within the cooldown still gets the waking text", "Waking" in r, r)
    check("cooldown: ...but no second wake flag", not w.wait_flag(0.6))
    w.raknet(b"\x05" + MAGIC + b"\x0b" + b"\x00" * 40)
    check("cooldown: applies to bedrock too", not w.wait_flag(0.5))
    log = w.stop()
    check("cooldown: logged", "COOLDOWN mode=open ip=127.0.0.1" in log)


def t_cooldown_expired(w, tmp):
    with open(os.path.join(w.rundir, "last_wake"), "w") as f:
        f.write(f"{time.time() - 3600:.3f}\n")
    w.java_login("Steve")
    check("cooldown: an expired stamp allows a wake", w.wait_flag())


def t_ip_limit(tmp_unused=None):
    print("\n== open mode: per-IP limit (WAKER_IP_LIMIT=2, cooldown 0, state survives restarts)")
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(BASE, WAKER_MODE="open", WAKER_COOLDOWN_S="0", WAKER_IP_LIMIT="2")
        logs = ""
        for n in (1, 2):                                  # attempts 1 and 2 wake (each waker exits; restart it)
            w = Waker(env, tmp)
            w.java_login("Steve")
            check(f"ip-limit: attempt {n} (within budget) wakes", w.wait_flag())
            w.exited(); logs += w.stop()
            os.remove(w.flag)
        w = Waker(env, tmp)
        w.java_login("Steve")
        check("ip-limit: attempt 3 from the same IP does not wake", not w.wait_flag(0.6))
        w.raknet(b"\x05" + MAGIC + b"\x0b" + b"\x00" * 40)
        w.http(b"POST /v1/join HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(SDP) + SDP)
        check("ip-limit: bedrock + nethernet from that IP are ignored too", not w.wait_flag(0.6))
        logs += w.stop()
        check("ip-limit: logged exactly once per window", logs.count("IP-LIMIT") == 1, logs.count("IP-LIMIT"))
        with open(os.path.join(tmp, "run", "ip_attempts.json")) as f:
            st = json.load(f)
        check("ip-limit: state file in the runtime dir tracks the IP", "127.0.0.1" in st)


def t_required_env(tmp_unused=None):
    print("\n== required settings")
    e = {k: v for k, v in os.environ.items() if not k.startswith(("WAKER_", "RUNTIME_DIRECTORY"))}
    r = subprocess.run([sys.executable, "-I", WAKER], env=e, capture_output=True, text=True, timeout=10)
    check("no WAKER_ID -> refuses to start", r.returncode != 0 and "WAKER_ID" in r.stderr)
    e2 = dict(e, WAKER_ID="mctest", WAKER_JAVA_PORT="70000", WAKER_BEDROCK_PORT="19999")
    r = subprocess.run([sys.executable, "-I", WAKER], env=e2, capture_output=True, text=True, timeout=10)
    check("out-of-range port -> refuses to start", r.returncode != 0 and "out of range" in r.stderr)


def t_name_override(w, tmp):
    st = w.java_status()
    check("WAKER_NAME overrides the server.properties motd", "Harbor Town" in st["description"]["text"]
          and "Test Realm" not in st["description"]["text"])


def t_no_creds(w, tmp):
    st = w.java_status()
    check("no credentials: name falls back to WAKER_ID", "mctest" in st["description"]["text"])
    r = w.java_login("Alice")
    check("no credentials + whitelist mode: nobody wakes via Java", "not whitelisted" in r and not w.wait_flag(0.4))


def t_slow_client(w, tmp):
    # one client that sends half a handshake and stalls must not block others
    s = socket.create_connection(("127.0.0.1", w.java), timeout=10); s.sendall(b"\x10")
    t0 = time.time(); st = w.java_status(); dt = time.time() - t0
    check("a stalled client doesn't block another client's status ping", "asleep" in st["description"]["text"]
          and dt < 1.5, f"{dt:.2f}s")
    s.close()


def t_custom_texts(w, tmp):
    desc = w.java_status()["description"]["text"]
    check("WAKER_ASLEEP_TEXT replaces the server-list line (name kept)",
          "players seen before: join to wake; new players: /wake" in desc and "Test Realm" in desc
          and "about a minute" not in desc, desc)
    r = w.java_login("Mallory")
    check("WAKER_DENY_TEXT replaces the whitelist refusal, still no wake",
          "Wake it with /wake" in r and "not whitelisted" not in r and not w.wait_flag(0.5), r)
    r = w.java_login("alice")
    check("custom texts: a known name still gets the waking text + flag", "Waking Test Realm up" in r and w.wait_flag(), r)


def t_text_too_long():
    print("\n== WAKER_DENY_TEXT over 200 chars refuses to start")
    with tempfile.TemporaryDirectory() as tmp:
        e = dict(os.environ, WAKER_ID="mctest", WAKER_JAVA_PORT="25999", WAKER_BEDROCK_PORT="19999",
                 RUNTIME_DIRECTORY=tmp, WAKER_DENY_TEXT="x" * 201)
        r = subprocess.run([sys.executable, "-I", WAKER], env=e, capture_output=True, text=True, timeout=10)
        check("over-long text: exits non-zero with a clear message",
              r.returncode != 0 and "at most 200" in (r.stderr + r.stdout), r.stderr[-200:])

if __name__ == "__main__":
    scenario("whitelist mode: status + logins", dict(BASE), t_status_and_whitelist)
    scenario("open mode: java login validation", dict(BASE, WAKER_MODE="open"), t_open_java)
    scenario("open mode: floodgate prefix", dict(BASE, WAKER_MODE="open"), t_open_floodgate)
    scenario("bedrock RakNet (open)", dict(BASE, WAKER_MODE="open"), t_raknet)
    scenario("bedrock RakNet (whitelist mode still wakes: Bedrock sends no name this early)", dict(BASE), t_raknet)
    scenario("NetherNet HTTP/1.1", dict(BASE, WAKER_MODE="open"), t_nethernet)
    scenario("NetherNet HTTP/2 prior knowledge", dict(BASE, WAKER_MODE="open"), t_nethernet_h2)
    scenario("open mode: cooldown blocks a second flag", dict(BASE, WAKER_MODE="open"), t_cooldown)
    scenario("open mode: cooldown expiry", dict(BASE, WAKER_MODE="open"), t_cooldown_expired)
    t_ip_limit()
    t_required_env()
    scenario("WAKER_NAME", dict(BASE, WAKER_NAME="Harbor Town"), t_name_override)
    scenario("WAKER_ASLEEP_TEXT + WAKER_DENY_TEXT", dict(BASE,
             WAKER_ASLEEP_TEXT="asleep, players seen before: join to wake; new players: /wake",
             WAKER_DENY_TEXT="This server is asleep. Wake it with /wake in the server's Discord."), t_custom_texts)
    t_text_too_long()
    scenario("no credentials", dict(BASE), t_no_creds, creds=False)
    scenario("slow client isolation", dict(BASE, WAKER_MODE="open"), t_slow_client)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)
