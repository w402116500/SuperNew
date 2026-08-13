import base64
import hashlib
import hmac
import os

PBKDF2_ROUNDS = int(os.getenv("PASSWORD_PBKDF2_ROUNDS", "310000"))


def verify_password(plain_password: str, password_hash: str) -> bool:
    if not plain_password or not password_hash:
        return False
    # 摘要字符串同时保存算法、迭代次数、salt 和结果，登录时才能按注册参数重新计算。
    if not password_hash.startswith("pbkdf2_sha256$"):
        return False

    try:
        _, rounds, salt_b64, digest_b64 = password_hash.split("$", 3)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        calculated = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            salt,
            int(rounds),
        )
        # 使用恒定时间比较，避免普通字符串比较泄露摘要匹配到哪一位。
        return hmac.compare_digest(calculated, expected)
    except (TypeError, ValueError):
        return False


def get_password_hash(password: str) -> str:
    if not password:
        raise ValueError("password is required")

    # 每次注册生成独立随机 salt，相同密码也不会得到相同摘要。
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ROUNDS,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt_b64}${digest_b64}"
