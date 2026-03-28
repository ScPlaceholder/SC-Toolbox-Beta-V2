"""
Dev-only tool: encrypts the extension payload into data/ext.dat.
DO NOT ship this file. Add to .gitignore.

Usage:
    python build_ext.py
"""
import base64
import hashlib
import json
import os
import sys

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

SALT = b"trade_hub_ext_v1"
ITERATIONS = 100_000
OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "ext.dat")

PAYLOAD = {
    "title": "C.A.B.A.L. Online",
    "subtitle": "Covert Analytical Business & Logistics",
    "colors": {
        "bg":      "#0a0a0a",
        "bg2":     "#1a0000",
        "panel":   "#111111",
        "accent":  "#cc0000",
        "accent2": "#ff2020",
        "fg":      "#cccccc",
        "fg2":     "#888888",
        "fg3":     "#ffffff",
        "sel":     "#330000",
        "border":  "#440000",
        "btn":     "#1a0808",
        "btn_active": "#2a0000",
    },
    "modes": [
        {
            "id": "standard",
            "name": "STANDARD",
            "desc": "Default profit calculation  (margin \u00d7 SCU)",
            "params": {},
        },
        {
            "id": "monte_carlo",
            "name": "MONTE CARLO",
            "desc": "Scenario simulation \u2014 price drift, inventory fluctuation, rare cargo loss",
            "params": {
                "iterations": 500,
                "price_noise_sell": 0.12,
                "price_noise_buy": 0.03,
                "inventory_drop_rate": 0.15,
                "inventory_drop_min": 0.2,
                "inventory_drop_max": 0.8,
                "cargo_loss_rate": 0.005,
            },
        },
        {
            "id": "risk_adjusted",
            "name": "RISK-ADJUSTED",
            "desc": "1-in-50 disaster average \u2014 amortized profit after total cargo loss events",
            "params": {
                "disaster_frequency": 50,
                "loss_multiplier": 1.0,
            },
        },
        {
            "id": "multi_hop",
            "name": "MULTI-HOP OPTIMIZER",
            "desc": "Rotating cargo \u2014 best profit-per-SCU/distance at each stop (switch to LOOPS tab)",
            "params": {
                "max_steps": 3,
                "score_metric": "profit_per_distance",
            },
        },
    ],
}


def derive_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=ITERATIONS,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def build(passphrase: str) -> None:
    fernet_key = derive_key(passphrase)
    f = Fernet(fernet_key)

    plaintext = json.dumps(PAYLOAD, separators=(",", ":")).encode("utf-8")
    token = f.encrypt(plaintext)

    # Also store the SHA-256 digest of the passphrase so ext_loader can gate
    digest = hashlib.sha256(passphrase.encode("utf-8")).hexdigest()

    # File format: hex-digest (64 chars) + newline + Fernet token
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "wb") as fp:
        fp.write(digest.encode("ascii"))
        fp.write(b"\n")
        fp.write(token)

    print(f"[+] Wrote {os.path.getsize(OUT_PATH)} bytes -> {OUT_PATH}")
    print(f"[+] SHA-256 digest: {digest}")


if __name__ == "__main__":
    phrase = sys.argv[1] if len(sys.argv) > 1 else input("Passphrase: ")
    build(phrase.strip())
