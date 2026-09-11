// rcon.js — persistent RCON client for mcfleet-web.
//
// WHY: the live-map /positions endpoint used to shell out to `python mc_rcon.py` on every poll
// (~1/sec per viewer, 3 calls each for Pos/Dimension/Rotation). Each shell-out spawned a python
// process AND opened a fresh RCON connection, which (a) flooded the MC server log with
// "Thread RCON Client … started/shutting down" (~86% of the log) and (b) burned CPU on the shared
// box. This holds ONE long-lived authenticated socket per server and reuses it for every command,
// so the server logs a single connection instead of thousands.
//
// Model (matches mc_rcon.py's proven framing): one command in flight at a time per server; each
// request maps 1:1 to the next reply packet (our commands — list, data get — are small,
// single-packet responses). Auto-reconnect + re-auth on drop; a dropped socket just fails the
// current poll (the map skips one frame) and the next call reconnects.
const net = require('net');
const fs = require('fs');
const path = require('path');

const AUTH = 3, EXEC = 2;             // SERVERDATA_AUTH / SERVERDATA_EXECCOMMAND
const pools = new Map();              // server dir -> pool

function readProps(dir) {
  const txt = fs.readFileSync(path.join(dir, 'server.properties'), 'utf8');
  const get = k => (txt.match(new RegExp('^' + k + '=(.*)$', 'm')) || [, ''])[1].trim();
  return { host: '127.0.0.1', port: parseInt(get('rcon.port') || '25575', 10), password: get('rcon.password') };
}

function packet(id, type, payload) {
  const body = Buffer.from(payload, 'utf8');
  const buf = Buffer.alloc(body.length + 14);      // 4 len + 4 id + 4 type + payload + 2 nulls
  buf.writeInt32LE(body.length + 10, 0);
  buf.writeInt32LE(id, 4);
  buf.writeInt32LE(type, 8);
  body.copy(buf, 12);                              // last 2 bytes stay 0 (alloc zero-fills)
  return buf;
}

function getPool(dir) {
  let p = pools.get(dir);
  if (!p) { p = { sock: null, authed: false, buf: Buffer.alloc(0), reqId: 0, inflight: null, queue: [], connecting: null }; pools.set(dir, p); }
  return p;
}

function fail(p, err) {
  // reject the in-flight + everything queued, drop the socket; next call reconnects.
  const e = err instanceof Error ? err : new Error(String(err));
  if (p.inflight) { clearTimeout(p.inflight.timer); p.inflight.reject(e); p.inflight = null; }
  while (p.queue.length) { const w = p.queue.shift(); w.reject(e); }
  if (p.sock) { try { p.sock.destroy(); } catch {} }
  p.sock = null; p.authed = false; p.buf = Buffer.alloc(0); p.connecting = null;
}

function onData(p, chunk) {
  p.buf = Buffer.concat([p.buf, chunk]);
  // parse as many complete packets as are buffered
  while (p.buf.length >= 4) {
    const len = p.buf.readInt32LE(0);
    if (p.buf.length < 4 + len) break;             // wait for the rest
    const frame = p.buf.subarray(4, 4 + len);
    p.buf = p.buf.subarray(4 + len);
    const id = frame.readInt32LE(0);               // (type at 4; body = frame[8 : len-2])
    const body = frame.subarray(8, len - 2).toString('utf8');
    const w = p.inflight;
    if (!w) continue;                              // stray packet; ignore
    if (w.auth) {
      if (id === -1) { fail(p, new Error('RCON auth failed')); return; }
      clearTimeout(w.timer); p.inflight = null; p.authed = true; w.resolve('');
    } else {
      clearTimeout(w.timer); p.inflight = null; w.resolve(body);
    }
    pump(p);                                        // send the next queued command
  }
}

function pump(p) {
  if (p.inflight || !p.queue.length || !p.sock || !p.authed) return;
  const w = p.queue.shift();
  p.inflight = w;
  w.timer = setTimeout(() => { fail(p, new Error('RCON command timeout')); }, w.timeout);
  try { p.sock.write(packet(++p.reqId, EXEC, w.command)); }
  catch (e) { fail(p, e); }
}

function connect(dir) {
  const p = getPool(dir);
  if (p.connecting) return p.connecting;
  if (p.sock && p.authed) return Promise.resolve(p);
  p.connecting = new Promise((resolve, reject) => {
    let props;
    try { props = readProps(dir); } catch (e) { return reject(e); }
    if (!props.password) return reject(new Error('RCON disabled / no password'));
    const sock = net.connect({ host: props.host, port: props.port });
    sock.setNoDelay(true);
    const to = setTimeout(() => { sock.destroy(); reject(new Error('RCON connect timeout')); }, 5000);
    sock.on('data', d => onData(p, d));
    sock.on('error', e => { clearTimeout(to); fail(p, e); reject(e); });
    sock.on('close', () => { fail(p, new Error('RCON socket closed')); });
    sock.on('connect', () => {
      clearTimeout(to);
      p.sock = sock; p.buf = Buffer.alloc(0);
      // authenticate; the auth reply is the next packet (id === our id on success, -1 on failure)
      p.inflight = { auth: true, resolve: () => resolve(p), reject, timer: setTimeout(() => fail(p, new Error('RCON auth timeout')), 5000) };
      try { sock.write(packet(++p.reqId, AUTH, props.password)); } catch (e) { fail(p, e); reject(e); }
    });
  }).finally(() => { p.connecting = null; });
  return p.connecting;
}

// exec(dir, command, timeout=8000) -> Promise<string reply>. Reuses the persistent socket.
async function exec(dir, command, timeout = 8000) {
  const p = await connect(dir);
  return new Promise((resolve, reject) => { p.queue.push({ command, timeout, resolve, reject }); pump(p); });
}

module.exports = { exec };
