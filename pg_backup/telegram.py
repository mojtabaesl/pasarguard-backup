"""Direct HTTPS Telegram transport. No listener, polling, proxy, or token in argv."""
from __future__ import annotations

import http.client
import json
import secrets
import socket
import ssl
import time
from pathlib import Path

from .config import ManagerError


HOST = "api.telegram.org"
CONNECT_TIMEOUT = 10


class InvalidResponse(ValueError):
    def __init__(self, status, empty=False):
        self.status = status
        detail = "returned an empty response" if empty else "did not contain a valid Telegram JSON response"
        super().__init__("HTTP {} {}".format(status, detail))


def connection_detail(error):
    """Explain failures without exposing request URLs, tokens or response bodies."""
    if isinstance(error, socket.gaierror):
        return "DNS could not resolve api.telegram.org; check the server's DNS settings"
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed; check the server clock and installed CA certificates"
    if isinstance(error, ssl.SSLError):
        return "the TLS handshake failed; check the server's TLS and network connection"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "the connection timed out; check direct outbound HTTPS access to api.telegram.org:443"
    if isinstance(error, ConnectionRefusedError):
        return "the HTTPS connection was refused; check outbound access to api.telegram.org:443"
    if isinstance(error, (ConnectionResetError, http.client.RemoteDisconnected)):
        return "the remote connection was closed before a complete response arrived"
    if isinstance(error, InvalidResponse):
        return str(error)
    if isinstance(error, ValueError):
        return "the response could not be read as Telegram JSON"
    if isinstance(error, http.client.HTTPException):
        return "an incomplete or invalid HTTP response was received"
    if isinstance(error, FileNotFoundError):
        return "a required local file was not found"
    # OSError.strerror is independent of the URL/filename in the exception.
    if isinstance(error, OSError) and error.strerror:
        return "network/system error: " + error.strerror
    return "a network or system error occurred (" + type(error).__name__ + ")"


def connect_tls(context, family=socket.AF_UNSPEC):
    """Try direct addresses in IPv4-first order, with verified TLS and SNI."""
    addresses = socket.getaddrinfo(HOST, 443, family, socket.SOCK_STREAM)
    addresses.sort(key=lambda address: address[0] != socket.AF_INET)
    last_error = None
    seen = set()
    for address_family, kind, protocol, _, address in addresses:
        if (address_family, address) in seen:
            continue
        seen.add((address_family, address))
        raw = None
        try:
            raw = socket.socket(address_family, kind, protocol)
            raw.settimeout(CONNECT_TIMEOUT)
            raw.connect(address)
            return context.wrap_socket(raw, server_hostname=HOST)
        except ssl.SSLCertVerificationError:
            if raw is not None:
                raw.close()
            raise
        except OSError as error:
            if raw is not None:
                raw.close()
            last_error = error
    if last_error is not None:
        raise last_error
    raise socket.gaierror(socket.EAI_NONAME, "No address found")


class DirectHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, context, **kwargs):
        super().__init__(*args, context=context, **kwargs)
        self.tls_context = context

    def connect(self):
        self.sock = connect_tls(self.tls_context)
        self.sock.settimeout(self.timeout)


def diagnose_network(emit=print):
    """On-demand, credential-free DNS/TCP/TLS/HTTP checks for each IP family."""
    emit("Checking direct HTTPS to api.telegram.org; no bot token or messages are sent.")
    available = False
    for label, family in (("IPv4", socket.AF_INET), ("IPv6", socket.AF_INET6)):
        connection = None
        secure = None
        try:
            secure = connect_tls(ssl.create_default_context(), family)
            secure.settimeout(CONNECT_TIMEOUT)
            connection = http.client.HTTPConnection(HOST, timeout=CONNECT_TIMEOUT)
            connection.sock = secure
            connection.request("HEAD", "/", headers={"Connection": "close"})
            response = connection.getresponse()
            emit("{}: OK — DNS, TCP and TLS succeeded (HTTP {}).".format(label, response.status))
            available = True
        except (OSError, http.client.HTTPException) as error:
            emit("{}: {}.".format(label, connection_detail(error)))
        finally:
            if connection is not None:
                connection.close()
            elif secure is not None:
                secure.close()
    emit("The manager tries IPv4 first and falls back to IPv6. Channel permissions are checked separately.")
    return available


