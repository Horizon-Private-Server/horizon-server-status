#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Message format:
# For UDP:
#   [1 byte password_len][password bytes][8-byte unsigned big-endian timestamp_ms]
#
# For TCP:
#   [2-byte big-endian msg_len][rest as above]


DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "HorizonMetricsBot/1.0 (+https://horizon)"

# AWS EC2 regional endpoints (root URL, no /ping)
# Docs: https://docs.aws.amazon.com/ec2/latest/devguide/ec2-endpoints.html
REGION_ENDPOINTS = {
    # US
    "us-east-1 (N. Virginia)": "https://ec2.us-east-1.amazonaws.com",
    "us-east-2 (Ohio)": "https://ec2.us-east-2.amazonaws.com",
    "us-west-1 (N. California)": "https://ec2.us-west-1.amazonaws.com",
    "us-west-2 (Oregon)": "https://ec2.us-west-2.amazonaws.com",
    # Europe
    "eu-west-1 (Ireland)": "https://ec2.eu-west-1.amazonaws.com",
    "eu-central-1 (Frankfurt)": "https://ec2.eu-central-1.amazonaws.com",
    "eu-north-1 (Stockholm)": "https://ec2.eu-north-1.amazonaws.com",
}

REGION_PING_INTERVAL_SECONDS = 5
TCP_TIMEOUT_SECONDS = 3
METRICS_PREFIX = b"METRICS|"


