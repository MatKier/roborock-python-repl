"""Passphrase encryption for the files in ~/.roborock-repl/ (login session and device cache).

scrypt turns the passphrase into a 256-bit key; AES-GCM encrypts and authenticates, so a wrong
passphrase (or a damaged file) fails the tag check instead of producing garbage.
Uses pycryptodome, which python-roborock already depends on."""
import base64
import os

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt

MIN_PASSPHRASE = 4
CACHE_MAGIC = b"RRENC1"  # prefix of an encrypted cache.pkl
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}  # ~0.3 s and 128 MB per derivation
NONCE_SIZE = 12


class WrongKey(Exception):
    """Wrong passphrase, or the data was changed or damaged."""


def new_kdf():
    """Parameters for a newly saved login: a fresh random salt every time."""
    return {"name": "scrypt", "salt": b64(os.urandom(16)), **SCRYPT}


def derive_key(passphrase, kdf):
    if kdf.get("name") != "scrypt":
        raise ValueError(f"Unknown key derivation {kdf.get('name')!r}")
    return scrypt(passphrase.encode(), unb64(kdf["salt"]), key_len=32, N=kdf["n"], r=kdf["r"], p=kdf["p"])


def encrypt(key, data, aad=b""):
    """nonce + ciphertext + tag. aad (e.g. a file header) is not encrypted but covered by the tag."""
    nonce = os.urandom(NONCE_SIZE)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return nonce + ciphertext + tag


def decrypt(key, blob, aad=b""):
    nonce, ciphertext, tag = blob[:NONCE_SIZE], blob[NONCE_SIZE:-16], blob[-16:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    try:
        return cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as e:  # tag mismatch
        raise WrongKey("wrong passphrase or damaged data") from e


def b64(data):
    return base64.b64encode(data).decode()


def unb64(text):
    return base64.b64decode(text)
