#!/usr/bin/env python3
import argparse
import asyncio
import struct
from datetime import datetime, timezone

# Message format:
# For UDP:
#   [1 byte password_len][password bytes][8-byte unsigned big-endian timestamp_ms]
#
# For TCP:
#   [2-byte big-endian msg_len][rest as above]


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
    def __init__(self, password: str):
        self.password = password
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Got echo back; compute RTT from embedded timestamp
        _, ts_ms = parse_payload(data, self.password)
        if ts_ms is None:
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        rtt_ms = now_ms - ts_ms
        print(f"UDP RTT: {rtt_ms} ms")


async def udp_client_task(host: str, port: int, delay_ms: int, password: str):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UdpClientProtocol(password),
        remote_addr=(host, port),
    )

    try:
        while True:
            payload = build_payload(password)
            transport.sendto(payload)
            await asyncio.sleep(delay_ms / 1000.0)
    finally:
        transport.close()


async def tcp_client_task(host: str, port: int, delay_ms: int, password: str):
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
                print(f"TCP RTT: {rtt_ms} ms")
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


async def run_client(host: str, tcp_port: int, udp_port: int, delay_ms: int, password: str):
    # Run both TCP and UDP clients concurrently
    await asyncio.gather(
        udp_client_task(host, udp_port, delay_ms, password),
        tcp_client_task(host, tcp_port, delay_ms, password),
    )


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
        asyncio.run(run_client(args.host, args.tcp_port, args.udp_port, args.delay_ms, args.password))


if __name__ == "__main__":
    main()

