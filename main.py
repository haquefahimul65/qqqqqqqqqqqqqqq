#!/usr/bin/env python3
"""
NerdMiner-Style Solo BTC Lottery Miner + Dashboard
- public-pool.io
- Wallet: bc1qxfd40euj7vsxvff2ens8l6ear52w3mu0rwts6a
- Dashboard: http://0.0.0.0:8080
- Fixed Stratum implementation based on NerdMiner
"""

import socket
import json
import hashlib
import time
import threading
import struct
from binascii import unhexlify, hexlify
from typing import Optional, Dict, Any, Set
import psutil
import platform

# ===================== CONFIG =====================
POOL_HOST = "public-pool.io"
POOL_PORT = 21496
BTC_WALLET = "bc1qxfd40euj7vsxvff2ens8l6ear52w3mu0rwts6a"
DASHBOARD_PORT = 8080
WORKER_PREFIX = platform.node()[:8]
HASH_BATCH_SIZE = 50000  # Check for new jobs every 50K hashes
# ================================================

# Global state
STATS = {
    "total_hashes": 0,
    "shares_submitted": 0,
    "shares_accepted": 0,
    "num_workers": 0,
    "current_hashrate": 0,
}
STATS_LOCK = threading.Lock()
GLOBAL_START = 0.0
worker_threads = []

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