@dataclass
class RttStats:
    tcp_ms: Optional[int] = None
    udp_ms: Optional[int] = None
    updated_at: Optional[datetime] = None
    tcp_history: List[Tuple[int, int]] = None
    udp_history: List[Tuple[int, int]] = None

    def __post_init__(self):
        self.tcp_history = []
        self.udp_history = []

    def update_tcp(self, rtt_ms: int):
        now = datetime.now(timezone.utc)
        now_ts_ms = int(now.timestamp() * 1000)
        self.tcp_ms = rtt_ms
        self.updated_at = now
        self._record(self.tcp_history, now_ts_ms, rtt_ms)

    def update_udp(self, rtt_ms: int):
        now = datetime.now(timezone.utc)
        now_ts_ms = int(now.timestamp() * 1000)
        self.udp_ms = rtt_ms
        self.updated_at = now
        self._record(self.udp_history, now_ts_ms, rtt_ms)

    def _record(self, history: List[Tuple[int, int]], now_ts_ms: int, rtt_ms: int):
        history.append((now_ts_ms, rtt_ms))
        cutoff = now_ts_ms - 30 * 60 * 1000  # keep last 30 minutes for 30m min/max
        while history and history[0][0] < cutoff:
            history.pop(0)

    def _window_stats(self, history: List[Tuple[int, int]], now_ts_ms: int, window_ms: int):
        cutoff = now_ts_ms - window_ms
        values = [r for ts, r in history if ts >= cutoff]
        if not values:
            return None
        return {
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def _window_minmax(self, history: List[Tuple[int, int]], now_ts_ms: int, window_ms: int):
        cutoff = now_ts_ms - window_ms
        values = [r for ts, r in history if ts >= cutoff]
        if not values:
            return None
        return {"min": min(values), "max": max(values)}

    def snapshot(self) -> Dict[str, Optional[str]]:
        """Return a shallow copy suitable for rendering."""
        now_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return {
            "tcp_ms": self.tcp_ms,
            "udp_ms": self.udp_ms,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "tcp_5m": self._window_stats(self.tcp_history, now_ts_ms, 5 * 60 * 1000),
            "tcp_30m": self._window_minmax(self.tcp_history, now_ts_ms, 30 * 60 * 1000),
            "udp_5m": self._window_stats(self.udp_history, now_ts_ms, 5 * 60 * 1000),
            "udp_30m": self._window_minmax(self.udp_history, now_ts_ms, 30 * 60 * 1000),
        }


class RegionLatencyState:
    """Stores the latest server-side AWS latency results plus short-term windows."""

    def __init__(self):
        self.latest_raw: Optional[Dict[str, Any]] = None
        # region -> list of (ts_ms, tcp_ms)
        self.history: Dict[str, List[Tuple[int, Optional[float]]]] = {}

    def _prune(self, history: List[Tuple[int, Optional[float]]], now_ms: int):
        cutoff = now_ms - 30 * 60 * 1000  # keep last 30 minutes
        while history and history[0][0] < cutoff:
            history.pop(0)

    def _window_stats(self, values: List[float]) -> Optional[Dict[str, float]]:
        if not values:
            return None
        return {
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def _window_minmax(self, values: List[float]) -> Optional[Dict[str, float]]:
        if not values:
            return None
        return {"min": min(values), "max": max(values)}

    def update(self, payload: Dict[str, Any]) -> None:
        ts_ms = payload.get("timestamp_ms") or int(time.time() * 1000)
        self.latest_raw = payload
        for entry in payload.get("regions", []):
            region = entry.get("region", "unknown")
            tcp_ms = entry.get("tcp_ms")
            hist = self.history.setdefault(region, [])
            hist.append((ts_ms, tcp_ms))
            self._prune(hist, ts_ms)

    def snapshot(self) -> Optional[Dict[str, Any]]:
        if self.latest_raw is None:
            return None

        now_ms = int(time.time() * 1000)
        latest_ts = self.latest_raw.get("timestamp_ms")
        latest_regions = {r.get("region", "unknown"): r for r in self.latest_raw.get("regions", [])}
        all_regions: List[str] = list(latest_regions.keys())
        for region in self.history.keys():
            if region not in latest_regions:
                all_regions.append(region)

        regions_snapshot: List[Dict[str, Any]] = []
        for region in all_regions:
            hist = self.history.get(region, [])
            self._prune(hist, now_ms)
            tcp_vals_5m = [v for ts, v in hist if ts >= now_ms - 5 * 60 * 1000 and v is not None]
            tcp_vals_30m = [v for ts, v in hist if ts >= now_ms - 30 * 60 * 1000 and v is not None]

            latest_entry = latest_regions.get(region, {})
            regions_snapshot.append({
                "region": region,
                "tcp_ms": latest_entry.get("tcp_ms"),
                "tcp_5m": self._window_stats(tcp_vals_5m),
                "tcp_30m": self._window_minmax(tcp_vals_30m),
            })

        return {
            "timestamp_ms": latest_ts,
            "regions": regions_snapshot,
        }


class DiscordBotClient:
    def __init__(self, token: str, channel_id: str, message_id: Optional[str]):
        self.token = token
        self.channel_id = channel_id
        self.message_id = message_id
        self.opener = urllib.request.build_opener()
        self.bot_user: Optional[str] = None

    def _make_request(self, method: str, url: str, payload: Optional[Dict[str, str]] = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bot {self.token}")
        req.add_header("User-Agent", USER_AGENT)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=10) as resp:
            body = resp.read()
            if not body:
                return {}
            return json.loads(body.decode("utf-8"))

    async def initialize(self) -> bool:
        """Validate token and fetch bot user info."""
        url = f"{DISCORD_API_BASE}/users/@me"

        def _init():
            try:
                data = self._make_request("GET", url)
                self.bot_user = data.get("id")
                username = data.get("username")
                if self.bot_user:
                    print(f"[DISCORD] Bot authenticated as {username} (id={self.bot_user})")
                else:
                    print("[DISCORD] Bot authentication response missing id")
                return True
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                print(f"[DISCORD] Failed to authenticate bot: {e} body={body}")
            except Exception as e:
                print(f"[DISCORD] Unexpected error during Discord init: {e}")
            return False

        return await asyncio.to_thread(_init)

    async def post_message(self, content: str) -> Optional[str]:
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        payload = {"content": content}

        def _send():
            try:
                data = self._make_request("POST", url, payload)
                return data.get("id")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                print(f"[DISCORD] Failed to post message: {e} body={body}")
            except Exception as e:
                print(f"[DISCORD] Unexpected error posting message: {e}")
            return None

        return await asyncio.to_thread(_send)

    async def edit_message(self, message_id: str, content: str) -> bool:
        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages/{message_id}"
        payload = {"content": content}

        def _send():
            try:
                self._make_request("PATCH", url, payload)
                return True
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                print(f"[DISCORD] Failed to edit message {message_id}: {e} body={body}")
            except Exception as e:
                print(f"[DISCORD] Unexpected error editing message {message_id}: {e}")
            return False

        return await asyncio.to_thread(_send)


def build_payload(password: str) -> bytes:
    """Build a payload with password + UTC timestamp in ms."""
    pwd_bytes = password.encode("utf-8")
    if len(pwd_bytes) > 255:
        raise ValueError("Password too long (max 255 bytes)")
    pwd_len = len(pwd_bytes)
    # UTC timestamp in ms
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return struct.pack("!B", pwd_len) + pwd_bytes + struct.pack("!Q", ts_ms)


def parse_payload(data: bytes, expected_password: str):
    """Parse payload and verify password. Returns (password, ts_ms) or (None, None) on failure."""
    if not data:
        return None, None
    # Need at least 1 + 8 bytes
    if len(data) < 1 + 8:
        return None, None
    pwd_len = data[0]
    if len(data) < 1 + pwd_len + 8:
        return None, None
    pwd_bytes = data[1:1 + pwd_len]
    ts_bytes = data[1 + pwd_len:1 + pwd_len + 8]
    try:
        password = pwd_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, None
    if password != expected_password:
        return None, None
    ts_ms = struct.unpack("!Q", ts_bytes)[0]
    return password, ts_ms


# ----------------- SERVER SIDE -----------------


class RegionMetricsCache:
    """Stores the most recent AWS latency frame to piggyback on TCP echoes."""

    def __init__(self):
        self.latest_frame: Optional[bytes] = None

    def update(self, frame: bytes) -> None:
        self.latest_frame = frame

    def get(self) -> Optional[bytes]:
        return self.latest_frame


async def measure_tcp_latency(host: str, port: int = 443) -> Optional[float]:
    start = time.perf_counter()
    try:
        conn = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(conn, timeout=TCP_TIMEOUT_SECONDS)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        writer.close()
        await writer.wait_closed()
        return elapsed_ms
    except Exception:
        return None


async def measure_region_latency(region: str, url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        return {"region": region, "tcp_ms": None}

    tcp_ms = await measure_tcp_latency(host)
    return {"region": region, "tcp_ms": tcp_ms}


async def collect_region_latencies() -> Dict[str, Any]:
    tasks = [
        measure_region_latency(region, url)
        for region, url in REGION_ENDPOINTS.items()
    ]
    results = await asyncio.gather(*tasks)
    return {
        "type": "region_latency",
        "timestamp_ms": int(time.time() * 1000),
        "regions": results,
    }


def build_region_metrics_message(payload: Dict[str, Any]) -> bytes:
    body = METRICS_PREFIX + json.dumps(payload).encode("utf-8")
    if len(body) > 65535:
        raise ValueError("Region metrics payload too large to frame")
    header = struct.pack("!H", len(body))
    return header + body


async def region_latency_loop(cache: RegionMetricsCache) -> None:
    while True:
        try:
            payload = await collect_region_latencies()
            framed = build_region_metrics_message(payload)
            cache.update(framed)
        except Exception as e:
            print(f"[SERVER] Region latency loop error: {e}")
        await asyncio.sleep(REGION_PING_INTERVAL_SECONDS)


class UdpServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, password: str):
        self.password = password
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        sockname = transport.get_extra_info("sockname")
        print(f"[SERVER] UDP listening on {sockname}")

    def datagram_received(self, data, addr):
        _, ts_ms = parse_payload(data, self.password)
        if ts_ms is None:
            # Wrong password or malformed -> ignore
            return
        # Echo back the same data
        self.transport.sendto(data, addr)


async def handle_tcp_client(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter,
                            password: str,
                            metrics_cache: RegionMetricsCache):
    addr = writer.get_extra_info("peername")
    print(f"[SERVER] TCP client connected: {addr}")
    try:
        while True:
            # Read length prefix
            header = await reader.readexactly(2)
            (msg_len,) = struct.unpack("!H", header)
            body = await reader.readexactly(msg_len)
            _, ts_ms = parse_payload(body, password)
            if ts_ms is None:
                print(f"[SERVER] TCP: invalid/wrong password from {addr}, closing")
                writer.close()
                await writer.wait_closed()
                return
            # Echo back the same frame, plus piggyback latest region metrics if available
            writer.write(header + body)
            latest_metrics = metrics_cache.get()
            if latest_metrics:
                writer.write(latest_metrics)
            await writer.drain()
    except asyncio.IncompleteReadError:
        # Client closed connection
        pass
    except Exception as e:
        print(f"[SERVER] Error with TCP client {addr}: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        print(f"[SERVER] TCP client disconnected: {addr}")


async def run_server(tcp_port: int, udp_port: int, password: str):
    metrics_cache = RegionMetricsCache()

    loop = asyncio.get_running_loop()

    # UDP server
    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: UdpServerProtocol(password),
        local_addr=("0.0.0.0", udp_port),
    )

    # TCP server
    tcp_server = await asyncio.start_server(
        lambda r, w: handle_tcp_client(r, w, password, metrics_cache),
        host="0.0.0.0",
        port=tcp_port,
    )
    addrs = ", ".join(str(sock.getsockname()) for sock in tcp_server.sockets)
    print(f"[SERVER] TCP listening on {addrs}")

    region_task = asyncio.create_task(region_latency_loop(metrics_cache))

    async with tcp_server:
        try:
            await tcp_server.serve_forever()
        finally:
            region_task.cancel()
            try:
                await region_task
            except asyncio.CancelledError:
                pass
            udp_transport.close()


# ----------------- CLIENT SIDE -----------------


class UdpClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, password: str, stats: RttStats):
        self.password = password
        self.transport = None
        self.stats = stats

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Got echo back; compute RTT from embedded timestamp
        _, ts_ms = parse_payload(data, self.password)
        if ts_ms is None:
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        rtt_ms = now_ms - ts_ms
        #(f"UDP RTT: {rtt_ms} ms")
        self.stats.update_udp(rtt_ms)


async def udp_client_task(host: str, port: int, delay_ms: int, password: str, stats: RttStats):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UdpClientProtocol(password, stats),
        remote_addr=(host, port),
    )

    try:
        while True:
            payload = build_payload(password)
            transport.sendto(payload)
            await asyncio.sleep(delay_ms / 1000.0)
    finally:
        transport.close()


