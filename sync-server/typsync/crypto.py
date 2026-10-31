"""Encryption used by TypendruckerGram Sync (see docs/sync/protocol.md).

Events and media are sealed to the owner's X25519 public key:

    blob = 0x01 | eph_pub(32) | nonce(12) | ciphertext | tag(16)
    key  = HKDF-SHA256(ikm=X25519(eph, owner_pub),
                       salt=eph_pub || owner_pub, info="typsync v1 event")
    AES-256-GCM(key, nonce, aad="typsync")

Pairing payloads are encrypted with a key derived from a short pair code.
"""
from __future__ import annotations

import hashlib
import os
import secrets

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
KEY_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
HEADER_SIZE = 1 + KEY_SIZE + NONCE_SIZE
AAD = b"typsync"
EVENT_INFO = b"typsync v1 event"
PAIR_SALT = b"typsync pair"
PAIR_INFO = b"typsync v1 pair"
PAIR_LOOKUP_PREFIX = b"typsync pair lookup|"

# Crockford base32: no I, L, O, U.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_GROUPS = 3
CODE_GROUP_SIZE = 4


class CryptoError(ValueError):
    """Blob or code cannot be decrypted (wrong key, corrupted or tampered)."""


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _raw_private(key: X25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption())


def _hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_SIZE, salt=salt, info=info,
    ).derive(ikm)


def generate_keypair() -> tuple[bytes, bytes]:
    """Returns (private, public), 32 raw bytes each."""
    private = X25519PrivateKey.generate()
    return _raw_private(private), _raw_public(private.public_key())


def public_from_private(private: bytes) -> bytes:
    return _raw_public(X25519PrivateKey.from_private_bytes(private).public_key())


def seal(owner_public: bytes, plaintext: bytes, *,
         ephemeral_private: bytes | None = None,
         nonce: bytes | None = None) -> bytes:
    """Encrypts for the owner. Fixed ephemeral key and nonce are for test vectors only."""
    if len(owner_public) != KEY_SIZE:
        raise ValueError("owner public key must be 32 bytes")
    eph = (X25519PrivateKey.from_private_bytes(ephemeral_private)
           if ephemeral_private is not None else X25519PrivateKey.generate())
    eph_pub = _raw_public(eph.public_key())
    shared = eph.exchange(X25519PublicKey.from_public_bytes(owner_public))
    key = _hkdf(shared, eph_pub + owner_public, EVENT_INFO)
    nonce = nonce if nonce is not None else os.urandom(NONCE_SIZE)
    if len(nonce) != NONCE_SIZE:
        raise ValueError("nonce must be 12 bytes")
    return bytes([VERSION]) + eph_pub + nonce + AESGCM(key).encrypt(nonce, plaintext, AAD)


def open_sealed(owner_private: bytes, blob: bytes) -> bytes:
    if len(blob) < HEADER_SIZE + TAG_SIZE or blob[0] != VERSION:
        raise CryptoError("unsupported or truncated blob")
    eph_pub = blob[1:1 + KEY_SIZE]
    nonce = blob[1 + KEY_SIZE:HEADER_SIZE]
    private = X25519PrivateKey.from_private_bytes(owner_private)
    owner_public = _raw_public(private.public_key())
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(eph_pub))
        key = _hkdf(shared, eph_pub + owner_public, EVENT_INFO)
        return AESGCM(key).decrypt(nonce, blob[HEADER_SIZE:], AAD)
    except Exception as e:  # InvalidTag, bad point
        raise CryptoError("cannot decrypt blob") from e


def new_pair_code() -> str:
    """Random 60-bit code formatted as XXXX-XXXX-XXXX."""
    chars = "".join(secrets.choice(CODE_ALPHABET)
                    for _ in range(CODE_GROUPS * CODE_GROUP_SIZE))
    return "-".join(chars[i:i + CODE_GROUP_SIZE]
                    for i in range(0, len(chars), CODE_GROUP_SIZE))


def normalize_pair_code(code: str) -> str:
    """Uppercases, drops separators and maps look-alike letters (O→0, I/L→1)."""
    out = []
    for ch in code.upper():
        if ch in "- \t":
            continue
        ch = {"O": "0", "I": "1", "L": "1"}.get(ch, ch)
        if ch not in CODE_ALPHABET:
            raise CryptoError(f"invalid character in pair code: {ch!r}")
        out.append(ch)
    if len(out) != CODE_GROUPS * CODE_GROUP_SIZE:
        raise CryptoError("pair code must have 12 characters")
    return "".join(out)


def pair_lookup_hash(code: str) -> str:
    """What the relay stores to find a pairing; reveals nothing usable without the code."""
    return hashlib.sha256(
        PAIR_LOOKUP_PREFIX + normalize_pair_code(code).encode()).hexdigest()


def _pair_key(code: str) -> bytes:
    return _hkdf(normalize_pair_code(code).encode(), PAIR_SALT, PAIR_INFO)


def pair_encrypt(code: str, payload: bytes, *, nonce: bytes | None = None) -> bytes:
    """nonce(12) | ciphertext | tag(16)."""
    nonce = nonce if nonce is not None else os.urandom(NONCE_SIZE)
    return nonce + AESGCM(_pair_key(code)).encrypt(nonce, payload, AAD)


def pair_decrypt(code: str, data: bytes) -> bytes:
    if len(data) < NONCE_SIZE + TAG_SIZE:
        raise CryptoError("truncated pairing payload")
    try:
        return AESGCM(_pair_key(code)).decrypt(data[:NONCE_SIZE], data[NONCE_SIZE:], AAD)
    except Exception as e:
        raise CryptoError("wrong pair code or corrupted payload") from e
