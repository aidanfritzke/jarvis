"""Generate a VAPID keypair for Web Push. Run ONCE.

    python -m jarvisagent.tools.gen_vapid

Writes `vapid_private.pem` in the current directory (put it in the server's secrets/ dir) and
prints the Application Server Key (the public key) to paste into config.yaml -> vapid.public_key.
Only needs the `cryptography` package (pulled in by pywebpush).
"""
from __future__ import annotations

import base64


def main() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with open("vapid_private.pem", "wb") as f:
        f.write(pem)

    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    app_server_key = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()

    print("Wrote vapid_private.pem  (copy to the server's secrets/ dir)")
    print("Application Server Key (paste into config.yaml -> vapid.public_key):")
    print(app_server_key)


if __name__ == "__main__":
    main()