async def tcp_client_task(
    host: str,
    port: int,
    delay_ms: int,
    password: str,
    stats: RttStats,
    region_metrics: RegionLatencyState,
):
    reader, writer = await asyncio.open_connection(host, port)
    addr = writer.get_extra_info("peername")
    print(f"[CLIENT] Connected to TCP server at {addr}")

    async def sender():
        try:
            while True:
                body = build_payload(password)
                msg_len = len(body)
                if msg_len > 65535:
                    raise ValueError("Message too long")
                header = struct.pack("!H", msg_len)
                writer.write(header + body)
                await writer.drain()
                await asyncio.sleep(delay_ms / 1000.0)
        except asyncio.CancelledError:
            pass

    async def receiver():
        try:
            while True:
                header = await reader.readexactly(2)
                (msg_len,) = struct.unpack("!H", header)
                body = await reader.readexactly(msg_len)
                if body.startswith(METRICS_PREFIX):
                    try:
                        payload = json.loads(body[len(METRICS_PREFIX):].decode("utf-8"))
                        region_metrics.update(payload)
                    except Exception as e:
                        print(f"[CLIENT] Failed to parse metrics payload: {e}")
                    continue

                _, ts_ms = parse_payload(body, password)
                if ts_ms is None:
                    continue
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                rtt_ms = now_ms - ts_ms
                #print(f"TCP RTT: {rtt_ms} ms")
                stats.update_tcp(rtt_ms)
        except asyncio.IncompleteReadError:
            print("[CLIENT] TCP connection closed by server")
        except asyncio.CancelledError:
            pass

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())
    try:
        await asyncio.gather(send_task, recv_task)
    finally:
        send_task.cancel()
        recv_task.cancel()
        writer.close()
        await writer.wait_closed()


