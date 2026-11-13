"""Direct HTTPS Telegram transport. No listener, polling, proxy, or token in argv."""
from __future__ import annotations

import http.client
import json
import secrets
import ssl
import time
from pathlib import Path

from .config import ManagerError


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
        boundary = "pgbackup-" + secrets.token_hex(16)
        chunks = []
        for name, value in fields.items():
            chunks.append(("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, name, value)).encode())
        prefix = b"".join(chunks)
        if document:
            document = Path(document)
            filename = document.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
            prefix += ("--{}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{}\"\r\nContent-Type: application/octet-stream\r\n\r\n".format(boundary, filename)).encode()
        # File bytes need a trailing CRLF. Field prefixes already include it.
        suffix = (("\r\n" if document else "") + "--{}--\r\n".format(boundary)).encode()
        length = len(prefix) + len(suffix) + (document.stat().st_size if document else 0)
        # http.client opens a direct socket; HTTP(S)_PROXY environment variables
        # are not consulted. TLS verification uses the operating system CA store.
        connection = http.client.HTTPSConnection("api.telegram.org", timeout=120, context=ssl.create_default_context())
        try:
            connection.putrequest("POST", "/bot{}/{}".format(self.token, method))
            connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
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
            connection.send(suffix)
            response = connection.getresponse()
            data = response.read(2 * 1024 * 1024)
            return response.status, json.loads(data)
        finally:
            connection.close()

    def call(self, method, fields=None, document=None):
        for attempt in range(self.retries + 1):
            wait = min(2 ** (attempt + 1), 30)
            try:
                status, body = self.transport(method, fields or {}, document)
            except (OSError, http.client.HTTPException, ValueError) as exc:
                if attempt == self.retries:
                    raise ManagerError("Telegram connection failed. Check direct HTTPS access and retry the pending upload.") from exc
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
