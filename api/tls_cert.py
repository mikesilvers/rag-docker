"""LAN mode's certificate step (#26): `python -m tls_cert`, the one-shot
`tls-init` service of docker-compose.lan.yml, run in the API image before the
proxy starts, on every start in LAN mode.

It leaves the pair nginx serves in the lan_certs volume (/certs here, read-only
/etc/nginx/certs in the proxy):

- Bring your own: when proxy/certs/cert.pem and proxy/certs/key.pem (/byo,
  read-only) both exist, they are used. cert.pem may hold a chain, server
  certificate first. Only one of the two, a file that doesn't parse or a key
  that doesn't match the certificate is refused; an expired certificate is
  used with a warning.
- Otherwise a self-signed certificate for RAG_LAN_HOSTNAMES (comma-separated
  DNS names and IP addresses) plus localhost, valid for 825 days, the most
  Apple platforms accept. It is reused on later starts and regenerated only
  when the active pair is missing or unreadable, has expired, names other
  hosts, or came from bring-your-own files that are gone.

Layout: /certs/live is a symlink to a pair-<random> folder holding cert.pem,
key.pem (mode 0600) and `source` (self-signed or byo). A new pair is written
into a fresh folder and `live` is switched with one rename, so nginx never
finds a certificate beside a key that doesn't match it; older pairs are then
removed.

The hosts the proxy answers for (#664): on every successful run the step also
writes /certs/hosts.map, which nginx.lan.conf includes in its map of accepted
hosts. It lists the active certificate's names other than localhost and
127.0.0.1 (nginx.lan.conf has those two already): DNS names in lower case,
wildcards (*.example.com) as nginx wildcards, IPv6 addresses in brackets. A
name that is none of these is left out, with a warning, so nothing else can
reach nginx's configuration. The file is replaced with one rename.

It prints one line naming the certificate (source, names, expiry, SHA-256
fingerprint, which is what a browser shows when asked to trust it) and exits
0, or prints one line on standard error and exits 1; the proxy then doesn't
start.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BYO_DIR = Path("/byo")
OUT_DIR = Path("/certs")
VALID_DAYS = 825
LIVE = "live"
SELF_SIGNED = "self-signed"
BYO = "byo"
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
HOSTS = "hosts.map"
# Accepted by nginx.lan.conf itself; a second entry for either would be a
# duplicate key, which stops nginx.
ALWAYS = ("localhost", "127.0.0.1")
# The map block's own parameter names: a key spelled like one needs a leading \.
MAP_WORDS = {"default", "hostnames", "include", "volatile"}


class CertError(Exception):
    """A reason the step can't produce a pair; one line for standard error."""


# ── names ─────────────────────────────────────────────────────────────────────

def _name(entry: str) -> str:
    """An IP address in its normal form, or a lowercased DNS name; else CertError."""
    try:
        return str(ipaddress.ip_address(entry))
    except ValueError:
        pass
    name = entry.lower()
    if len(name) > 253 or not all(_LABEL.match(label) for label in name.split(".")):
        raise CertError(f"RAG_LAN_HOSTNAMES: {entry!r} is neither a DNS name nor an IP address")
    return name


def wanted_names(raw: str | None) -> list[str]:
    """The certificate's names: the listed entries in order, then localhost, without duplicates."""
    entries = [entry.strip() for entry in (raw or "").split(",")]
    if not any(entries):
        raise CertError("RAG_LAN_HOSTNAMES is empty: set it to the names and addresses clients use to reach this "
                        "machine (comma-separated), or put your own cert.pem and key.pem in proxy/certs")
    names: list[str] = []
    for entry in entries:
        if not entry:
            raise CertError("RAG_LAN_HOSTNAMES has an empty entry")
        name = _name(entry)
        if name not in names:
            names.append(name)
    if "localhost" not in names:
        names.append("localhost")
    return names