class Telegram:
    def __init__(self, config, transport=None, sleep=time.sleep):
        self.token = config["bot_token"]
        self.channel = config["channel_id"]
        self.retries = config["retries"]
        self.transport = transport or self._https
        self.sleep = sleep
        if not self.token or not self.channel:
            raise ManagerError("Telegram is not configured. Open Telegram channel settings once.")

    def _https(self, method, fields, document=None):
        # JSON is accepted by Telegram for requests without a file, including
        # getMe with no parameters. Multipart is used only for real file uploads.
        prefix = json.dumps(fields).encode("utf-8")
        suffix = b""
        content_type = "application/json"
        if document:
            boundary = "pgbackup-" + secrets.token_hex(16)
            chunks = []
            for name, value in fields.items():
                chunks.append(("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, name, value)).encode())
            document = Path(document)
            filename = document.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
            prefix = b"".join(chunks)
            prefix += ("--{}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{}\"\r\nContent-Type: application/octet-stream\r\n\r\n".format(boundary, filename)).encode()
            suffix = ("\r\n--{}--\r\n".format(boundary)).encode()
            content_type = "multipart/form-data; boundary=" + boundary
        length = len(prefix) + len(suffix) + (document.stat().st_size if document else 0)
        # Direct sockets ignore HTTP(S)_PROXY. Certificate verification remains on.
        connection = DirectHTTPSConnection(HOST, timeout=120, context=ssl.create_default_context())
        try:
            connection.putrequest("POST", "/bot{}/{}".format(self.token, method))
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(length))
            connection.endheaders()
            connection.send(prefix)
            if document:
                with document.open("rb") as stream:
                    while True:
                        chunk = stream.read(256 * 1024)
                        if not chunk:
                            break
                        connection.send(chunk)
            if suffix:
                connection.send(suffix)
            response = connection.getresponse()
            data = response.read(2 * 1024 * 1024)
            if not data:
                raise InvalidResponse(response.status, empty=True)
            try:
                body = json.loads(data)
            except ValueError as error:
                raise InvalidResponse(response.status) from error
            return response.status, body
        finally:
            connection.close()

    def call(self, method, fields=None, document=None):
        for attempt in range(self.retries + 1):
            wait = min(2 ** (attempt + 1), 30)
            try:
                status, body = self.transport(method, fields or {}, document)
                if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
                    raise InvalidResponse(status)
            except (OSError, http.client.HTTPException, ValueError) as error:
                permanent_response = isinstance(error, InvalidResponse) and 400 <= error.status < 500 and error.status != 429
                if attempt == self.retries or permanent_response or isinstance(error, (ssl.SSLCertVerificationError, FileNotFoundError)):
                    detail = connection_detail(error).replace(self.token, "[token hidden]")
                    raise ManagerError("Telegram request failed during {}: {}. Check the connection and try again.".format(method, detail)) from error
            else:
                if status == 200 and body.get("ok") is True:
                    return body.get("result", {})
                transient = status == 429 or status >= 500 or body.get("error_code") == 429
                description = str(body.get("description", "HTTP {}".format(status))).replace(self.token, "[token hidden]")
                if not transient or attempt == self.retries:
                    raise ManagerError("Telegram: " + description)
                if status == 429 or body.get("error_code") == 429:
                    wait = max(1, int(body.get("parameters", {}).get("retry_after", wait)))
            self.sleep(wait)
        raise ManagerError("Telegram request failed.")

    def validate_channel(self):
        bot = self.call("getMe")
        chat = self.call("getChat", {"chat_id": self.channel})
        if chat.get("type") != "channel":
            raise ManagerError("The destination must be a Telegram channel. This destination is not a channel.")
        member = self.call("getChatMember", {"chat_id": chat["id"], "user_id": bot["id"]})
        if member.get("status") != "creator" and not (member.get("status") == "administrator" and member.get("can_post_messages")):
            raise ManagerError("Add the bot to your channel as an administrator with Post Messages permission.")
        return {"channel_id": str(chat["id"]), "channel_title": chat.get("title", "Telegram channel")}

    def document(self, path, caption=""):
        result = self.call("sendDocument", {"chat_id": self.channel, "caption": caption[:1000]}, path)
        if not isinstance(result.get("message_id"), int):
            raise ManagerError("Telegram did not confirm the document. The upload remains pending.")
        return result["message_id"]
