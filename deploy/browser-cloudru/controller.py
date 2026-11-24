"""Owner-console adapter for the existing installer. No public setup listener."""
import base64
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import https_setup
import setup


class InputError(ValueError):
    """Only messages authored for the owner console may be shown verbatim."""


def key_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise InputError("Проверьте идентификатор ключа.")
    return value


def unseal(payload, password):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    packed = base64.b64decode(payload.strip(), validate=True)
    if len(packed) > 8192 or len(packed) < 44:
        raise InputError("Повреждён блок настроек cloud-init.")
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), packed[:16], 600000, 32)
    try:
        data = AESGCM(key).decrypt(packed[16:28], packed[28:], b"pc-browser-input-v1")
    except InvalidTag:
        raise InputError("Проверьте пароль из локального помощника.") from None
    return json.loads(data)


def project_target(interfaces, project, instance_ids, addresses):
    if len(interfaces) != 1:
        raise InputError("Нужен отдельный проект с одним сетевым интерфейсом ВМ.")
    nic = interfaces[0]
    if (nic.get("project_id") != project or (nic.get("vm") or {}).get("id") not in instance_ids
            or nic.get("ip_address") not in addresses):
        raise InputError("Интерфейс проекта не совпал с идентификатором и внутренним IP этой ВМ.")
    groups = nic.get("security_groups") or []
    if len(groups) != 1:
        raise InputError("Назначьте интерфейсу одну выделенную группу безопасности.")
    ip = ipaddress.IPv4Address((nic.get("attach_external_ip") or {}).get("ip_address") or "")
    if not ip.is_global or ip.is_multicast:
        raise InputError("Подключите к интерфейсу публичный IPv4.")
    return {"project_id": project, "security_group_id": str(UUID(groups[0]["id"])), "origin_ip": str(ip)}


def discover(state, project):
    folder = state / "inventory"
    folder.mkdir(exist_ok=True, mode=0o700)
    shutil.copyfile(HERE / "inventory.tf", folder / "main.tf")
    setup.save(folder / "deployment.auto.tfvars.json", {"project_id": project})
    setup.terraform(state, ["init", "-input=false"], folder=folder, capture=True)
    setup.terraform(state, ["plan", "-input=false", "-out=inventory.tfplan"], folder=folder, capture=True)
    plan = json.loads(setup.terraform(state, ["show", "-json", "inventory.tfplan"], folder=folder, capture=True).stdout)
    nics = plan["planned_values"]["outputs"]["interfaces"]["value"]
    ids = {path.read_text().strip().lower() for path in (
        Path("/etc/pc-browser-vm-id"), Path("/var/lib/cloud/data/instance-id")) if path.is_file()}
    addrs = json.loads(subprocess.check_output(["ip", "-j", "-4", "address", "show"], text=True))
    return project_target(nics, project, ids, {a["local"] for nic in addrs for a in nic["addr_info"]})


def verify_bucket(backup):
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", endpoint_url=backup["endpoint"], region_name=backup["region"],
                      aws_access_key_id=backup["access_key"], aws_secret_access_key=backup["secret_key"])
    bucket = {"Bucket": backup["bucket"]}
    if s3.get_bucket_versioning(**bucket).get("Status") != "Enabled":
        raise InputError("Включите версионирование бакета в Cloud.ru.")
    grants = s3.get_bucket_acl(**bucket)["Grants"]
    if any(g["Grantee"].get("URI", "").endswith(("/AllUsers", "/AuthenticatedUsers")) for g in grants):
        raise InputError("Для копий нужен закрытый бакет.")
    try:
        s3.get_bucket_policy(**bucket)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
            raise
    else:
        policy = s3.get_bucket_policy_status(**bucket).get("PolicyStatus", {})
        if policy.get("IsPublic") is not False:
            raise InputError("Публичная или неподтверждённая политика бакета. Проверьте доступ к копиям.")
    s3.list_objects_v2(**bucket, MaxKeys=1)


