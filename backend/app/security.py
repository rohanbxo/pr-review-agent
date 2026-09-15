from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class TokenError(Exception):
    pass


def mint_jwt(user_id: int) -> tuple[str, datetime]:
    """Backend session token. Carries only the subject: role is read from the DB per request."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=s.jwt_ttl_seconds)
    token = jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": exp, "iss": s.jwt_issuer},
        s.jwt_secret,
        algorithm="HS256",
    )
    return token, exp


def decode_jwt(token: str) -> int:
    s = get_settings()
    try:
        claims = jwt.decode(
            token, s.jwt_secret, algorithms=["HS256"], issuer=s.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError) as e:
        raise TokenError(str(e)) from e


def _fernet() -> Fernet:
    s = get_settings()
    if not s.token_encryption_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    if s.token_encryption_key == s.jwt_secret:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must differ from JWT_SECRET")
    return Fernet(s.token_encryption_key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise TokenError("cannot decrypt stored token") from e
