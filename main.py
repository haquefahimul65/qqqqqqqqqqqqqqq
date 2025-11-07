#!/usr/bin/env python3
"""
NerdMiner-Style Solo BTC Lottery Miner + Dashboard
- public-pool.io
- Wallet: bc1qxfd40euj7vsxvff2ens8l6ear52w3mu0rwts6a
- Dashboard: http://0.0.0.0:8080
- No disk logs, cross-platform
- Pure threading (no multiprocessing)
"""

import socket
import json
import hashlib
import time
import threading
import struct
from binascii import unhexlify, hexlify
from typing import Optional, Dict, Any, Set, Tuple
import os
import psutil

# ===================== CONFIG =====================
POOL_HOST = "public-pool.io"
POOL_PORT = 21496
BTC_WALLET = "bc1qxfd40euj7vsxvff2ens8l6ear52w3mu0rwts6a"
MAX_WORKERS = 100
LOG_TO_FILE = False
DASHBOARD_PORT = 8080
# ================================================

# Global state
STATS = {"total_hashes": 0, "shares_submitted": 0, "shares_accepted": 0, "num_workers": 0}
STATS_LOCK = threading.Lock()
NUM_WORKERS = 1
GLOBAL_START = 0.0

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def bits_to_target(bits: int) -> int:
    exp = bits >> 24
    mant = bits & 0xFFFFFF
    return mant << (8 * (exp - 3))

def merkle_root(coinbase: bytes, merkle_branches: list) -> bytes:
    root = sha256d(coinbase)
    for branch_hex in merkle_branches:
        branch = unhexlify(branch_hex)[::-1]
        root = sha256d(root + branch)
    return root

def build_block_header(
    version: int,
    prev_hash: str,
    merkle_root: bytes,
    ntime: int,
    bits: int,
    nonce: int,
) -> bytes:
    header = (
        struct.pack("<L", version) +
        unhexlify(prev_hash)[::-1] +
        merkle_root[::-1] +
        struct.pack("<L", ntime) +
        struct.pack("<L", bits) +
        struct.pack("<L", nonce)
    )
    return header

