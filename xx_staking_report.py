#!/usr/bin/env python3
"""Create a staking reward and validator commission report for xx Network."""

from __future__ import annotations

import argparse
import atexit
import base64
import csv
import hashlib
import json
import os
import socket
import smtplib
import ssl
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


API_URL = "https://indexer.xx.network/v1/graphql"
RPC_URL = "https://xxnetwork-rpc.n.dwellir.com"
ACTIVE_VALIDATORS_RPC_URL = "https://xxnetwork-rpc.n.dwellir.com"
OFFICIAL_FALLBACK_RPC_URL = "wss://rpc.xx.network"
MARKET_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=xxcoin&vs_currencies=usd,cad&include_last_updated_at=true"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)
XX_ATOMIC_UNITS = Decimal("1000000000")
DEFAULT_CANDIDATE_MAX_COMMISSION = Decimal("18")
DEFAULT_CANDIDATE_MAX_HISTORICAL_COMMISSION = Decimal("25")
DEFAULT_CANDIDATE_MIN_HISTORY_ERAS = 60
DEFAULT_ACCOUNTS_FILE = Path(__file__).with_name("xx_staking_accounts.json")
DEFAULT_OUTPUT_DIR = Path(__file__).with_name("xx_staking_output")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
XXH64_MASK = (1 << 64) - 1
XXH64_PRIME_1 = 11400714785074694791
XXH64_PRIME_2 = 14029467366897019727
XXH64_PRIME_3 = 1609587929392839161
XXH64_PRIME_4 = 9650029242287828579
XXH64_PRIME_5 = 2870177450012600261

OVERVIEW_QUERY = """
query StakingOverview($accountIds: [String!]!) {
  configuredAccounts: account(where: {account_id: {_in: $accountIds}}) {
    account_id
    bonded_balance
  }
  latestReward: staking_reward(order_by: {era: desc}, limit: 1) {
    era
    timestamp
  }
  latestValidatorStats: validator_stats(order_by: {era: desc}, limit: 1) {
    era
    timestamp
  }
}
"""

REWARDS_QUERY = """
query RewardsInWindow($accountIds: [String!]!, $fromEra: Int!, $toEra: Int!) {
  rewards: staking_reward(
    where: {
      account_id: {_in: $accountIds},
      era: {_gte: $fromEra, _lte: $toEra}
    },
    order_by: {era: desc, block_number: desc}
  ) {
    account_id
    amount
    era
    timestamp
    validator_id
  }
}
"""

VALIDATOR_NAMES_QUERY = """
query ValidatorNames($validatorIds: [String!]!) {
  validators: validator(where: {stash_address: {_in: $validatorIds}}) {
    stash_address
    account {
      identity {
        display
      }
    }
  }
}
"""

