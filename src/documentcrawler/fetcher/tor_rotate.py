"""Tor control-port client: request a fresh circuit (new exit IP).

Implements the minimal slice of Tor's control protocol (v1) needed for
``SIGNAL NEWNYM`` — the programmatic equivalent of Tor Browser's
"New Identity" button. Authenticates with the control-port cookie Tor
writes next to its data directory when ``CookieAuthentication 1`` is set.
"""

from __future__ import annotations

import re
import socket

from documentcrawler.utils.logging import get_logger

log = get_logger(__name__)


def tor_newnym(
    control_host: str = "127.0.0.1",
    control_port: int = 9051,
    cookie_path: str | None = None,
    timeout_s: float = 4.0,
) -> bool:
    """Ask the local Tor daemon to build fresh circuits.

    Returns True when Tor acknowledged the signal. Best-effort by design:
    any failure (daemon down, auth mismatch, timeout) returns False —
    callers proceed with the current circuit.
    """
    try:
        with socket.create_connection((control_host, control_port), timeout=timeout_s) as sock:
            f = sock.makefile("rwb")

            # Tor is silent until asked: request the protocol banner.
            f.write(b"PROTOCOLINFO 1\r\n")
            f.flush()

            # Banner + AUTH lines; grab the cookie path if not supplied.
            cookie = cookie_path
            deadline_lines = 10
            while deadline_lines > 0:
                line = f.readline()
                if not line:
                    return False
                if cookie is None and b"COOKIEFILE" in line:
                    m = re.search(r'COOKIEFILE="([^"]*)"', line.decode("latin-1"))
                    if m and m.group(1):
                        cookie = m.group(1).replace("\\\\", "\\")
                if line.startswith(b"250 "):
                    break
                deadline_lines -= 1
            if not cookie:
                log.debug("tor control: no cookie file advertised; cannot authenticate")
                return False

            token = open(cookie, "rb").read().hex()
            f.write(f"AUTHENTICATE {token}\r\n".encode())
            f.flush()
            if not f.readline().startswith(b"250"):
                log.debug("tor control: authentication failed")
                return False

            f.write(b"SIGNAL NEWNYM\r\n")
            f.flush()
            ok = f.readline().startswith(b"250")
            if ok:
                log.debug("tor control: NEWNYM accepted — fresh circuits on the way")
            return ok
    except OSError as e:
        log.debug("tor control unreachable on %s:%s: %s", control_host, control_port, e)
        return False
