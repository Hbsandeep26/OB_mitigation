from __future__ import annotations

import json
import socket
import ssl
from datetime import datetime
from typing import Any, Dict

import requests

import config


def _check_dns(host: str) -> Dict[str, Any]:
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, 443)})
        return {"ok": True, "addresses": addresses}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _check_tls(host: str) -> Dict[str, Any]:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                return {
                    "ok": True,
                    "tls_version": tls_sock.version(),
                    "cipher": tls_sock.cipher()[0],
                }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _check_settings() -> Dict[str, Any]:
    token = config.get_dhan_access_token()
    client_id = config.get_dhan_client_id()
    return {
        "broker": config.get_active_broker(),
        "has_dhan_client_id": bool(client_id),
        "has_dhan_access_token": bool(token),
        "has_dhan_pin": bool(config._setting("DHAN_PIN", "")),
        "has_dhan_totp_secret": bool(config._setting("DHAN_TOTP_SECRET", "")),
    }


def _check_fundlimit() -> Dict[str, Any]:
    token = config.get_dhan_access_token()
    client_id = config.get_dhan_client_id()
    if not token or not client_id:
        return {"ok": False, "error": "Missing DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID"}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
        "dhanClientId": client_id,
    }
    try:
        response = requests.get("https://api.dhan.co/v2/fundlimit", headers=headers, timeout=10)
        return {
            "ok": response.status_code == 200,
            "status_code": response.status_code,
            "body": (response.text or "")[:1000],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_diagnostics() -> Dict[str, Any]:
    host = "api.dhan.co"
    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "settings": _check_settings(),
        "dns": _check_dns(host),
        "tls": _check_tls(host),
        "fundlimit": _check_fundlimit(),
    }


if __name__ == "__main__":
    print(json.dumps(run_diagnostics(), indent=2))
