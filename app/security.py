import hashlib


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_password(plain: str, stored_sha256: str) -> bool:
    return sha256_hex(plain) == stored_sha256