def canonical_uuid(value, label):
    try:
        return str(UUID(value.strip()))
    except ValueError:
        raise InputError("Проверьте поле " + label + ": требуется UUID.") from None


def initialize(state, fields, password):
    project = canonical_uuid(fields["project"], "ID проекта")
    bucket = fields["bucket"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise InputError("Проверьте имя бакета.")
    install_key = key_id(fields["key_id"].strip())
    tenant, sep, s3_key = fields["s3_key"].strip().partition(":")
    if not sep:
        raise InputError("Введите S3 Access Key в формате tenant_id:key_id.")
    backup = {"bucket": bucket, "endpoint": "https://s3.cloud.ru", "region": "ru-central-1",
              "access_key": canonical_uuid(tenant, "Tenant ID") + ":" + key_id(s3_key), "secret_key": fields["s3_secret"]}
    if not backup["secret_key"] or not fields["secret"]:
        raise InputError("Заполните оба секретных поля.")
    if not password.isascii() or not password.isprintable() or password.strip() != password or len(password) < 20:
        raise InputError("Сохраните в менеджере паролей пароль копий из 20 или более печатных ASCII-символов.")
    intent = state / "browser-target.json"
    target = {"project": project, "bucket": bucket}
    if (state / "https-plan.json").exists() and setup.read(intent) != target:
        raise InputError("Эта установка уже привязана к другому проекту или бакету.")
    pw = state / "restic-password"
    if pw.exists():
        unlock(state, password)
    verify_bucket(backup)
    setup.save(intent, target)
    if not pw.exists():
        with pw.open("x", encoding="ascii") as handle:
            handle.write(password + "\n")
    setup.save(state / "credentials.json", {"AUTH_KEY_ID": install_key, "AUTH_SECRET": fields["secret"]})
    setup.save(state / "backup-config.json", backup)
    setup.prepare(state, "server")
    setup.configure(state, {"host": "127.0.0.1", "ssh_user": "pcowner", "project_id": project})
    setup.save(state / "browser-prepared.json", target)


def unlock(state, password):
    expected = (state / "restic-password").read_bytes().strip()
    if not hmac.compare_digest(expected, password.encode("utf-8")):
        raise InputError("Неверный пароль этой установки.")


def review(state, progress):
    project = setup.read(state / "browser-target.json")["project"]
    progress("Загрузка проверенных инструментов Terraform")
    setup.bootstrap(state)
    progress("Сверка ВМ, сетевого интерфейса и группы безопасности")
    config = discover(state, project)
    progress("Проверка плана HTTPS")
    plan = https_setup.plan(state, config)
    only_ingress(plan)
    return plan


def only_ingress(plan):
    expected = [{"resource": "cloudru_evolution_compute_security_group_rule.gateway_origin", "actions": ["create"]}]
    if plan["changes"] not in ([], expected):
        raise InputError("План меняет существующую инфраструктуру. Применение остановлено.")


def install(state, approval):
    plan = setup.read(state / "https-plan.json")
    only_ingress(plan)
    if not approval or not hmac.compare_digest(approval, plan["plan_sha256"]):
        raise InputError("Сначала подтвердите показанный план.")
    result = https_setup.install(state, approval)
    if not result["checks"]["public"]["unauthenticated_rejected"] or not result["recovery_kit"]["backup_verified"]:
        raise InputError("Не подтверждены защита публичного входа и восстановление из копии.")
    return result["mcp_url"]


def login(state, password):
    unlock(state, password)
    if not (state / "https-installed.json").is_file():
        raise InputError("Сначала завершите установку HTTPS.")
    receipt = https_setup.connection_receipt(state)
    handoff = https_setup.owner_login(state)
    data = setup.read(Path(handoff["private_file"]))
    expected = receipt["origin"] + "/login/alternative/code?redirect=/settings/account"
    if data["login_url"] != expected:
        raise InputError("Адрес входа отличается от адреса этой памяти.")
    return receipt["mcp_url"], expected, data["code"]