class MinerWorker(threading.Thread):
    def __init__(self, worker_id: int):
        super().__init__(daemon=True)
        self.id = worker_id
        self.sock: Optional[socket.socket] = None
        self.extranonce1: bytes = b""
        self.extranonce2_size: int = 4
        self.job: Optional[Dict[str, Any]] = None
        self.difficulty: float = 1.0
        self.target: Optional[int] = None
        self.mining_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.hashes = 0
        self.start_time = time.time()
        self.submitted_cache: Set[Tuple[str, bytes, int, int]] = set()
        self.recv_buffer = b""

    def connect(self) -> bool:
        try:
            self.sock = socket.socket()
            self.sock.settimeout(30)
            self.sock.connect((POOL_HOST, POOL_PORT))
            return True
        except Exception:
            return False

    def send(self, msg: dict):
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode())
        except Exception:
            pass

    def recv(self) -> Optional[dict]:
        try:
            while b"\n" not in self.recv_buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return None
                self.recv_buffer += chunk
            line, self.recv_buffer = self.recv_buffer.split(b"\n", 1)
            return json.loads(line.decode())
        except Exception:
            return None

    def subscribe(self) -> bool:
        self.send({"id": 1, "method": "mining.subscribe", "params": []})
        resp = self.recv()
        if not resp or not resp.get("result"):
            return False
        self.extranonce1 = unhexlify(resp["result"][1])
        self.extranonce2_size = resp["result"][2]
        return True

    def authorize(self) -> bool:
        self.send({
            "id": 2,
            "method": "mining.authorize",
            "params": [f"{BTC_WALLET}.{self.id}", ""]
        })
        resp = self.recv()
        return resp and resp.get("result") is True

    def handle_notify(self, params: list):
        job_id, prevhash, cb1, cb2, branches, version, nbits, ntime, clean = params
        self.job = {
            "id": job_id,
            "prevhash": prevhash,
            "cb1": unhexlify(cb1),
            "cb2": unhexlify(cb2),
            "branches": branches,
            "version": int(version, 16),
            "nbits": int(nbits, 16),
            "ntime": int(ntime, 16),
            "clean": clean
        }
        self.target = bits_to_target(self.job["nbits"])
        if self.job["clean"]:
            self.stop_event.set()
            if self.mining_thread and self.mining_thread.is_alive():
                self.mining_thread.join(timeout=1)
            self.stop_event.clear()
            self.submitted_cache.clear()
            self.mining_thread = threading.Thread(target=self.mine, daemon=True)
            self.mining_thread.start()

    def handle_set_difficulty(self, params: list):
        self.difficulty = float(params[0]) if params else 1.0

    def submit_share(self, nonce: int, extranonce2: bytes, ntime: int):
        key = (self.job["id"], extranonce2, ntime, nonce)
        if key in self.submitted_cache:
            return
        self.submitted_cache.add(key)
        if len(self.submitted_cache) > 10000:
            # Keep only last 5000 to control memory
            self.submitted_cache = set(list(self.submitted_cache)[-5000:])

        params = [
            f"{BTC_WALLET}.{self.id}",
            self.job["id"],
            hexlify(extranonce2).decode(),
            f"{ntime:08x}",
            f"{nonce:08x}"
        ]
        self.send({"id": 3, "method": "mining.submit", "params": params})
        resp = self.recv()
        accepted = resp and resp.get("result") is True
        with STATS_LOCK:
            STATS["shares_submitted"] += 1
            if accepted:
                STATS["shares_accepted"] += 1
        status = "ACCEPTED" if accepted else "REJECTED"
        log(f"Worker {self.id}: Share {status} (D={self.difficulty})")
        if accepted and self.difficulty >= 1000000:
            log(f"!!! BLOCK FOUND BY WORKER {self.id} !!!")

    def mine(self):
        if not self.job:
            return

        max_extranonce2 = 1 << (8 * self.extranonce2_size)
        stride = max(1, max_extranonce2 // max(NUM_WORKERS, 1))
        start_extranonce2 = (self.id - 1) * stride
        end_extranonce2 = min(start_extranonce2 + stride, max_extranonce2)

        extranonce2 = start_extranonce2
        ntime = self.job["ntime"]
        share_target = self.target * int(self.difficulty)

        while not self.stop_event.is_set() and extranonce2 < end_extranonce2:
            en2_bytes = extranonce2.to_bytes(self.extranonce2_size, "little")
            coinbase = self.job["cb1"] + self.extranonce1 + en2_bytes + self.job["cb2"]
            merkle = merkle_root(coinbase, self.job["branches"])

            for nonce in range(0, 0x100000000):
                if self.stop_event.is_set():
                    return

                header = build_block_header(
                    self.job["version"],
                    self.job["prevhash"],
                    merkle,
                    ntime,
                    self.job["nbits"],
                    nonce
                )
                hash_result = sha256d(header)
                hash_int = int.from_bytes(hash_result[::-1], "big")

                self.hashes += 1
                with STATS_LOCK:
                    STATS["total_hashes"] += 1

                if hash_int < share_target:
                    self.submit_share(nonce, en2_bytes, ntime)
                    if hash_int < self.target:
                        log(f"!!! BLOCK CANDIDATE BY WORKER {self.id} !!!")

                if self.hashes % 100000 == 0:
                    elapsed = time.time() - self.start_time
                    hr = self.hashes / elapsed if elapsed > 0 else 0
                    if hr > 1000:
                        log(f"Worker {self.id}: {hr/1000:.1f} KH/s")

            extranonce2 += 1
            ntime += 1

    def run(self):
        backoff = 1
        while True:
            if not self.connect():
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1

            if not self.subscribe():
                self.sock.close()
                continue
            if not self.authorize():
                self.sock.close()
                continue

            log(f"Worker {self.id}: Connected")

            while True:
                msg = self.recv()
                if not msg:
                    break
                if "method" in msg:
                    if msg["method"] == "mining.notify":
                        self.handle_notify(msg["params"])
                    elif msg["method"] == "mining.set_difficulty":
                        self.handle_set_difficulty(msg["params"])
                elif msg.get("id") == 2 and not msg.get("result"):
                    break

            try:
                self.sock.close()
            except Exception:
                pass
            log(f"Worker {self.id}: Reconnecting...")

def monitor_stats():
    while True:
        time.sleep(10)
        elapsed = time.time() - GLOBAL_START
        if elapsed < 1:
            continue
        with STATS_LOCK:
            total_hr = STATS["total_hashes"] / elapsed
            STATS["num_workers"] = sum(1 for thr in threads if thr.is_alive())
            log(f"STATS: {total_hr/1000:.1f} KH/s | Shares: {STATS['shares_accepted']}/{STATS['shares_submitted']} | Workers: {STATS['num_workers']} | CPU: {psutil.cpu_percent()}%")

def get_optimal_workers() -> int:
    global NUM_WORKERS
    cores = os.cpu_count() or 1
    workers = max(1, int(cores * 0.95))
    workers = min(workers, MAX_WORKERS)
    ram_gb = psutil.virtual_memory().total / (1024**3)
    if ram_gb < 2:
        workers = min(workers, 2)
    NUM_WORKERS = workers
    log(f"Using {workers} workers on {cores} cores, {ram_gb:.1f} GB RAM")
    return workers

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>BTC Lottery Miner</title>
    <meta http-equiv="refresh" content="10">
    <style>
        body { font-family: Arial; background: #000; color: #0f0; padding: 20px; text-align: center; }
        .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; max-width: 600px; margin: 20px auto; }
        .stat { background: #111; padding: 15px; border: 1px solid #0f0; }
        .label { font-size: 12px; color: #aaa; }
        .value { font-size: 24px; color: #0f0; font-family: monospace; }
        .wallet { margin: 20px; font-size: 14px; word-break: break-all; }
    </style>
</head>
<body>
    <h1>BTC Lottery Miner</h1>
    <div class="wallet">Wallet: {{ wallet }}</div>
    <div class="stats">
        <div class="stat"><div class="label">Miners</div><div class="value" id="num">{{ num_workers }}</div></div>
        <div class="stat"><div class="label">Hashrate</div><div class="value" id="hr">{{ hashrate }} KH/s</div></div>
        <div class="stat"><div class="label">Shares OK</div><div class="value" id="ok">{{ shares_accepted }}</div></div>
        <div class="stat"><div class="label">Submitted</div><div class="value" id="sub">{{ shares_submitted }}</div></div>
        <div class="stat"><div class="label">Uptime</div><div class="value" id="up">{{ uptime }}</div></div>
        <div class="stat"><div class="label">CPU %</div><div class="value" id="cpu">{{ cpu }}</div></div>
    </div>
    <p><a href="https://web.public-pool.io" target="_blank" style="color:#0f0;">Pool Dashboard</a></p>
    <script>
        setInterval(() => {
            fetch('/api/stats').then(r => r.json()).then(d => {
                document.getElementById('num').textContent = d.num_workers;
                document.getElementById('hr').textContent = d.hashrate.toFixed(1);
                document.getElementById('ok').textContent = d.shares_accepted;
                document.getElementById('sub').textContent = d.shares_submitted;
                let h = Math.floor(d.uptime/3600), m = Math.floor((d.uptime%3600)/60);
                document.getElementById('up').textContent = h + 'h ' + m + 'm';
                document.getElementById('cpu').textContent = d.cpu.toFixed(0);
            });
        }, 10000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    import platform
    # No multiprocessing used, so no start_method necessary

    GLOBAL_START = time.time()
    num_workers = get_optimal_workers()

    # === DASHBOARD ===
    from flask import Flask, render_template_string
    app = Flask(__name__)

    @app.route('/')
    def dashboard():
        with STATS_LOCK:
            elapsed = time.time() - GLOBAL_START
            hashrate = STATS["total_hashes"] / elapsed / 1000 if elapsed > 0 else 0
            uptime = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
            cpu = psutil.cpu_percent()
        return render_template_string(HTML_TEMPLATE,
                                      wallet=BTC_WALLET,
                                      num_workers=STATS["num_workers"],
                                      hashrate=f"{hashrate:.1f}",
                                      shares_accepted=STATS["shares_accepted"],
                                      shares_submitted=STATS["shares_submitted"],
                                      uptime=uptime,
                                      cpu=f"{cpu:.0f}")

    @app.route('/api/stats')
    def api_stats():
        with STATS_LOCK:
            elapsed = time.time() - GLOBAL_START
            hashrate = STATS["total_hashes"] / elapsed / 1000 if elapsed > 0 else 0
            return {
                "num_workers": STATS["num_workers"],
                "hashrate": hashrate,
                "shares_accepted": STATS["shares_accepted"],
                "shares_submitted": STATS["shares_submitted"],
                "uptime": elapsed,
                "cpu": psutil.cpu_percent()
            }

    dash_thread = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=DASHBOARD_PORT, debug=False, use_reloader=False),
        daemon=True
    )
    dash_thread.start()
    log(f"Dashboard: http://localhost:{DASHBOARD_PORT}")

    # === MINERS ===
    threads = []
    for i in range(1, num_workers + 1):
        thr = MinerWorker(i)
        thr.start()
        threads.append(thr)
        time.sleep(0.2)

    stat_thread = threading.Thread(target=monitor_stats, daemon=True)
    stat_thread.start()

    try:
        while any(thr.is_alive() for thr in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log("Shutting down...")
        for thr in threads:
            thr.stop_event.set()
        for thr in threads:
            thr.join(timeout=2)