class MinerWorker:
    def __init__(self, worker_id: int, num_workers: int):
        self.id = worker_id
        self.num_workers = num_workers
        self.sock: Optional[socket.socket] = None
        self.sock_lock = threading.Lock()
        self.extranonce1: bytes = b""
        self.extranonce2_size: int = 4
        self.job: Optional[Dict[str, Any]] = None
        self.job_lock = threading.Lock()
        self.difficulty: float = 1.0
        self.target: Optional[int] = None
        self.mining_thread: Optional[threading.Thread] = None
        self.receiver_thread: Optional[threading.Thread] = None
        self.stop_mining = threading.Event()
        self.shutdown = threading.Event()
        self.hashes = 0
        self.last_hash_time = time.time()
        self.current_hashrate = 0
        self.submitted_cache: Set[str] = set()
        self.recv_buffer = b""
        self.is_alive = False
        self.authorized = False
        self.subscribed = False
        self.msg_id = 3  # Start at 3 (1=subscribe, 2=authorize)

    def connect(self) -> bool:
        try:
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((POOL_HOST, POOL_PORT))
            self.sock.settimeout(60)  # 60s timeout for receive
            log(f"Worker {self.id}: TCP connected")
            return True
        except Exception as e:
            log(f"Worker {self.id}: Connection failed - {e}")
            return False

    def send(self, msg: dict) -> bool:
        try:
            with self.sock_lock:
                if self.sock:
                    data = json.dumps(msg) + "\n"
                    self.sock.sendall(data.encode())
                    return True
        except Exception as e:
            log(f"Worker {self.id}: Send error - {e}")
        return False

    def recv_line(self, timeout: float = 1.0) -> Optional[dict]:
        try:
            self.sock.settimeout(timeout)
            while b"\n" not in self.recv_buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return None
                self.recv_buffer += chunk
            line, self.recv_buffer = self.recv_buffer.split(b"\n", 1)
            if line.strip():
                return json.loads(line.decode())
        except socket.timeout:
            return None
        except Exception as e:
            log(f"Worker {self.id}: Recv error - {e}")
            return None

    def receiver_loop(self):
        """Continuously listen for pool messages"""
        while not self.shutdown.is_set():
            try:
                msg = self.recv_line(timeout=5.0)
                if msg is None:
                    continue

                # Handle methods from pool
                if "method" in msg:
                    if msg["method"] == "mining.notify":
                        self.handle_notify(msg["params"])
                    elif msg["method"] == "mining.set_difficulty":
                        self.handle_set_difficulty(msg["params"])
                    elif msg["method"] == "client.reconnect":
                        log(f"Worker {self.id}: Pool requested reconnect")
                        break

                # Handle responses
                elif "id" in msg and "result" in msg:
                    msg_id = msg.get("id")
                    result = msg.get("result")
                    error = msg.get("error")

                    if msg_id == 1:  # subscribe response
                        if result:
                            self.extranonce1 = unhexlify(result[1])
                            self.extranonce2_size = result[2]
                            self.subscribed = True
                            log(f"Worker {self.id}: Subscribed (extranonce1={hexlify(self.extranonce1).decode()})")
                    elif msg_id == 2:  # authorize response
                        if result is True:
                            self.authorized = True
                            log(f"Worker {self.id}: ✓ Authorized")
                        else:
                            log(f"Worker {self.id}: ✗ Authorization FAILED - {error}")
                    else:  # share submit response
                        if result is True:
                            with STATS_LOCK:
                                STATS["shares_accepted"] += 1
                            log(f"Worker {self.id}: Share ✓ ACCEPTED (D={self.difficulty:.0f})")
                            if self.difficulty >= 1000000:
                                log(f"🎉🎉🎉 BLOCK FOUND BY WORKER {self.id}! 🎉🎉🎉")
                        else:
                            log(f"Worker {self.id}: Share ✗ REJECTED - {error}")

            except Exception as e:
                if not self.shutdown.is_set():
                    log(f"Worker {self.id}: Receiver error - {e}")
                break

        self.is_alive = False

    def subscribe(self) -> bool:
        """Subscribe to mining"""
        self.send({"id": 1, "method": "mining.subscribe", "params": ["NerdMinerPy/1.0"]})
        
        # Wait for subscribe response
        for _ in range(50):  # 5 second timeout
            if self.subscribed:
                return True
            time.sleep(0.1)
        
        log(f"Worker {self.id}: Subscribe timeout")
        return False

    def authorize(self) -> bool:
        """Authorize with wallet address"""
        worker_name = f"{BTC_WALLET}.{WORKER_PREFIX}-{self.id}"
        self.send({"id": 2, "method": "mining.authorize", "params": [worker_name, "x"]})
        
        # Wait for authorization
        for _ in range(50):  # 5 second timeout
            if self.authorized:
                return True
            time.sleep(0.1)
        
        log(f"Worker {self.id}: Authorization timeout")
        return False

    def handle_notify(self, params: list):
        """Handle new job from pool"""
        try:
            job_id, prevhash, cb1, cb2, branches, version, nbits, ntime, clean = params
            
            new_job = {
                "id": job_id,
                "prevhash": prevhash,
                "cb1": unhexlify(cb1),
                "cb2": unhexlify(cb2),
                "branches": branches,
                "version": int(version, 16),
                "nbits": int(nbits, 16),
                "ntime": int(ntime, 16),
                "clean": clean,
            }

            with self.job_lock:
                self.job = new_job
                self.target = bits_to_target(self.job["nbits"])

            log(f"Worker {self.id}: New job {job_id[:8]}... (clean={clean})")

            # If clean job, restart mining
            if clean:
                self.stop_mining.set()
                if self.mining_thread and self.mining_thread.is_alive():
                    self.mining_thread.join(timeout=0.5)
                self.stop_mining.clear()
                self.submitted_cache.clear()
                self.mining_thread = threading.Thread(target=self.mine, daemon=True)
                self.mining_thread.start()
            # If not mining yet, start
            elif not self.mining_thread or not self.mining_thread.is_alive():
                self.stop_mining.clear()
                self.mining_thread = threading.Thread(target=self.mine, daemon=True)
                self.mining_thread.start()

        except Exception as e:
            log(f"Worker {self.id}: Notify error - {e}")

    def handle_set_difficulty(self, params: list):
        """Handle difficulty change from pool"""
        try:
            self.difficulty = float(params[0]) if params else 1.0
            log(f"Worker {self.id}: Difficulty → {self.difficulty:.0f}")
        except:
            pass

    def submit_share(self, nonce: int, extranonce2: bytes, ntime: int, job_id: str):
        """Submit valid share to pool"""
        # Deduplicate
        key = f"{job_id}-{ntime:08x}-{nonce:08x}"
        if key in self.submitted_cache:
            return
        self.submitted_cache.add(key)
        if len(self.submitted_cache) > 1000:
            self.submitted_cache = set(list(self.submitted_cache)[-500:])

        worker_name = f"{BTC_WALLET}.{WORKER_PREFIX}-{self.id}"
        params = [
            worker_name,
            job_id,
            hexlify(extranonce2).decode(),
            f"{ntime:08x}",
            f"{nonce:08x}",
        ]

        self.msg_id += 1
        self.send({"id": self.msg_id, "method": "mining.submit", "params": params})
        
        with STATS_LOCK:
            STATS["shares_submitted"] += 1

    def mine(self):
        """Main mining loop"""
        with self.job_lock:
            if not self.job:
                return
            job = self.job.copy()
            target = self.target

        # Calculate this worker's nonce range
        nonce_start = (self.id - 1) * (0x100000000 // self.num_workers)
        nonce_end = self.id * (0x100000000 // self.num_workers)
        
        # Calculate extranonce2 range
        max_extranonce2 = 1 << (8 * self.extranonce2_size)
        extranonce2_stride = max(1, max_extranonce2 // self.num_workers)
        extranonce2_start = (self.id - 1) * extranonce2_stride
        extranonce2_end = min(extranonce2_start + extranonce2_stride, max_extranonce2)

        share_target = target * int(self.difficulty)
        extranonce2 = extranonce2_start
        ntime = job["ntime"]
        batch_start = time.time()
        batch_hashes = 0

        log(f"Worker {self.id}: Mining job {job['id'][:8]}... nonces={nonce_start:08x}-{nonce_end:08x}")

        while not self.stop_mining.is_set() and not self.shutdown.is_set():
            # Build coinbase
            en2_bytes = extranonce2.to_bytes(self.extranonce2_size, "little")
            coinbase = job["cb1"] + self.extranonce1 + en2_bytes + job["cb2"]
            merkle = merkle_root(coinbase, job["branches"])

            # Mine nonce range in batches
            for nonce in range(nonce_start, nonce_end, HASH_BATCH_SIZE):
                if self.stop_mining.is_set() or self.shutdown.is_set():
                    return

                # Hash batch
                batch_end = min(nonce + HASH_BATCH_SIZE, nonce_end)
                for n in range(nonce, batch_end):
                    header = build_block_header(
                        job["version"], job["prevhash"], merkle, ntime, job["nbits"], n
                    )
                    hash_result = sha256d(header)
                    hash_int = int.from_bytes(hash_result[::-1], "big")

                    self.hashes += 1
                    batch_hashes += 1
                    with STATS_LOCK:
                        STATS["total_hashes"] += 1

                    # Check if valid share
                    if hash_int < share_target:
                        self.submit_share(n, en2_bytes, ntime, job["id"])
                        # Check if valid block
                        if hash_int < target:
                            log(f"🚀 BLOCK CANDIDATE BY WORKER {self.id}! 🚀")

                # Update hashrate every batch
                now = time.time()
                elapsed = now - batch_start
                if elapsed > 0:
                    self.current_hashrate = batch_hashes / elapsed
                    with STATS_LOCK:
                        STATS["current_hashrate"] = sum(
                            getattr(t, "worker", None).current_hashrate
                            for t in worker_threads
                            if hasattr(t, "worker") and getattr(t, "worker", None)
                        )

            # Move to next extranonce2/ntime
            extranonce2 += 1
            if extranonce2 >= extranonce2_end:
                extranonce2 = extranonce2_start
                ntime += 1

    def run(self):
        """Main worker loop"""
        backoff = 1
        while not self.shutdown.is_set():
            self.is_alive = False
            self.authorized = False
            self.subscribed = False
            self.recv_buffer = b""

            # Connect
            if not self.connect():
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1

            # Subscribe
            if not self.subscribe():
                self.sock.close()
                time.sleep(2)
                continue

            # Start receiver thread
            self.receiver_thread = threading.Thread(target=self.receiver_loop, daemon=True)
            self.receiver_thread.start()

            # Authorize
            if not self.authorize():
                self.sock.close()
                time.sleep(2)
                continue

            self.is_alive = True
            log(f"Worker {self.id}: ✓ Ready to mine")

            # Keep alive
            while not self.shutdown.is_set() and self.is_alive:
                time.sleep(1)

            # Cleanup
            try:
                self.sock.close()
            except:
                pass
            
            if not self.shutdown.is_set():
                log(f"Worker {self.id}: Reconnecting...")
                time.sleep(3)

def monitor_stats():
    """Print stats periodically"""
    while True:
        time.sleep(15)
        elapsed = time.time() - GLOBAL_START
        if elapsed < 1:
            continue
        
        with STATS_LOCK:
            avg_hr = STATS["total_hashes"] / elapsed / 1000
            curr_hr = STATS["current_hashrate"] / 1000
            workers = sum(
                1 for t in worker_threads 
                if hasattr(t, "worker") and getattr(t, "worker", None) and t.worker.is_alive
            )
            STATS["num_workers"] = workers
            
            log(
                f"STATS: {curr_hr:.1f} KH/s (avg {avg_hr:.1f}) | "
                f"Shares: {STATS['shares_accepted']}/{STATS['shares_submitted']} | "
                f"Workers: {workers} | CPU: {psutil.cpu_percent():.0f}%"
            )

def get_optimal_workers() -> int:
    """Calculate optimal number of workers"""
    try:
        cores = psutil.cpu_count(logical=True) or 4
        ram_gb = psutil.virtual_memory().total / (1024**3)
        
        # Use 75% of cores for mining
        workers = max(1, int(cores * 0.75))
        
        # Limit by RAM (1 worker per 0.5GB)
        max_by_ram = max(1, int(ram_gb * 2))
        workers = min(workers, max_by_ram)
        
        log(f"Using {workers} workers on {cores} cores, {ram_gb:.1f} GB RAM")
        return workers
    except:
        return 4

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>BTC Lottery Miner</title>
    <meta http-equiv="refresh" content="15">
    <style>
        body { font-family: 'Courier New', monospace; background: #000; color: #0f0; padding: 20px; text-align: center; }
        h1 { text-shadow: 0 0 10px #0f0; }
        .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; max-width: 700px; margin: 20px auto; }
        .stat { background: #111; padding: 20px; border: 2px solid #0f0; border-radius: 5px; box-shadow: 0 0 10px #0f040; }
        .label { font-size: 12px; color: #0a0; text-transform: uppercase; }
        .value { font-size: 28px; color: #0f0; margin-top: 5px; }
        .wallet { margin: 20px; font-size: 13px; word-break: break-all; color: #0a0; }
        a { color: #0f0; text-decoration: none; text-shadow: 0 0 5px #0f0; }
        a:hover { color: #fff; }
    </style>
</head>
<body>
    <h1>⛏️ BTC LOTTERY MINER ⛏️</h1>
    <div class="wallet">{{ wallet }}</div>
    <div class="stats">
        <div class="stat"><div class="label">Active Miners</div><div class="value" id="num">{{ num_workers }}</div></div>
        <div class="stat"><div class="label">Hashrate</div><div class="value" id="hr">{{ hashrate }} KH/s</div></div>
        <div class="stat"><div class="label">Shares Accepted</div><div class="value" id="ok">{{ shares_accepted }}</div></div>
        <div class="stat"><div class="label">Total Submitted</div><div class="value" id="sub">{{ shares_submitted }}</div></div>
        <div class="stat"><div class="label">Uptime</div><div class="value" id="up">{{ uptime }}</div></div>
        <div class="stat"><div class="label">CPU Usage</div><div class="value" id="cpu">{{ cpu }}%</div></div>
    </div>
    <p><a href="https://web.public-pool.io/#/app/{{ wallet }}" target="_blank">📊 View Pool Dashboard</a></p>
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
        }, 15000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    GLOBAL_START = time.time()
    num_workers = get_optimal_workers()

    # === DASHBOARD ===
    from flask import Flask, render_template_string, jsonify
    
    app = Flask(__name__)
    app.logger.disabled = True

    @app.route("/")
    def dashboard():
        with STATS_LOCK:
            elapsed = time.time() - GLOBAL_START
            hashrate = STATS["current_hashrate"] / 1000 if STATS["current_hashrate"] > 0 else 0
            uptime = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
        return render_template_string(
            HTML_TEMPLATE,
            wallet=BTC_WALLET,
            num_workers=STATS["num_workers"],
            hashrate=f"{hashrate:.1f}",
            shares_accepted=STATS["shares_accepted"],
            shares_submitted=STATS["shares_submitted"],
            uptime=uptime,
            cpu=f"{psutil.cpu_percent():.0f}",
        )

    @app.route("/api/stats")
    def api_stats():
        with STATS_LOCK:
            elapsed = time.time() - GLOBAL_START
            hashrate = STATS["current_hashrate"] / 1000
            return jsonify(
                {
                    "num_workers": STATS["num_workers"],
                    "hashrate": hashrate,
                    "shares_accepted": STATS["shares_accepted"],
                    "shares_submitted": STATS["shares_submitted"],
                    "uptime": elapsed,
                    "cpu": psutil.cpu_percent(),
                }
            )

    dash_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False),
        daemon=True,
    )
    dash_thread.start()
    log(f"Dashboard: http://localhost:{DASHBOARD_PORT}")

    # === START MINERS ===
    for i in range(1, num_workers + 1):
        worker = MinerWorker(i, num_workers)
        t = threading.Thread(target=worker.run, daemon=True)
        t.worker = worker
        t.start()
        worker_threads.append(t)
        time.sleep(0.3)

    stat_thread = threading.Thread(target=monitor_stats, daemon=True)
    stat_thread.start()

    try:
        log("Mining started! Press Ctrl+C to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("\nShutting down...")
        for t in worker_threads:
            if hasattr(t, "worker"):
                t.worker.shutdown.set()
        time.sleep(2)
        log("Goodbye!")