def _format_region_section(region_snapshot: Optional[Dict[str, Any]]) -> str:
    if not region_snapshot or not region_snapshot.get("regions"):
        return "**AWS latency (server ➜ AWS)**\n```\nPending first sample\n```\n"

    def fmt_ms(value: Optional[float]) -> str:
        if value is None:
            return "err"
        return f"{int(round(value))}ms"

    def fmt_triplet(window: Optional[Dict[str, float]]) -> str:
        if not window:
            return "n/a"
        return f"{int(round(window['avg']))}/{int(round(window['min']))}/{int(round(window['max']))}"

    def fmt_minmax(window: Optional[Dict[str, float]]) -> str:
        if not window:
            return "n/a"
        return f"{int(round(window['min']))}/{int(round(window['max']))}"

    ts_ms = region_snapshot.get("timestamp_ms")
    if ts_ms:
        try:
            updated_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            ts = int(updated_dt.timestamp())
            updated_str = f"<t:{ts}:F> (<t:{ts}:R>)"
        except Exception:
            updated_str = str(ts_ms)
    else:
        updated_str = "pending"

    header = f"{'Region':<26} {'Latest':>8} {'5m a/m/x':>12} {'30m min/max':>14}"
    rows: List[str] = []
    for entry in region_snapshot.get("regions", []):
        region = entry.get("region", "unknown")
        latest_tcp = fmt_ms(entry.get("tcp_ms"))
        window_5m = fmt_triplet(entry.get("tcp_5m"))
        window_30m = fmt_minmax(entry.get("tcp_30m"))
        rows.append(
            f"{region:<26} {latest_tcp:>8} {window_5m:>12} {window_30m:>14}"
        )

    table = "\n".join([header, "-" * len(header), *rows]) if rows else header

    return (
        "**AWS latency (server ➜ AWS)**\n"
        f"```\n{table}\n```\n"
        f"*Server-updated:* {updated_str}\n"
    )