VALIDATOR_HISTORY_QUERY = """
query ValidatorHistory($validatorIds: [String!]!) {
  validators: validator(where: {stash_address: {_in: $validatorIds}}) {
    stash_address
    account {
      identity {
        display
      }
    }
  }
  stats: validator_stats(
    where: {stash_address: {_in: $validatorIds}},
    order_by: {era: desc}
  ) {
    stash_address
    era
    commission
  }
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query the xx Network indexer for rewards and the current RPC "
            "for nominations and validator commissions."
        )
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=DEFAULT_ACCOUNTS_FILE,
        help="JSON file containing a label and address for each account.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of eras/days to include in the average (default: 30).",
    )
    parser.add_argument(
        "--end-era",
        type=int,
        help="Last era to include; defaults to the most recent era with a reward.",
    )
    parser.add_argument(
        "--max-commission",
        dest="max_commission",
        type=Decimal,
        default=Decimal("22"),
        help="Maximum accepted commission; alert only above it (default: > 22).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for the Markdown report and CSV files.",
    )
    parser.add_argument(
        "--api-url",
        default=API_URL,
        help="xx Network indexer GraphQL endpoint.",
    )
    parser.add_argument(
        "--rpc-url",
        default=RPC_URL,
        help="Current xx Network RPC endpoint for nominations and commissions.",
    )
    parser.add_argument(
        "--active-validators-rpc-url",
        default=ACTIVE_VALIDATORS_RPC_URL,
        help="Wallet-listed RPC endpoint used to enumerate active validators.",
    )
    parser.add_argument(
        "--fallback-rpc-url",
        default=OFFICIAL_FALLBACK_RPC_URL,
        help="Fallback RPC tried after a primary RPC failure (default: official wss://rpc.xx.network).",
    )
    parser.add_argument(
        "--candidate-max-commission",
        type=Decimal,
        default=DEFAULT_CANDIDATE_MAX_COMMISSION,
        help="Maximum current commission for a candidate (default: <= 18).",
    )
    parser.add_argument(
        "--candidate-max-historical-commission",
        type=Decimal,
        default=DEFAULT_CANDIDATE_MAX_HISTORICAL_COMMISSION,
        help="Maximum observed historical commission for a candidate (default: <= 25).",
    )
    parser.add_argument(
        "--candidate-min-history-eras",
        type=int,
        default=DEFAULT_CANDIDATE_MIN_HISTORY_ERAS,
        help="Minimum number of indexed eras observed for a candidate (default: 60).",
    )
    parser.add_argument(
        "--price-url",
        default=MARKET_PRICE_URL,
        help="CoinGecko endpoint for the current XX price in USD and CAD.",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP user agent used when contacting the Cloudflare-protected API.",
    )
    parser.add_argument(
        "--monitor-state-file",
        type=Path,
        help="JSON state file used to detect commission increases.",
    )
    parser.add_argument(
        "--email-on-alert",
        action="store_true",
        help="Send email when an actionable alert is detected.",
    )
    parser.add_argument(
        "--email-test",
        action="store_true",
        help="Send a test email even when no alert is present.",
    )
    parser.add_argument(
        "--email-to",
        default=os.environ.get("XX_STAKING_EMAIL_TO", ""),
        help="Notification recipient email address.",
    )
    parser.add_argument(
        "--smtp-host",
        default=os.environ.get("XX_STAKING_SMTP_HOST", "smtp.gmail.com"),
        help="Outbound SMTP server (default: smtp.gmail.com).",
    )
    parser.add_argument(
        "--smtp-port",
        type=int,
        default=int(os.environ.get("XX_STAKING_SMTP_PORT", "465")),
        help="Implicit TLS SMTP port (default: 465).",
    )
    parser.add_argument(
        "--smtp-user",
        default=os.environ.get("XX_STAKING_SMTP_USER", ""),
        help="SMTP sender account.",
    )
    parser.add_argument(
        "--smtp-password-env",
        default="XX_STAKING_SMTP_PASSWORD",
        help="Name of the environment variable containing the SMTP password.",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be greater than zero")
    if args.candidate_min_history_eras < 1:
        parser.error("--candidate-min-history-eras must be greater than zero")
    return args


def load_accounts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(
            f"Accounts file not found: {path}. Create xx_staking_accounts.json "
            "from xx_staking_accounts.example.json and add your addresses."
        )
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list) or not raw:
        raise ValueError("The accounts file must contain a non-empty list.")

    accounts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each account must be a JSON object.")
        label = str(item.get("label", "")).strip()
        address = str(item.get("address", "")).strip()
        if not label or not address:
            raise ValueError("Each account must have a label and an address.")
        if address in seen:
            raise ValueError(f"Duplicate address: {address}")
        seen.add(address)
        accounts.append({"label": label, "address": address})
    return accounts


def graphql(
    api_url: str, user_agent: str, query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API HTTP error {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Unable to reach the API: {exc.reason}") from exc
    if payload.get("errors"):
        raise RuntimeError("GraphQL error: " + json.dumps(payload["errors"]))
    return payload["data"]


RPC_EFFECTIVE_ENDPOINTS: dict[str, str] = {}
RPC_FAILOVER_EVENTS: list[tuple[str, str, str]] = []
WEBSOCKET_CONNECTIONS: dict[str, tuple[socket.socket, bytearray]] = {}


def http_rpc(rpc_url: str, user_agent: str, method: str, params: list[Any]) -> Any:
    body = json.dumps(
        {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    ).encode("utf-8")
    request = Request(
        rpc_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"RPC HTTP error {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Unable to reach the RPC: {exc.reason}") from exc
    if payload.get("error"):
        raise RuntimeError("RPC error: " + json.dumps(payload["error"]))
    return payload.get("result")


def websocket_frame(payload: bytes, opcode: int) -> bytes:
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header)


def websocket_take(connection: socket.socket, buffer: bytearray, length: int) -> bytes:
    while len(buffer) < length:
        chunk = connection.recv(max(4096, length - len(buffer)))
        if not chunk:
            raise RuntimeError("WebSocket connection closed before the RPC response.")
        buffer.extend(chunk)
    value = bytes(buffer[:length])
    del buffer[:length]
    return value


def open_websocket_connection(
    rpc_url: str, user_agent: str
) -> tuple[socket.socket, bytearray]:
    parsed = urlsplit(rpc_url)
    host = parsed.hostname
    if not host or parsed.scheme not in {"ws", "wss"}:
        raise RuntimeError(f"Invalid WebSocket RPC URL: {rpc_url}")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    default_port = 443 if parsed.scheme == "wss" else 80
    host_header = host if port == default_port else f"{host}:{port}"
    for attempt in range(3):
        connection: socket.socket | None = None
        try:
            connection = socket.create_connection((host, port), timeout=30)
            if parsed.scheme == "wss":
                connection = ssl.create_default_context().wrap_socket(
                    connection, server_hostname=host
                )
                connection.settimeout(30)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: {user_agent}\r\n"
                "\r\n"
            ).encode("ascii")
            connection.sendall(handshake)
            buffer = bytearray()
            while b"\r\n\r\n" not in buffer:
                chunk = connection.recv(4096)
                if not chunk:
                    raise RuntimeError("The WebSocket RPC closed during the handshake.")
                buffer.extend(chunk)
                if len(buffer) > 65536:
                    raise RuntimeError("The WebSocket RPC handshake response is too large.")
            split_at = buffer.find(b"\r\n\r\n") + 4
            header_lines = bytes(buffer[:split_at]).decode("iso-8859-1").split("\r\n")
            del buffer[:split_at]
            if len(header_lines) < 1 or " 101 " not in header_lines[0]:
                raise RuntimeError(
                    f"WebSocket RPC handshake refused: {header_lines[0] if header_lines else 'no response'}"
                )
            headers = {}
            for line in header_lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            expected_accept = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            ).decode("ascii")
            if headers.get("sec-websocket-accept") != expected_accept:
                raise RuntimeError("Invalid WebSocket RPC handshake response.")
            return connection, buffer
        except RuntimeError:
            if connection is not None:
                connection.close()
            if attempt == 2:
                raise
        except (OSError, UnicodeError, ValueError) as exc:
            if connection is not None:
                connection.close()
            if attempt == 2:
                raise RuntimeError(f"Unable to reach WebSocket RPC {rpc_url}: {exc}") from exc
        time.sleep(1)
    raise RuntimeError(f"Unable to reach WebSocket RPC {rpc_url}.")


def close_websocket_connection(rpc_url: str) -> None:
    item = WEBSOCKET_CONNECTIONS.pop(rpc_url, None)
    if item is not None:
        item[0].close()


def close_websocket_connections() -> None:
    for rpc_url in list(WEBSOCKET_CONNECTIONS):
        close_websocket_connection(rpc_url)


atexit.register(close_websocket_connections)


def websocket_message(connection: socket.socket, buffer: bytearray) -> bytes:
    message = bytearray()
    started = False
    while True:
        first = websocket_take(connection, buffer, 2)
        finished = bool(first[0] & 0x80)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", websocket_take(connection, buffer, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", websocket_take(connection, buffer, 8))[0]
        mask = websocket_take(connection, buffer, 4) if masked else None
        payload = websocket_take(connection, buffer, length)
        if mask:
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload)
            )
        if opcode == 0x8:
            raise RuntimeError("The WebSocket RPC closed before returning a response.")
        if opcode == 0x9:
            connection.sendall(websocket_frame(payload, 0xA))
            continue
        if opcode == 0xA:
            continue
        if opcode in {0x1, 0x2}:
            message = bytearray(payload)
            started = True
        elif opcode == 0x0 and started:
            message.extend(payload)
        else:
            continue
        if finished:
            return bytes(message)


def websocket_rpc(rpc_url: str, user_agent: str, method: str, params: list[Any]) -> Any:
    for attempt in range(2):
        if rpc_url not in WEBSOCKET_CONNECTIONS:
            WEBSOCKET_CONNECTIONS[rpc_url] = open_websocket_connection(
                rpc_url, user_agent
            )
        connection, buffer = WEBSOCKET_CONNECTIONS[rpc_url]
        try:
            request_body = json.dumps(
                {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
            ).encode("utf-8")
            connection.sendall(websocket_frame(request_body, 0x1))
            response = json.loads(websocket_message(connection, buffer).decode("utf-8"))
        except (RuntimeError, OSError, UnicodeError, ValueError) as exc:
            close_websocket_connection(rpc_url)
            if attempt == 1:
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"Unable to reach WebSocket RPC {rpc_url}: {exc}") from exc
            continue
        if response.get("error"):
            raise RuntimeError("RPC error: " + json.dumps(response["error"]))
        return response.get("result")
    raise RuntimeError(f"Unable to reach WebSocket RPC {rpc_url}.")


def endpoint_rpc(rpc_url: str, user_agent: str, method: str, params: list[Any]) -> Any:
    scheme = urlsplit(rpc_url).scheme.lower()
    if scheme in {"ws", "wss"}:
        return websocket_rpc(rpc_url, user_agent, method, params)
    if scheme in {"http", "https"}:
        return http_rpc(rpc_url, user_agent, method, params)
    raise RuntimeError(f"Unsupported RPC protocol for {rpc_url}.")


def rpc(
    rpc_url: str,
    user_agent: str,
    method: str,
    params: list[Any],
    fallback_url: str | None = None,
) -> Any:
    effective_url = RPC_EFFECTIVE_ENDPOINTS.get(rpc_url, rpc_url)
    try:
        return endpoint_rpc(effective_url, user_agent, method, params)
    except RuntimeError as primary_error:
        if effective_url != rpc_url or not fallback_url or fallback_url == rpc_url:
            raise
        try:
            result = endpoint_rpc(fallback_url, user_agent, method, params)
        except RuntimeError as fallback_error:
            raise RuntimeError(
                f"Primary RPC failed ({rpc_url}): {primary_error}; "
                f"fallback RPC failed ({fallback_url}): {fallback_error}"
            ) from fallback_error
        RPC_EFFECTIVE_ENDPOINTS[rpc_url] = fallback_url
        RPC_FAILOVER_EVENTS.append((rpc_url, fallback_url, str(primary_error)))
        return result


def rpc_endpoint_label(rpc_url: str) -> str:
    effective_url = RPC_EFFECTIVE_ENDPOINTS.get(rpc_url, rpc_url)
    if effective_url == rpc_url:
        return rpc_url
    return f"{effective_url} (fallback after failure of {rpc_url})"


def market_prices(price_url: str, user_agent: str) -> dict[str, Any]:
    request = Request(
        price_url,
        headers={"Accept": "application/json", "User-Agent": user_agent},
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Price API HTTP error {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Unable to reach the price API: {exc.reason}") from exc
    coin = payload.get("xxcoin") or {}
    if coin.get("usd") is None or coin.get("cad") is None:
        raise RuntimeError("CoinGecko did not return the XX price in USD and CAD.")
    return {
        "usd": Decimal(str(coin["usd"])),
        "cad": Decimal(str(coin["cad"])),
        "last_updated_at": coin.get("last_updated_at"),
    }


def rotate_left(value: int, bits: int) -> int:
    value &= XXH64_MASK
    return ((value << bits) | (value >> (64 - bits))) & XXH64_MASK


def xxh64_round(accumulator: int, value: int) -> int:
    accumulator = (accumulator + value * XXH64_PRIME_2) & XXH64_MASK
    accumulator = rotate_left(accumulator, 31)
    return (accumulator * XXH64_PRIME_1) & XXH64_MASK


def xxh64_merge(accumulator: int, value: int) -> int:
    accumulator ^= xxh64_round(0, value)
    return (accumulator * XXH64_PRIME_1 + XXH64_PRIME_4) & XXH64_MASK


def xxh64(data: bytes, seed: int = 0) -> int:
    offset = 0
    length = len(data)
    if length >= 32:
        v1 = (seed + XXH64_PRIME_1 + XXH64_PRIME_2) & XXH64_MASK
        v2 = (seed + XXH64_PRIME_2) & XXH64_MASK
        v3 = seed & XXH64_MASK
        v4 = (seed - XXH64_PRIME_1) & XXH64_MASK
        while offset <= length - 32:
            v1 = xxh64_round(v1, int.from_bytes(data[offset : offset + 8], "little"))
            v2 = xxh64_round(v2, int.from_bytes(data[offset + 8 : offset + 16], "little"))
            v3 = xxh64_round(v3, int.from_bytes(data[offset + 16 : offset + 24], "little"))
            v4 = xxh64_round(v4, int.from_bytes(data[offset + 24 : offset + 32], "little"))
            offset += 32
        result = (
            rotate_left(v1, 1)
            + rotate_left(v2, 7)
            + rotate_left(v3, 12)
            + rotate_left(v4, 18)
        ) & XXH64_MASK
        for value in (v1, v2, v3, v4):
            result = xxh64_merge(result, value)
    else:
        result = (seed + XXH64_PRIME_5) & XXH64_MASK
    result = (result + length) & XXH64_MASK
    while offset <= length - 8:
        lane = int.from_bytes(data[offset : offset + 8], "little")
        result ^= xxh64_round(0, lane)
        result = (rotate_left(result, 27) * XXH64_PRIME_1 + XXH64_PRIME_4) & XXH64_MASK
        offset += 8
    if offset <= length - 4:
        result ^= (
            int.from_bytes(data[offset : offset + 4], "little") * XXH64_PRIME_1
        ) & XXH64_MASK
        result &= XXH64_MASK
        result = (rotate_left(result, 23) * XXH64_PRIME_2 + XXH64_PRIME_3) & XXH64_MASK
        offset += 4
    while offset < length:
        result ^= (data[offset] * XXH64_PRIME_5) & XXH64_MASK
        result &= XXH64_MASK
        result = (rotate_left(result, 11) * XXH64_PRIME_1) & XXH64_MASK
        offset += 1
    result ^= result >> 33
    result = (result * XXH64_PRIME_2) & XXH64_MASK
    result ^= result >> 29
    result = (result * XXH64_PRIME_3) & XXH64_MASK
    result ^= result >> 32
    return result & XXH64_MASK


def twox128(value: str) -> bytes:
    data = value.encode("ascii")
    return xxh64(data, 0).to_bytes(8, "little") + xxh64(data, 1).to_bytes(8, "little")


def base58_decode(value: str) -> bytes:
    number = 0
    for character in value:
        try:
            digit = BASE58_ALPHABET.index(character)
        except ValueError as exc:
            raise ValueError(f"Invalid SS58 address: {value}") from exc
        number = number * 58 + digit
    encoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + encoded


def base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58_ALPHABET[digit] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + (encoded or "")


def ss58_decode(address: str) -> tuple[bytes, bytes]:
    raw = base58_decode(address)
    if not raw:
        raise ValueError(f"Empty SS58 address: {address}")
    prefix_length = 2 if raw[0] & 0b01000000 else 1
    account_id = raw[prefix_length : prefix_length + 32]
    checksum = raw[prefix_length + 32 :]
    if len(account_id) != 32 or not checksum:
        raise ValueError(f"Invalid SS58 address length: {address}")
    expected = hashlib.blake2b(
        b"SS58PRE" + raw[: prefix_length + 32], digest_size=64
    ).digest()[: len(checksum)]
    if checksum != expected:
        raise ValueError(f"Invalid SS58 checksum: {address}")
    return account_id, raw[:prefix_length]


def ss58_encode(account_id: bytes, prefix: bytes) -> str:
    payload = prefix + account_id
    checksum = hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]
    return base58_encode(payload + checksum)


def storage_key(pallet: str, item: str, account_id: bytes) -> str:
    key = (
        twox128(pallet)
        + twox128(item)
        + xxh64(account_id).to_bytes(8, "little")
        + account_id
    )
    return "0x" + key.hex()


def singleton_storage_key(pallet: str, item: str) -> str:
    return "0x" + (twox128(pallet) + twox128(item)).hex()


def double_storage_key_twox64(
    pallet: str, item: str, first_key: bytes, second_key: bytes
) -> str:
    key = (
        twox128(pallet)
        + twox128(item)
        + xxh64(first_key).to_bytes(8, "little")
        + first_key
        + xxh64(second_key).to_bytes(8, "little")
        + second_key
    )
    return "0x" + key.hex()


def decode_compact(data: bytes, offset: int = 0) -> tuple[int, int]:
    mode = data[offset] & 0b11
    if mode == 0:
        return data[offset] >> 2, offset + 1
    if mode == 1:
        return int.from_bytes(data[offset : offset + 2], "little") >> 2, offset + 2
    if mode == 2:
        return int.from_bytes(data[offset : offset + 4], "little") >> 2, offset + 4
    byte_length = (data[offset] >> 2) + 4
    start = offset + 1
    return int.from_bytes(data[start : start + byte_length], "little"), start + byte_length


def storage_values(
    rpc_url: str,
    user_agent: str,
    keys: list[str],
    block_hash: str,
    fallback_url: str | None = None,
    batch_size: int = 75,
) -> dict[str, bytes | None]:
    if not keys:
        return {}
    returned: dict[str, Any] = {}
    for offset in range(0, len(keys), batch_size):
        batch = keys[offset : offset + batch_size]
        rows = rpc(
            rpc_url,
            user_agent,
            "state_queryStorageAt",
            [batch, block_hash],
            fallback_url,
        )
        changes = rows[0]["changes"] if rows else []
        returned.update({key: value for key, value in changes})
    return {
        key: (
            bytes.fromhex(returned[key][2:])
            if returned.get(key) is not None
            else None
        )
        for key in keys
    }


def decode_nominations(value: bytes | None, prefix: bytes) -> set[str]:
    if value is None:
        return set()
    count, offset = decode_compact(value)
    targets: set[str] = set()
    for _ in range(count):
        account_id = value[offset : offset + 32]
        if len(account_id) != 32:
            raise RuntimeError("Incomplete Staking.Nominators value returned by the RPC.")
        targets.add(ss58_encode(account_id, prefix))
        offset += 32
    return targets


def decode_account_ids(value: bytes | None, prefix: bytes) -> list[str]:
    if value is None:
        return []
    count, offset = decode_compact(value)
    addresses: list[str] = []
    for _ in range(count):
        account_id = value[offset : offset + 32]
        if len(account_id) != 32:
            raise RuntimeError("Incomplete account list returned by the RPC.")
        addresses.append(ss58_encode(account_id, prefix))
        offset += 32
    return addresses


def decode_validator_commission(value: bytes | None) -> Decimal | None:
    if value is None:
        return None
    perbill, _ = decode_compact(value)
    return Decimal(perbill) / Decimal("10000000")


def decode_staking_exposure(value: bytes | None, prefix: bytes) -> dict[str, Any] | None:
    if value is None:
        return None
    total, offset = decode_compact(value)
    custody, offset = decode_compact(value, offset)
    own, offset = decode_compact(value, offset)
    nominators, offset = decode_compact(value, offset)
    nominator_stakes: dict[str, Decimal] = {}
    for _ in range(nominators):
        account_id = value[offset : offset + 32]
        if len(account_id) != 32:
            raise RuntimeError("Incomplete Staking.ErasStakers exposure returned by the RPC.")
        offset += 32
        stake, offset = decode_compact(value, offset)
        nominator_stakes[ss58_encode(account_id, prefix)] = to_xx(stake)
    return {
        "total": to_xx(total),
        "custody": to_xx(custody),
        "own": to_xx(own),
        "active_nominators": nominators,
        "nominator_stakes": nominator_stakes,
        "smallest_nominator": (
            min(nominator_stakes.values()) if nominator_stakes else None
        ),
    }


def to_xx(atomic_amount: Any) -> Decimal:
    return Decimal(str(atomic_amount)) / XX_ATOMIC_UNITS


def fmt_xx(amount: Decimal) -> str:
    value = f"{amount.quantize(Decimal('0.000000001')):f}"
    return value.rstrip("0").rstrip(".") or "0"


def fmt_fiat(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01')):,.2f}"


def fmt_market_price(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.00000001')):f}"


def fmt_percent(value: Any) -> str:
    if value is None:
        return "unknown"
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):f}"


def fmt_timestamp(value: Any) -> str:
    if value is None:
        return ""
    parsed = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def identity_name(validator: dict[str, Any] | None) -> str:
    if not validator:
        return ""
    return ((validator.get("account") or {}).get("identity") or {}).get("display") or ""


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def send_email(
    host: str,
    port: int,
    user: str,
    password_env: str,
    recipient: str,
    subject: str,
    content: str,
) -> None:
    password = os.environ.get(password_env, "")
    if not user or not recipient or not password:
        raise RuntimeError(
            "Email notification requested but the SMTP configuration is incomplete. "
            "Configure the sender, recipient, and app password."
        )
    message = EmailMessage()
    message["From"] = user
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(content)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    args = parse_args()
    accounts = load_accounts(args.accounts_file)
    address_ids = [account["address"] for account in accounts]
    monitor_state_path = (
        args.monitor_state_file
        if args.monitor_state_file is not None
        else args.output_dir / "monitor_state.json"
    )
    previous_state = load_state(monitor_state_path)

    overview = graphql(
        args.api_url, args.user_agent, OVERVIEW_QUERY, {"accountIds": address_ids}
    )
    returned_address_ids = {
        row["account_id"] for row in (overview.get("configuredAccounts") or [])
    }
    configured_by_address = {
        row["account_id"]: row for row in (overview.get("configuredAccounts") or [])
    }
    missing_accounts = [
        account for account in accounts if account["address"] not in returned_address_ids
    ]
    if missing_accounts:
        details = ", ".join(
            f'{account["label"]} ({account["address"]})' for account in missing_accounts
        )
        raise RuntimeError(
            "Address not found in the indexer: "
            f"{details}. Check the address in your configuration file."
        )
    latest_reward_rows = overview.get("latestReward") or []
    if not latest_reward_rows:
        raise RuntimeError("The API returned no reward era.")

    latest_reward = latest_reward_rows[0]
    end_era = args.end_era if args.end_era is not None else int(latest_reward["era"])
    from_era = end_era - args.days + 1
    reward_data = graphql(
        args.api_url,
        args.user_agent,
        REWARDS_QUERY,
        {"accountIds": address_ids, "fromEra": from_era, "toEra": end_era},
    )
    rewards = reward_data.get("rewards") or []
    rewards_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for reward in rewards:
        rewards_by_account[reward["account_id"]].append(reward)

    block_hash = rpc(
        args.rpc_url, args.user_agent, "chain_getFinalizedHead", [], args.fallback_rpc_url
    )
    block_header = rpc(
        args.rpc_url,
        args.user_agent,
        "chain_getHeader",
        [block_hash],
        args.fallback_rpc_url,
    )
    block_number = int(block_header["number"], 16)
    decoded_owners = {
        owner["address"]: ss58_decode(owner["address"]) for owner in accounts
    }
    nominator_keys = {
        address: storage_key("Staking", "Nominators", account_id)
        for address, (account_id, _) in decoded_owners.items()
    }
    owned_validator_keys = {
        address: storage_key("Staking", "Validators", account_id)
        for address, (account_id, _) in decoded_owners.items()
    }
    owner_storage = storage_values(
        args.rpc_url,
        args.user_agent,
        list(nominator_keys.values()) + list(owned_validator_keys.values()),
        block_hash,
        args.fallback_rpc_url,
    )
    current_targets_by_owner = {
        address: decode_nominations(owner_storage[nominator_keys[address]], prefix)
        for address, (_, prefix) in decoded_owners.items()
    }
    owned_validator_ids = {
        address
        for address in address_ids
        if owner_storage[owned_validator_keys[address]] is not None
    }

    requested_validators: set[str] = set()
    for targets in current_targets_by_owner.values():
        requested_validators.update(targets)
    for reward in rewards:
        requested_validators.add(reward["validator_id"])
    requested_validators.update(owned_validator_ids)

    validator_keys = {
        address: storage_key("Staking", "Validators", ss58_decode(address)[0])
        for address in requested_validators
    }
    validator_storage = storage_values(
        args.rpc_url,
        args.user_agent,
        list(validator_keys.values()),
        block_hash,
        args.fallback_rpc_url,
    )
    current_commissions = {
        address: decode_validator_commission(validator_storage[key])
        for address, key in validator_keys.items()
    }

    validator_records: list[dict[str, Any]] = []
    if requested_validators:
        result = graphql(
            args.api_url,
            args.user_agent,
            VALIDATOR_NAMES_QUERY,
            {"validatorIds": sorted(requested_validators)},
        )
        validator_records = result.get("validators") or []
    validators_by_address = {
        record["stash_address"]: record for record in validator_records
    }

    summary_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    detail_seen: set[tuple[str, str, str]] = set()
    all_accounts_total_reward = Decimal("0")
    all_accounts_daily_reward = Decimal("0")

    def add_detail(
        owner: dict[str, str], source: str, validator_id: str
    ) -> None:
        key = (owner["address"], source, validator_id)
        if key in detail_seen:
            return
        detail_seen.add(key)
        validator = validators_by_address.get(validator_id)
        commission = current_commissions.get(validator_id)
        high_commission = (
            commission is not None
            and Decimal(str(commission)) > args.max_commission
        )
        inactive_target = commission is None and source == "current_nomination_target"
        alert = high_commission or inactive_target
        alert_reason = (
            f"commission > {fmt_percent(args.max_commission)} %"
            if high_commission
            else ("inactive / offline" if inactive_target else "")
        )
        detail_rows.append(
            {
                "owner_label": owner["label"],
                "owner_address": owner["address"],
                "source": source,
                "validator_address": validator_id,
                "validator_name": identity_name(validator),
                "commission_percent": fmt_percent(commission),
                "alert": "YES" if alert else "NO",
                "alert_reason": alert_reason,
                "commission_source": "RPC Staking.Validators",
                "rpc_finalized_block": block_number,
            }
        )

    for owner in accounts:
        owner_rewards = rewards_by_account[owner["address"]]
        total_reward = sum((to_xx(row["amount"]) for row in owner_rewards), Decimal("0"))
        daily_reward = total_reward / Decimal(args.days)
        all_accounts_total_reward += total_reward
        all_accounts_daily_reward += daily_reward
        reward_validator_ids = {row["validator_id"] for row in owner_rewards}
        current_target_ids = current_targets_by_owner[owner["address"]]
        owner_is_validator = owner["address"] in owned_validator_ids
        own_commission = current_commissions.get(owner["address"])
        own_over_limit = (
            owner_is_validator
            and own_commission is not None
            and own_commission > args.max_commission
        )

        summary_rows.append(
            {
                "label": owner["label"],
                "address": owner["address"],
                "era_from": from_era,
                "era_to": end_era,
                "days": args.days,
                "bonded_xx": fmt_xx(
                    to_xx(configured_by_address[owner["address"]]["bonded_balance"])
                ),
                "total_rewards_xx": fmt_xx(total_reward),
                "average_rewards_xx_per_day": fmt_xx(daily_reward),
                "reward_events": len(owner_rewards),
                "reward_validators_in_window": len(reward_validator_ids),
                "current_nomination_targets": len(current_target_ids),
                "is_validator": "YES" if owner_is_validator else "NO",
                "own_commission_over_limit": "YES" if own_over_limit else "NO",
            }
        )

        for target in sorted(current_target_ids):
            add_detail(owner, "current_nomination_target", target)
        for validator_id in sorted(reward_validator_ids):
            add_detail(owner, "reward_source_in_window", validator_id)
        if owner_is_validator:
            add_detail(owner, "owned_validator", owner["address"])

    all_current_targets = {
        target
        for targets in current_targets_by_owner.values()
        for target in targets
    }
    active_block_hash = rpc(
        args.active_validators_rpc_url,
        args.user_agent,
        "chain_getFinalizedHead",
        [],
        args.fallback_rpc_url,
    )
    active_block_header = rpc(
        args.active_validators_rpc_url,
        args.user_agent,
        "chain_getHeader",
        [active_block_hash],
        args.fallback_rpc_url,
    )
    active_block_number = int(active_block_header["number"], 16)
    address_prefix = next(iter(decoded_owners.values()))[1]
    active_storage_hex = rpc(
        args.active_validators_rpc_url,
        args.user_agent,
        "state_getStorage",
        [singleton_storage_key("Session", "Validators"), active_block_hash],
        args.fallback_rpc_url,
    )
    active_validators = decode_account_ids(
        bytes.fromhex(active_storage_hex[2:]) if active_storage_hex else None,
        address_prefix,
    )
    active_validator_keys = {
        address: storage_key("Staking", "Validators", ss58_decode(address)[0])
        for address in active_validators
    }
    active_validator_storage = storage_values(
        args.active_validators_rpc_url,
        args.user_agent,
        list(active_validator_keys.values()),
        active_block_hash,
        args.fallback_rpc_url,
    )
    active_commissions = {
        address: decode_validator_commission(active_validator_storage[key])
        for address, key in active_validator_keys.items()
    }
    candidate_pool = [
        address
        for address in active_validators
        if active_commissions[address] is not None
        and active_commissions[address] <= args.candidate_max_commission
        and address not in owned_validator_ids
    ]
    candidate_names: dict[str, str] = {}
    candidate_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if candidate_pool:
        candidate_data = graphql(
            args.api_url,
            args.user_agent,
            VALIDATOR_HISTORY_QUERY,
            {"validatorIds": candidate_pool},
        )
        candidate_names = {
            row["stash_address"]: identity_name(row)
            for row in (candidate_data.get("validators") or [])
        }
        for row in candidate_data.get("stats") or []:
            candidate_history[row["stash_address"]].append(row)

    candidate_rows: list[dict[str, Any]] = []
    for address in candidate_pool:
        name = candidate_names.get(address, "")
        if "bootnode" in name.lower():
            continue
        history = candidate_history[address]
        observed_eras = sorted({int(row["era"]) for row in history})
        historical_commissions = [
            Decimal(str(row["commission"]))
            for row in history
            if row.get("commission") is not None
        ]
        historical_max = max(historical_commissions, default=None)
        if (
            len(observed_eras) < args.candidate_min_history_eras
            or historical_max is None
            or historical_max > args.candidate_max_historical_commission
        ):
            continue
        candidate_rows.append(
            {
                "validator_name": name,
                "validator_address": address,
                "current_commission_percent": fmt_percent(active_commissions[address]),
                "indexed_eras_observed": len(observed_eras),
                "first_indexed_era": observed_eras[0],
                "last_indexed_era": observed_eras[-1],
                "max_indexed_commission_percent": fmt_percent(historical_max),
                "active_rpc_finalized_block": active_block_number,
                "nominated_by": ", ".join(
                    owner["label"]
                    for owner in accounts
                    if address in current_targets_by_owner[owner["address"]]
                ),
            }
        )
    candidate_rows.sort(
        key=lambda row: (
            Decimal(row["current_commission_percent"]),
            Decimal(row["max_indexed_commission_percent"]),
            -int(row["indexed_eras_observed"]),
            row["validator_name"].lower(),
        )
    )

    active_era_storage_hex = rpc(
        args.active_validators_rpc_url,
        args.user_agent,
        "state_getStorage",
        [singleton_storage_key("Staking", "ActiveEra"), active_block_hash],
        args.fallback_rpc_url,
    )
    if not active_era_storage_hex:
        raise RuntimeError("The active era was not returned by the RPC.")
    active_era = int.from_bytes(bytes.fromhex(active_era_storage_hex[2:])[:4], "little")
    era_key = active_era.to_bytes(4, "little")
    capacity_addresses = (
        set(active_validators)
        | all_current_targets
        | owned_validator_ids
        | {row["validator_address"] for row in candidate_rows}
    )
    exposure_keys = {
        address: double_storage_key_twox64(
            "Staking", "ErasStakers", era_key, ss58_decode(address)[0]
        )
        for address in capacity_addresses
    }
    exposure_storage = storage_values(
        args.active_validators_rpc_url,
        args.user_agent,
        list(exposure_keys.values()),
        active_block_hash,
        args.fallback_rpc_url,
    )
    exposures = {
        address: decode_staking_exposure(exposure_storage[key], address_prefix)
        for address, key in exposure_keys.items()
    }

    def capacity_values(address: str) -> dict[str, str]:
        exposure = exposures.get(address)
        if exposure is None:
            return {
                "active_era": str(active_era),
                "elected_active_era": "NO",
                "active_total_stake_xx": "",
                "active_own_stake_xx": "",
                "active_nominators": "",
                "smallest_active_nominator_xx": "",
            }
        return {
            "active_era": str(active_era),
            "elected_active_era": "YES",
            "active_total_stake_xx": fmt_xx(exposure["total"]),
            "active_own_stake_xx": fmt_xx(exposure["own"]),
            "active_nominators": str(exposure["active_nominators"]),
            "smallest_active_nominator_xx": (
                fmt_xx(exposure["smallest_nominator"])
                if exposure["smallest_nominator"] is not None
                else ""
            ),
        }

    for row in detail_rows:
        row.update(capacity_values(row["validator_address"]))
        exposure = exposures.get(row["validator_address"])
        active_amount = (
            exposure["nominator_stakes"].get(row["owner_address"])
            if exposure and row["source"] == "current_nomination_target"
            else None
        )
        row["selected_for_owner_active_era"] = (
            "YES" if active_amount is not None else "NO"
            if row["source"] == "current_nomination_target"
            else ""
        )
        row["owner_active_stake_xx"] = (
            fmt_xx(active_amount) if active_amount is not None else ""
        )
    for row in candidate_rows:
        row.update(capacity_values(row["validator_address"]))

    active_assignment_validator_ids = {
        validator_address
        for validator_address, exposure in exposures.items()
        if exposure
        and any(
            owner["address"] in exposure["nominator_stakes"] for owner in accounts
        )
    }
    missing_assignment_names = (
        active_assignment_validator_ids - set(validators_by_address)
    )
    if missing_assignment_names:
        result = graphql(
            args.api_url,
            args.user_agent,
            VALIDATOR_NAMES_QUERY,
            {"validatorIds": sorted(missing_assignment_names)},
        )
        validators_by_address.update(
            {
                record["stash_address"]: record
                for record in (result.get("validators") or [])
            }
        )
    active_assignment_rows: list[dict[str, str]] = []
    for owner in accounts:
        for validator_address in sorted(active_assignment_validator_ids):
            exposure = exposures[validator_address]
            amount = exposure["nominator_stakes"].get(owner["address"])
            if amount is None:
                continue
            commission = active_commissions.get(validator_address)
            high_commission = (
                commission is not None
                and commission > args.max_commission
            )
            active_assignment_rows.append(
                {
                    "owner_label": owner["label"],
                    "validator_address": validator_address,
                    "validator_name": identity_name(
                        validators_by_address.get(validator_address)
                    ),
                    "commission_percent": fmt_percent(commission),
                    "owner_active_stake_xx": fmt_xx(amount),
                    "active_total_stake_xx": fmt_xx(exposure["total"]),
                    "active_nominators": str(exposure["active_nominators"]),
                    "still_current_target": (
                        "YES"
                        if validator_address in current_targets_by_owner[owner["address"]]
                        else "NO"
                    ),
                    "alert": "YES" if high_commission else "NO",
                    "alert_reason": (
                        f"commission > {fmt_percent(args.max_commission)} %"
                        if high_commission
                        else ""
                    ),
                }
            )
    for row in summary_rows:
        row["active_nomination_targets"] = sum(
            1
            for assignment in active_assignment_rows
            if assignment["owner_label"] == row["label"]
        )

    prices = market_prices(args.price_url, args.user_agent)
    usd_per_xx = prices["usd"]
    cad_per_xx = prices["cad"]
    one_usd_in_xx = Decimal("1") / usd_per_xx
    daily_total_usd = all_accounts_daily_reward * usd_per_xx
    daily_total_cad = all_accounts_daily_reward * cad_per_xx

    details_by_source = {
        "current_nomination_target": "RPC nomination target",
        "reward_source_in_window": "Validator that paid during the window",
        "owned_validator": "Owned validator",
    }
    alert_rows = [
        row
        for row in detail_rows
        if row["source"] == "current_nomination_target" and row["alert"] == "YES"
    ]
    active_assignment_alert_rows = [
        row for row in active_assignment_rows if row["alert"] == "YES"
    ]
    previous_commissions = previous_state.get("current_nomination_commissions") or {}
    current_commissions_snapshot: dict[str, dict[str, Any]] = {}
    commission_increase_rows: list[dict[str, str]] = []
    for row in detail_rows:
        if row["source"] != "current_nomination_target":
            continue
        state_key = f"{row['owner_address']}|{row['validator_address']}"
        current_value = (
            None
            if row["commission_percent"] == "unknown"
            else Decimal(row["commission_percent"])
        )
        current_commissions_snapshot[state_key] = {
            "owner_label": row["owner_label"],
            "validator_address": row["validator_address"],
            "validator_name": row["validator_name"],
            "commission_percent": row["commission_percent"],
        }
        previous = previous_commissions.get(state_key) or {}
        previous_value_text = previous.get("commission_percent")
        if (
            current_value is not None
            and previous_value_text not in (None, "unknown")
            and current_value > Decimal(str(previous_value_text))
        ):
            commission_increase_rows.append(
                {
                    "owner_label": row["owner_label"],
                    "validator_name": row["validator_name"],
                    "validator_address": row["validator_address"],
                    "previous_percent": fmt_percent(previous_value_text),
                    "current_percent": row["commission_percent"],
                }
            )
    had_previous_commissions = bool(previous_commissions)

    latest_stats_rows = overview.get("latestValidatorStats") or []
    latest_stats = latest_stats_rows[0] if latest_stats_rows else None
    lag_eras = (
        end_era - int(latest_stats["era"])
        if latest_stats and latest_stats.get("era") is not None
        else None
    )
    report_lines = [
        "# xx Network Staking Report",
        "",
        f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Historical rewards source: {args.api_url}",
        f"Current nominations and commissions: {rpc_endpoint_label(args.rpc_url)} at finalized block #{block_number}",
        f"Active validators examined for candidates: {rpc_endpoint_label(args.active_validators_rpc_url)} at finalized block #{active_block_number}",
        f"Live validator capacity: exposure in era {active_era}",
        f"Reward window: eras {from_era} to {end_era} ({args.days} days/eras)",
        f"Maximum commission accepted without alert: <= {fmt_percent(args.max_commission)} %",
        "",
        "## Data Freshness",
        "",
        f"Latest observed reward: era {latest_reward['era']} ({fmt_timestamp(latest_reward.get('timestamp'))}).",
    ]
    if RPC_FAILOVER_EVENTS:
        failovers = sorted({(primary, fallback) for primary, fallback, _ in RPC_FAILOVER_EVENTS})
        report_lines.extend(
            [
                "",
                "RPC NOTE: the primary RPC failed during this run; "
                "the report automatically used the configured fallback RPC: "
                + ", ".join(f"{primary} -> {fallback}" for primary, fallback in failovers)
                + ".",
            ]
        )
    if latest_stats:
        report_lines.append(
            "Latest indexer validator statistic (not used for current commissions): "
            f"era {latest_stats['era']} ({fmt_timestamp(latest_stats.get('timestamp'))})."
        )
    if lag_eras is not None and lag_eras > 1:
        report_lines.extend(
            [
                "",
                "NOTE: indexer validator_stats lags behind rewards by "
                f"{lag_eras} eras. This report does not use the indexer for "
                "current nominations or commissions; those values come from the live RPC.",
            ]
        )

    report_lines.extend(
        [
            "",
            "## Average Rewards",
            "",
            md_table(
                [
                    "Account",
                    "Stake bonded XX",
                    "Total XX",
                    "XX/day",
                    "Events",
                    "Reward validators",
                    "RPC nomination targets",
                    "Active targets in era",
                    "Owned validator",
                ],
                (
                    [
                        row["label"],
                        row["bonded_xx"],
                        row["total_rewards_xx"],
                        row["average_rewards_xx_per_day"],
                        row["reward_events"],
                        row["reward_validators_in_window"],
                        row["current_nomination_targets"],
                        row["active_nomination_targets"],
                        row["is_validator"],
                    ]
                    for row in summary_rows
                ),
            )
            + "\n"
            + "| **TOTAL ALL ACCOUNTS** |  | "
            + f"**{fmt_xx(all_accounts_total_reward)}** | "
            + f"**{fmt_xx(all_accounts_daily_reward)}** |  |  |  |  |  |",
            "",
            "## Fiat Value Of Total Average",
            "",
            f"CoinGecko price updated at: {fmt_timestamp(prices['last_updated_at'] * 1000 if prices['last_updated_at'] else None)}.",
            "",
            md_table(
                ["Metric", "Value"],
                [
                    [
                        "Current price of 1 XX",
                        f"${fmt_market_price(usd_per_xx)} USD / CA${fmt_market_price(cad_per_xx)} CAD",
                    ],
                    ["Equivalent of 1 USD", f"{fmt_xx(one_usd_in_xx)} XX"],
                    [
                        "Average total across all accounts",
                        f"{fmt_xx(all_accounts_daily_reward)} XX/day",
                    ],
                    [
                        "Average total value in USD",
                        f"${fmt_fiat(daily_total_usd)} USD/day",
                    ],
                    [
                        "Average total value in CAD",
                        f"CA${fmt_fiat(daily_total_cad)} CAD/day",
                    ],
                ],
            ),
            "",
            "Fiat value is indicative and changes with the market price.",
            "",
            "## Commission Changes",
            "",
        ]
    )
    if not had_previous_commissions:
        report_lines.append(
            "Baseline state initialized: commission increases will be detected "
            "starting with the next execution."
        )
    elif commission_increase_rows:
        report_lines.append(
            md_table(
                ["Account", "Validator", "Address", "Previous %", "Current %"],
                (
                    [
                        row["owner_label"],
                        row["validator_name"],
                        row["validator_address"],
                        row["previous_percent"],
                        row["current_percent"],
                    ]
                    for row in commission_increase_rows
                ),
            )
        )
    else:
        report_lines.append(
            "No commission increase detected among current nomination targets "
            "since the previous execution."
        )
    report_lines.extend(
        [
            "",
            "## Nomination Alerts",
            "",
            "Commission is the share of rewards retained by the validator and is "
            "compared against the alert threshold. A target without a current "
            "`Staking.Validators` entry is also flagged as inactive / offline.",
            "Capacity columns use live exposure for the active era: a validator "
            "not elected in this era produces no reward for this era; active "
            "nominator count helps monitor the 256 rewarded-nominator limit.",
            "",
            "### Current Targets In Alert",
            "",
        ]
    )
    if alert_rows:
        report_lines.append(
            md_table(
                [
                    "Account",
                    "Source",
                    "Validator",
                    "Name",
                    "Commission %",
                    "Elected in era",
                    "Total stake XX",
                    "Active nominators",
                    "Active for account",
                    "Account stake XX",
                    "Reason",
                ],
                (
                    [
                        row["owner_label"],
                        details_by_source[row["source"]],
                        row["validator_address"],
                        row["validator_name"],
                        row["commission_percent"],
                        row["elected_active_era"],
                        row["active_total_stake_xx"],
                        row["active_nominators"],
                        row["selected_for_owner_active_era"],
                        row["owner_active_stake_xx"],
                        row["alert_reason"],
                    ]
                    for row in alert_rows
                ),
            )
        )
    else:
        report_lines.append(
            f"No commission above {fmt_percent(args.max_commission)} % "
            "and no inactive target was detected among current nominations."
        )
    report_lines.extend(["", f"### Active Assignments In Alert - Era {active_era}", ""])
    if active_assignment_alert_rows:
        report_lines.append(
            md_table(
                [
                    "Account",
                    "Active validator",
                    "Name",
                    "Commission %",
                    "Account stake XX",
                    "Total stake XX",
                    "Active nominators",
                    "Still current target",
                    "Reason",
                ],
                (
                    [
                        row["owner_label"],
                        row["validator_address"],
                        row["validator_name"],
                        row["commission_percent"],
                        row["owner_active_stake_xx"],
                        row["active_total_stake_xx"],
                        row["active_nominators"],
                        row["still_current_target"],
                        row["alert_reason"],
                    ]
                    for row in active_assignment_alert_rows
                ),
            )
        )
        report_lines.append(
            ""
        )
        report_lines.append(
            "These assignments may belong to a previous target list; a recent "
            "nomination change is applied only at the next era election."
        )
    else:
        report_lines.append(
            f"No active assignment with commission above "
            f"{fmt_percent(args.max_commission)} % was detected."
        )

    report_lines.extend(
        [
            "",
            "## Observed Low-Commission Candidates",
            "",
            "Criteria: currently active validator, excluding owned validators; "
            f"current commission <= {fmt_percent(args.candidate_max_commission)} %; "
            f"at least {args.candidate_min_history_eras} observed indexed eras; "
            "and no indexed historical commission "
            f"> {fmt_percent(args.candidate_max_historical_commission)} %. "
            "Bootnodes are excluded.",
            "",
        ]
    )
    if latest_stats:
        report_lines.extend(
            [
                "Important limitation: the explorer commission history ends at "
                f"era {latest_stats['era']}; current commission is checked on-chain, "
                "but missing eras between the two sources prevent a complete "
                "historical guarantee through today.",
                "",
            ]
        )
    if candidate_rows:
        report_lines.append(
            md_table(
                [
                    "Validator",
                    "Address",
                    "Current commission %",
                    "Elected in era",
                    "Total stake XX",
                    "Active nominators",
                    "Already nominated by",
                    "Observed eras",
                    "Indexed period",
                    "Indexed maximum %",
                ],
                (
                    [
                        row["validator_name"],
                        row["validator_address"],
                        row["current_commission_percent"],
                        row["elected_active_era"],
                        row["active_total_stake_xx"],
                        row["active_nominators"],
                        row["nominated_by"],
                        row["indexed_eras_observed"],
                        f"{row['first_indexed_era']} - {row['last_indexed_era']}",
                        row["max_indexed_commission_percent"],
                    ]
                    for row in candidate_rows
                ),
            )
        )
    else:
        report_lines.append("No candidate currently satisfies all of these criteria.")

    owned_validator_rows = [
        row for row in detail_rows if row["source"] == "owned_validator"
    ]
    report_lines.extend(["", "## Owned Validators", ""])
    if owned_validator_rows:
        report_lines.append(
            md_table(
                [
                    "Account",
                    "Validator",
                    "Name",
                    "Current commission %",
                    "Elected in era",
                    "Total stake XX",
                    "Active nominators",
                    "Alert",
                    "Reason",
                ],
                (
                    [
                        row["owner_label"],
                        row["validator_address"],
                        row["validator_name"],
                        row["commission_percent"],
                        row["elected_active_era"],
                        row["active_total_stake_xx"],
                        row["active_nominators"],
                        row["alert"],
                        row["alert_reason"],
                    ]
                    for row in owned_validator_rows
                ),
            )
        )
    else:
        report_lines.append("No owned validator was detected through the RPC.")

    report_lines.extend(
        ["", f"## Active Nominator Assignments - Era {active_era}", ""]
    )
    if active_assignment_rows:
        report_lines.append(
            md_table(
                [
                    "Account",
                    "Active validator",
                    "Name",
                    "Commission %",
                    "Account stake XX",
                    "Total stake XX",
                    "Active nominators",
                    "Still current target",
                ],
                (
                    [
                        row["owner_label"],
                        row["validator_address"],
                        row["validator_name"],
                        row["commission_percent"],
                        row["owner_active_stake_xx"],
                        row["active_total_stake_xx"],
                        row["active_nominators"],
                        row["still_current_target"],
                    ]
                    for row in active_assignment_rows
                ),
            )
        )
    else:
        report_lines.append("No active assignment was detected for configured nominators.")

    current_target_rows_without_alert = [
        row
        for row in detail_rows
        if row["source"] == "current_nomination_target" and row["alert"] == "NO"
    ]
    report_lines.extend(["", "## Current Nominations Without Alerts", ""])
    if current_target_rows_without_alert:
        report_lines.append(
            md_table(
                [
                    "Account",
                    "Validator",
                    "Name",
                    "Commission %",
                    "Elected in era",
                    "Total stake XX",
                    "Active nominators",
                    "Active for account",
                    "Account stake XX",
                ],
                (
                    [
                        row["owner_label"],
                        row["validator_address"],
                        row["validator_name"],
                        row["commission_percent"],
                        row["elected_active_era"],
                        row["active_total_stake_xx"],
                        row["active_nominators"],
                        row["selected_for_owner_active_era"],
                        row["owner_active_stake_xx"],
                    ]
                    for row in current_target_rows_without_alert
                ),
            )
        )
    else:
        report_lines.append("No alert-free current nomination was returned by the RPC.")
    report_lines.extend(
        [
            "",
            "The detailed CSV also includes validators that produced a reward "
            "during the window, even when they are not current nomination targets.",
            "An `unknown` commission for a nomination target triggers an alert: "
            "the address has no current `Staking.Validators` entry at the finalized "
            "block (inactive or offline for reward monitoring).",
            "",
        ]
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "rewards_summary.csv"
    detail_path = args.output_dir / "validator_commissions.csv"
    candidate_path = args.output_dir / "nomination_candidates.csv"
    report_path = args.output_dir / "report.md"
    write_csv(
        summary_path,
        [
            "label",
            "address",
            "era_from",
            "era_to",
            "days",
            "bonded_xx",
            "total_rewards_xx",
            "average_rewards_xx_per_day",
            "reward_events",
            "reward_validators_in_window",
            "current_nomination_targets",
            "active_nomination_targets",
            "is_validator",
            "own_commission_over_limit",
        ],
        summary_rows,
    )
    write_csv(
        detail_path,
        [
            "owner_label",
            "owner_address",
            "source",
            "validator_address",
            "validator_name",
            "commission_percent",
            "alert",
            "alert_reason",
            "commission_source",
            "rpc_finalized_block",
            "active_era",
            "elected_active_era",
            "active_total_stake_xx",
            "active_own_stake_xx",
            "active_nominators",
            "smallest_active_nominator_xx",
            "selected_for_owner_active_era",
            "owner_active_stake_xx",
        ],
        detail_rows,
    )
    write_csv(
        candidate_path,
        [
            "validator_name",
            "validator_address",
            "current_commission_percent",
            "indexed_eras_observed",
            "first_indexed_era",
            "last_indexed_era",
            "max_indexed_commission_percent",
            "active_rpc_finalized_block",
            "nominated_by",
            "active_era",
            "elected_active_era",
            "active_total_stake_xx",
            "active_own_stake_xx",
            "active_nominators",
            "smallest_active_nominator_xx",
        ],
        candidate_rows,
    )
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    action_lines: list[str] = []
    for row in commission_increase_rows:
        action_lines.append(
            f"Commission increased: {row['owner_label']} / "
            f"{row['validator_name'] or row['validator_address']} "
            f"{row['previous_percent']} % -> {row['current_percent']} %."
        )
    for row in alert_rows:
        action_lines.append(
            f"Current target in alert: {row['owner_label']} / "
            f"{row['validator_name'] or row['validator_address']} "
            f"({row['alert_reason']})."
        )
    for row in active_assignment_alert_rows:
        action_lines.append(
            f"Active assignment in alert: {row['owner_label']} / "
            f"{row['validator_name'] or row['validator_address']} "
            f"({row['commission_percent']} %, {row['owner_active_stake_xx']} XX exposed)."
        )
    should_send_email = args.email_test or (args.email_on_alert and bool(action_lines))
    if should_send_email:
        subject = (
            "[xx staking] Test email"
            if args.email_test and not action_lines
            else f"[xx staking] Action required - {len(action_lines)} alert(s)"
        )
        email_lines = [
            "Automated xx Network report",
            "",
            f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        if action_lines:
            email_lines.extend(["", "Actions to review:", *[f"- {line}" for line in action_lines]])
        else:
            email_lines.extend(["", "This is a test email. No active alerts."])
        email_lines.extend(["", f"Full report: {report_path}"])
        send_email(
            args.smtp_host,
            args.smtp_port,
            args.smtp_user,
            args.smtp_password_env,
            args.email_to,
            subject,
            "\n".join(email_lines),
        )
        print(f"Email sent to: {args.email_to}")

    save_state(
        monitor_state_path,
        {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "current_nomination_commissions": current_commissions_snapshot,
        },
    )
    print("\n".join(report_lines))
    print(f"CSV rewards: {summary_path}")
    print(f"CSV commissions: {detail_path}")
    print(f"Candidate CSV: {candidate_path}")
    print(f"Markdown report: {report_path}")
    print(f"Monitoring state: {monitor_state_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