def _cert_names(cert: x509.Certificate) -> set[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return ({name.lower() for name in san.get_values_for_type(x509.DNSName)}
            | {str(address) for address in san.get_values_for_type(x509.IPAddress)})


# ── reading and describing pairs ─────────────────────────────────────────────

def _public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_pair(cert_bytes: bytes, key_bytes: bytes):
    """(server certificate, chain) for a matching pair; CertError otherwise."""
    try:
        chain = x509.load_pem_x509_certificates(cert_bytes)
    except ValueError:
        raise CertError("cert.pem doesn't hold a PEM certificate") from None
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
    except (ValueError, TypeError):
        raise CertError("key.pem doesn't hold an unencrypted PEM private key") from None
    if _public(key.public_key()) != _public(chain[0].public_key()):
        raise CertError("key.pem doesn't match the first certificate in cert.pem")
    return chain[0], chain


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def describe(cert: x509.Certificate) -> str:
    names = ", ".join(sorted(_cert_names(cert))) or "none"
    expires = cert.not_valid_after_utc.strftime("%Y-%m-%d %H:%M UTC")
    return f"names {names}; expires {expires}; SHA-256 {fingerprint(cert)}"


def _read_live(out_dir: Path):
    """(source, cert bytes, key bytes) of the active pair, or None if it is missing or unreadable."""
    live = out_dir / LIVE
    try:
        return ((live / "source").read_text().strip(), (live / "cert.pem").read_bytes(),
                (live / "key.pem").read_bytes())
    except OSError:
        return None


# ── writing ──────────────────────────────────────────────────────────────────

def install(out_dir: Path, cert_bytes: bytes, key_bytes: bytes, source: str) -> None:
    """Write a new pair folder and switch `live` to it with one rename; then remove older pairs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pair = out_dir / f"pair-{secrets.token_hex(8)}"
    pair.mkdir(mode=0o700)
    (pair / "cert.pem").write_bytes(cert_bytes)
    fd = os.open(pair / "key.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_bytes)
    (pair / "source").write_text(source + "\n")
    link = out_dir / f".live-{secrets.token_hex(8)}"
    os.symlink(pair.name, link)
    os.replace(link, out_dir / LIVE)
    for old in out_dir.iterdir():
        if old.name.startswith("pair-") and old.name != pair.name:
            shutil.rmtree(old, ignore_errors=True)
        elif old.name.startswith(".live-"):
            old.unlink(missing_ok=True)


def self_signed(names: list[str], now: datetime.datetime) -> tuple[bytes, bytes]:
    """A new P-256 key and a self-signed server certificate for `names`, valid for 825 days from `now`.
    Not a CA certificate and without key usage, so OpenSSL-based clients and browsers accept it as
    its own trust anchor once trusted."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    alternatives = []
    for name in names:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alternatives.append(x509.DNSName(name))
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


# ── the hosts the proxy answers for (#664) ───────────────────────────────────

def _dns(name: str) -> bool:
    return len(name) <= 253 and all(_LABEL.match(label) for label in name.split("."))


def map_key(name: str) -> str | None:
    """`name` as a key of nginx's `map ... { hostnames; }`, or None if it can't be one safely."""
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return f"[{address}]" if address.version == 6 else str(address)
    if name.startswith("*.") and _dns(name[2:]):
        return name
    if not _dns(name):
        return None
    return "\\" + name if name in MAP_WORDS else name


def write_hosts(out_dir: Path, cert: x509.Certificate, err) -> None:
    """Write hosts.map for `cert`'s names, through a temporary file and one rename."""
    keys: list[str] = []
    for name in sorted(_cert_names(cert)):
        if name in ALWAYS:
            continue
        key = map_key(name)
        if key is None:
            print(f"tls-init: warning: {name!r} is left out of the names the proxy answers for: it is neither a "
                  f"DNS name nor an IP address", file=err, flush=True)
        elif key not in keys:
            keys.append(key)
    text = ("# Written by tls-init (api/tls_cert.py): the certificate's names besides localhost and 127.0.0.1.\n"
            + "".join(f"{key} 1;\n" for key in keys))
    temp = out_dir / f".hosts-{secrets.token_hex(8)}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(temp, 0o644)
    os.replace(temp, out_dir / HOSTS)
    for old in out_dir.iterdir():
        if old.name.startswith(".hosts-"):
            old.unlink(missing_ok=True)


# ── the step ─────────────────────────────────────────────────────────────────

def run(byo_dir: Path, out_dir: Path, hostnames: str | None, now: datetime.datetime | None = None,
        out=sys.stdout, err=sys.stderr) -> int:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        line = _step(byo_dir, out_dir, hostnames, now, err)
    except CertError as error:
        print(f"tls-init: {error}", file=err, flush=True)
        return 1
    except OSError as error:
        print(f"tls-init: could not read or write the certificate files: {error}", file=err, flush=True)
        return 1
    print(f"tls-init: {line}", file=out, flush=True)
    return 0


def _step(byo_dir: Path, out_dir: Path, hostnames: str | None, now: datetime.datetime, err) -> str:
    byo_cert, byo_key = byo_dir / "cert.pem", byo_dir / "key.pem"
    if byo_cert.exists() or byo_key.exists():
        if not (byo_cert.is_file() and byo_key.is_file()):
            raise CertError("proxy/certs needs both cert.pem and key.pem; only one of them is there")
        cert_bytes, key_bytes = byo_cert.read_bytes(), byo_key.read_bytes()
        cert, _ = load_pair(cert_bytes, key_bytes)
        if cert.not_valid_after_utc <= now:
            print(f"tls-init: warning: the certificate in proxy/certs expired on "
                  f"{cert.not_valid_after_utc:%Y-%m-%d}; browsers will refuse it", file=err, flush=True)
        if _read_live(out_dir) != (BYO, cert_bytes, key_bytes):
            install(out_dir, cert_bytes, key_bytes, BYO)
        write_hosts(out_dir, cert, err)
        return f"using your certificate from proxy/certs: {describe(cert)}"

    names = wanted_names(hostnames)
    current = _read_live(out_dir)
    if current and current[0] == SELF_SIGNED:
        try:
            cert, _ = load_pair(current[1], current[2])
        except CertError:
            cert = None
        if cert is not None and cert.not_valid_after_utc > now and _cert_names(cert) == set(names):
            write_hosts(out_dir, cert, err)
            return f"reusing the self-signed certificate: {describe(cert)}"
    cert_bytes, key_bytes = self_signed(names, now)
    install(out_dir, cert_bytes, key_bytes, SELF_SIGNED)
    cert, _ = load_pair(cert_bytes, key_bytes)
    write_hosts(out_dir, cert, err)
    return f"generated a self-signed certificate: {describe(cert)}"


if __name__ == "__main__":
    sys.exit(run(BYO_DIR, OUT_DIR, os.environ.get("RAG_LAN_HOSTNAMES")))
