from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionError(RuntimeError):
    pass


class CredentialCipher:
    """Encrypt sender secrets with the environment-owned Fernet key."""

    def __init__(self, key: str):
        clean_key = key.strip()
        if not clean_key:
            raise CredentialEncryptionError(
                "Ключ шифрования почтовых паролей не настроен"
            )
        try:
            self._fernet = Fernet(clean_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise CredentialEncryptionError(
                "Ключ шифрования почтовых паролей имеет неверный формат"
            ) from exc

    def encrypt(self, password: str) -> str:
        if not password:
            raise CredentialEncryptionError("Пароль внешнего приложения обязателен")
        return self._fernet.encrypt(password.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_password: str | None) -> str:
        if not encrypted_password:
            raise CredentialEncryptionError("Пароль внешнего приложения не сохранён")
        try:
            return self._fernet.decrypt(encrypted_password.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise CredentialEncryptionError(
                "Сохранённый пароль не удалось расшифровать. Замените пароль ящика"
            ) from exc


def generate_encryption_key() -> str:
    """Return a new key for explicit local setup; callers decide where to store it."""
    return Fernet.generate_key().decode("ascii")