def build_discord_content(
    snapshot: Dict[str, Optional[str]],
    region_snapshot: Optional[Dict[str, Any]],
) -> str:
    def fmt_latency(val: Optional[int]) -> str:
        return f"{val} ms" if val is not None else "n/a"

    def fmt_window(data: Optional[Dict[str, float]]) -> str:
        if not data:
            return "n/a"
        avg = int(round(data["avg"]))
        return f"{avg} ms (min {int(round(data['min']))}, max {int(round(data['max']))})"

    def fmt_minmax(data: Optional[Dict[str, float]]) -> str:
        if not data:
            return "n/a"
        return f"min {int(round(data['min']))}, max {int(round(data['max']))}"

    def format_block(title: str, latest: Optional[int], win5: Optional[Dict[str, float]], win30: Optional[Dict[str, float]]) -> str:
        lines = [
            f"{title}",
            f"{'Latest':<10}{fmt_latency(latest)}",
            f"{'5m avg':<10}{fmt_window(win5)}",
            f"{'30m':<10}{fmt_minmax(win30)}",
        ]
        return "```\n" + "\n".join(lines) + "\n```"

    updated = snapshot["updated_at"]
    if updated:
        try:
            updated_dt = datetime.fromisoformat(updated)
            ts = int(updated_dt.timestamp())
            updated_str = f"<t:{ts}:F> (<t:{ts}:R>)"
        except Exception:
            updated_str = updated
    else:
        updated_str = "pending"

    tcp_block = format_block("TCP (client ↔ server)", snapshot["tcp_ms"], snapshot.get("tcp_5m"), snapshot.get("tcp_30m"))
    udp_block = format_block("UDP (client ↔ server)", snapshot["udp_ms"], snapshot.get("udp_5m"), snapshot.get("udp_30m"))
    region_section = _format_region_section(region_snapshot)

    return (
        "**Server Status (ping from FourBolt's place)**\n"
        f"{tcp_block}\n"
        f"{udp_block}\n"
        f"*Updated:* {updated_str}\n"
        f"{region_section}"
    )


