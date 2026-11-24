"""Export the existing verified recovery kit in a browser-decryptable envelope."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

import controller as c


def seal(folder, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("kit.json", "s3-config.json", "restic-password", "README.txt"):
            archive.writestr(name, (folder / name).read_bytes())
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, 600000, 32)
    ciphertext = AESGCM(key).encrypt(nonce, contents.getvalue(), b"pc-browser-kit-v1")
    return b"pc-browser-kit-v1\n" + base64.b64encode(salt + nonce + ciphertext)


def export(state, password):
    import boto3
    c.unlock(state, password)
    receipt = c.setup.read(state / "recovery-kit-receipt.json")
    workspace = c.setup.read(state / "https-installed.json")["workspace_id"]
    if not receipt["backup_verified"] or receipt["workspace_id"] != workspace:
        raise c.InputError("Проверенный комплект для этой памяти отсутствует.")
    folder = Path(receipt["private_directory"]).resolve()
    if folder.parent != state.resolve():
        raise c.InputError("Комплект находится за пределами установки.")
    config = c.setup.read(state / "backup-config.json")
    client = boto3.client("s3", endpoint_url=config["endpoint"], region_name=config["region"],
                          aws_access_key_id=config["access_key"], aws_secret_access_key=config["secret_key"])
    name = f"recovery/{workspace}/browser-kit.enc"
    data = seal(folder, password)
    args = {"Bucket": config["bucket"], "Key": name}
    client.put_object(**args, Body=data)
    with client.get_object(**args)["Body"] as body:
        if body.read(len(data) + 1) != data:
            raise c.InputError("Не удалось прочитать и сверить комплект в S3.")
    return name
