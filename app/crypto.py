from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretCipher:
    """Encrypt persisted Playerok credentials using a key derived from BOT_TOKEN.

    This keeps BOT_TOKEN and DATABASE_URL as the only required environment
    variables while avoiding plaintext Playerok cookies/proxies in PostgreSQL.
    Changing BOT_TOKEN makes previously encrypted secrets unreadable.
    """

    def __init__(self, bot_token: str):
        digest = hashlib.sha256(("playerok-bot:v1:" + bot_token).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError(
                "Cannot decrypt stored credentials. Was BOT_TOKEN changed?"
            ) from exc