async def discord_status_task(
    stats: RttStats,
    region_metrics: RegionLatencyState,
    bot: DiscordBotClient,
    interval_seconds: int = 5,
):
    if not await bot.initialize():
        print("[DISCORD] Skipping status updates due to failed bot initialization")
        return

    current_message_id = bot.message_id or None
    posted_once = current_message_id is not None
    edit_failure_logged = False

    while True:
        snapshot = stats.snapshot()
        region_snapshot = region_metrics.snapshot()
        content = build_discord_content(snapshot, region_snapshot)

        if not posted_once:
            new_id = await bot.post_message(content)
            if new_id:
                current_message_id = new_id
                bot.message_id = new_id
                posted_once = True
                print(f"[DISCORD] Posted status message id={new_id}")
            else:
                print("[DISCORD] Failed to post initial status message; retrying...")
            await asyncio.sleep(interval_seconds)
            continue

        if not current_message_id:
            print("[DISCORD] No message id available to edit; skipping update")
            await asyncio.sleep(interval_seconds)
            continue

        success = await bot.edit_message(current_message_id, content)
        if not success and not edit_failure_logged:
            print("[DISCORD] Failed to edit status message; will keep retrying the same message id")
            edit_failure_logged = True
        if success and edit_failure_logged:
            # Reset so we log again if future edits start failing after a recovery
            edit_failure_logged = False
        await asyncio.sleep(interval_seconds)


async def run_client(
    host: str,
    tcp_port: int,
    udp_port: int,
    delay_ms: int,
    password: str,
    discord_token: Optional[str],
    discord_channel_id: Optional[str],
    discord_message_id: Optional[str],
):
    stats = RttStats()
    region_metrics = RegionLatencyState()
    tasks = [
        udp_client_task(host, udp_port, delay_ms, password, stats),
        tcp_client_task(host, tcp_port, delay_ms, password, stats, region_metrics),
    ]

    if discord_token and discord_channel_id:
        bot = DiscordBotClient(discord_token, discord_channel_id, discord_message_id)
        tasks.append(
            discord_status_task(
                stats,
                region_metrics,
                bot,
            )
        )
    else:
        print("[DISCORD] Skipping status updates (token or channel ID missing)")

    # Run both TCP and UDP clients concurrently
    await asyncio.gather(*tasks)


# ----------------- ENTRY POINT -----------------


def main():
    parser = argparse.ArgumentParser(
        description="Async TCP/UDP echo client/server with password and RTT measurement."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Server mode
    server_parser = subparsers.add_parser("server", help="Run in server mode")
    server_parser.add_argument("--tcp-port", type=int, required=True, help="TCP port to listen on")
    server_parser.add_argument("--udp-port", type=int, required=True, help="UDP port to listen on")
    server_parser.add_argument("--password", type=str, required=True, help="Shared password")

    # Client mode
    client_parser = subparsers.add_parser("client", help="Run in client mode")
    client_parser.add_argument("--host", type=str, required=True, help="Server hostname or IP")
    client_parser.add_argument("--tcp-port", type=int, required=True, help="Server TCP port")
    client_parser.add_argument("--udp-port", type=int, required=True, help="Server UDP port")
    client_parser.add_argument("--delay-ms", type=int, required=True, help="Delay between packets in milliseconds")
    client_parser.add_argument("--password", type=str, required=True, help="Shared password")

    args = parser.parse_args()

    if args.mode == "server":
        asyncio.run(run_server(args.tcp_port, args.udp_port, args.password))
    else:
        discord_token = os.environ.get("HORIZON_STATUS_DISCORD_TOKEN")
        discord_channel_id = os.environ.get("HORIZON_STATUS_CHANNEL_ID")
        discord_message_id = os.environ.get("HORIZON_STATUS_MESSAGE_ID")
        if not discord_token:
            print("ERROR: HORIZON_STATUS_DISCORD_TOKEN must be set for Discord updates.")
            sys.exit(1)
        if not (discord_message_id or discord_channel_id):
            print("ERROR: Provide HORIZON_STATUS_MESSAGE_ID or HORIZON_STATUS_CHANNEL_ID for Discord updates.")
            sys.exit(1)
        if not discord_channel_id:
            print("ERROR: HORIZON_STATUS_CHANNEL_ID must be set to target the Discord message.")
            sys.exit(1)
        asyncio.run(
            run_client(
                args.host,
                args.tcp_port,
                args.udp_port,
                args.delay_ms,
                args.password,
                discord_token,
                discord_channel_id,
                discord_message_id,
            )
        )


if __name__ == "__main__":
    main()
