#!/usr/bin/env python3
"""
SSH ProxyCommand helper for tunneling SSH through WebSocket
Used by ssh with: ssh -o ProxyCommand='gpu-dev-ssh-proxy %h %p' user@host
"""

import os
import sys
import asyncio
import random
import websockets
import ssl as ssl_module

MAX_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_MAX = 5.0

# Status codes that indicate non-transient errors (don't retry)
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 4000, 4004}


def _is_retryable(exc: Exception) -> bool:
    """Return True if the error is transient and worth retrying."""
    if isinstance(exc, (asyncio.TimeoutError, ConnectionRefusedError, OSError)):
        return True
    if isinstance(exc, websockets.exceptions.InvalidStatusCode):
        return exc.status_code not in NON_RETRYABLE_STATUS_CODES
    return False


async def tunnel_ssh(target_host: str, target_port: int):
    """
    Create WebSocket tunnel to SSH server via proxy

    Args:
        target_host: Target SSH hostname
        target_port: Target SSH port
    """
    # Bypass corporate/local HTTP proxies for devservers.io - we connect
    # directly to our ALB, and proxies can cause WebSocket handshake timeouts
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(var, None)

    # Determine proxy URL based on target host
    if ".test.devservers.io" in target_host:
        proxy_host = "ssh.test.devservers.io"
    elif ".devservers.io" in target_host:
        proxy_host = "ssh.devservers.io"
    else:
        print(f"Error: Unsupported domain: {target_host}", file=sys.stderr)
        sys.exit(1)

    # WebSocket URL - wss:// for secure WebSocket
    ws_url = f"wss://{proxy_host}/tunnel/{target_host}"

    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            async with websockets.connect(
                ws_url, open_timeout=20,
                ping_interval=30, ping_timeout=10,
            ) as websocket:
                # Set up stdin/stdout for SSH
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)

                await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

                # Create writer for stdout
                writer_transport, writer_protocol = await loop.connect_write_pipe(
                    asyncio.streams.FlowControlMixin, sys.stdout.buffer
                )
                writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

                async def stdin_to_ws():
                    """Forward stdin to WebSocket"""
                    try:
                        while True:
                            data = await reader.read(8192)
                            if not data:
                                break
                            await websocket.send(data)
                    except Exception as e:
                        print(f"Error in stdin_to_ws: {e}", file=sys.stderr)
                    finally:
                        await websocket.close()

                async def ws_to_stdout():
                    """Forward WebSocket to stdout"""
                    try:
                        async for message in websocket:
                            if isinstance(message, bytes):
                                writer.write(message)
                                await writer.drain()
                    except websockets.exceptions.ConnectionClosed:
                        pass
                    except Exception as e:
                        print(f"Error in ws_to_stdout: {e}", file=sys.stderr)

                # Run both directions concurrently
                await asyncio.gather(stdin_to_ws(), ws_to_stdout())
                return  # Clean exit

        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt == MAX_RETRIES - 1:
                break
            delay = min(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.5), BACKOFF_MAX)
            print(f"Connection failed ({e}), retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})...", file=sys.stderr)
            await asyncio.sleep(delay)

    # All retries exhausted or non-retryable error
    if isinstance(last_exc, websockets.exceptions.InvalidStatusCode):
        print(f"Error connecting to proxy: HTTP {last_exc.status_code}", file=sys.stderr)
    else:
        print(f"Error connecting to proxy: {last_exc}", file=sys.stderr)
    sys.exit(1)


def main():
    """
    Main entry point for ssh ProxyCommand
    Usage: gpu-dev-ssh-proxy <target_host> <target_port>
    """
    if len(sys.argv) != 3:
        print("Usage: gpu-dev-ssh-proxy <target_host> <target_port>", file=sys.stderr)
        print("This command is meant to be used as SSH ProxyCommand", file=sys.stderr)
        sys.exit(1)

    target_host = sys.argv[1]
    target_port = int(sys.argv[2])

    # Run the async tunnel
    try:
        asyncio.run(tunnel_ssh(target_host, target_port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
