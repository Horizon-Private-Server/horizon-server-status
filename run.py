#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import struct
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Message format:
# For UDP:
#   [1 byte password_len][password bytes][8-byte unsigned big-endian timestamp_ms]
#
# For TCP:
#   [2-byte big-endian msg_len][rest as above]


DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "HorizonMetricsBot/1.0 (+https://horizon)"


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
        cutoff = now_ts_ms - 5 * 60 * 1000  # keep last 5 minutes
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

    def snapshot(self) -> Dict[str, Optional[str]]:
        """Return a shallow copy suitable for rendering."""
        now_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return {
            "tcp_ms": self.tcp_ms,
            "udp_ms": self.udp_ms,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "tcp_2m": self._window_stats(self.tcp_history, now_ts_ms, 2 * 60 * 1000),
            "tcp_5m": self._window_stats(self.tcp_history, now_ts_ms, 5 * 60 * 1000),
            "udp_2m": self._window_stats(self.udp_history, now_ts_ms, 2 * 60 * 1000),
            "udp_5m": self._window_stats(self.udp_history, now_ts_ms, 5 * 60 * 1000),
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


def write_message_id_to_file(path: str, message_id: str) -> None:
    try:
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(message_id)
    except Exception as e:
        print(f"[DISCORD] Failed to persist message id to {path}: {e}")


# ----------------- SERVER SIDE -----------------


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
                            password: str):
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
            # Echo back the same frame
            writer.write(header + body)
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
    loop = asyncio.get_running_loop()

    # UDP server
    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: UdpServerProtocol(password),
        local_addr=("0.0.0.0", udp_port),
    )

    # TCP server
    tcp_server = await asyncio.start_server(
        lambda r, w: handle_tcp_client(r, w, password),
        host="0.0.0.0",
        port=tcp_port,
    )
    addrs = ", ".join(str(sock.getsockname()) for sock in tcp_server.sockets)
    print(f"[SERVER] TCP listening on {addrs}")

    async with tcp_server:
        try:
            await tcp_server.serve_forever()
        finally:
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


async def tcp_client_task(host: str, port: int, delay_ms: int, password: str, stats: RttStats):
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


def build_discord_content(snapshot: Dict[str, Optional[str]]) -> str:
    tcp = snapshot["tcp_ms"]
    udp = snapshot["udp_ms"]
    updated = snapshot["updated_at"]
    tcp_2m = snapshot.get("tcp_2m")
    tcp_5m = snapshot.get("tcp_5m")
    udp_2m = snapshot.get("udp_2m")
    udp_5m = snapshot.get("udp_5m")
    tcp_str = f"{tcp} ms" if tcp is not None else "n/a"
    udp_str = f"{udp} ms" if udp is not None else "n/a"
    if updated:
        # Discord timestamp tags render in each user's local timezone
        try:
            updated_dt = datetime.fromisoformat(updated)
            ts = int(updated_dt.timestamp())
            updated_str = f"<t:{ts}:F> (<t:{ts}:R>)"
        except Exception:
            updated_str = updated
    else:
        updated_str = "pending"

    def fmt_window(data: Optional[Dict[str, float]]) -> str:
        if not data:
            return "n/a"
        return (
            f"avg `{data['avg']:.1f} ms` · "
            f"min `{int(data['min'])}` · "
            f"max `{int(data['max'])}`"
        )

    return (
        "**Server Status (ping from FourBolt's place)**\n"
        f"**TCP**\n"
        f"• Latest: `{tcp_str}`\n"
        f"• 2m: {fmt_window(tcp_2m)}\n"
        f"• 5m: {fmt_window(tcp_5m)}\n"
        f"**UDP**\n"
        f"• Latest: `{udp_str}`\n"
        f"• 2m: {fmt_window(udp_2m)}\n"
        f"• 5m: {fmt_window(udp_5m)}\n"
        f"*Updated:* {updated_str}\n"
    )


async def discord_status_task(
    stats: RttStats,
    bot: DiscordBotClient,
    interval_seconds: int = 5,
    message_id_file: Optional[str] = None,
):
    if not await bot.initialize():
        print("[DISCORD] Skipping status updates due to failed bot initialization")
        return

    current_message_id = bot.message_id or None
    while True:
        snapshot = stats.snapshot()
        content = build_discord_content(snapshot)
        if current_message_id:
            success = await bot.edit_message(current_message_id, content)
            if not success:
                # Try to recreate on next iteration
                current_message_id = None
        else:
            new_id = await bot.post_message(content)
            if new_id:
                current_message_id = new_id
                bot.message_id = new_id
                print(f"[DISCORD] Posted status message id={new_id}")
                if message_id_file:
                    write_message_id_to_file(message_id_file, new_id)
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
    discord_message_id_file: Optional[str],
):
    stats = RttStats()
    tasks = [
        udp_client_task(host, udp_port, delay_ms, password, stats),
        tcp_client_task(host, tcp_port, delay_ms, password, stats),
    ]

    if discord_token and discord_channel_id:
        bot = DiscordBotClient(discord_token, discord_channel_id, discord_message_id)
        tasks.append(
            discord_status_task(stats, bot, message_id_file=discord_message_id_file)
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
        discord_message_id_file = os.environ.get("HORIZON_STATUS_MESSAGE_ID_FILE")
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
                discord_message_id_file,
            )
        )


if __name__ == "__main__":
    main()
