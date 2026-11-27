import hashlib
import io
import os
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import frappe
from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from frappe.utils.file_manager import get_file_path
from frappe.utils.password import get_encryption_key


LEGACY_MAGIC = b"FDMENC1\n"
MAGIC = b"FDMENC2\n"
BLOCK_SIZE = 1024 * 1024
_V2_HEADER_STRUCT = struct.Struct(">QI16s")  # plaintext_size, chunk_size, salt
_V2_CHUNK_LEN_STRUCT = struct.Struct(">I")
_V2_NONCE_SIZE = 12
_V2_TAG_SIZE = 16
INLINE_UNSAFE_EXTENSIONS = frozenset(
    {".svg", ".svgz", ".html", ".htm", ".xhtml", ".xml", ".js", ".mjs"}
)


def file_encryption_enabled():
    try:
        return bool(
            int(
                frappe.db.get_single_value(
                    "Document Management Settings",
                    "enable_file_encryption",
                )
                or 0
            )
        )
    except Exception:
        return False


def _legacy_cipher():
    return Fernet(get_encryption_key().encode("utf-8"))


def _derive_v2_key(salt):
    master_key = get_encryption_key().encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"fdmenc2-aes-256-gcm",
    ).derive(master_key)


def _v2_aad(plaintext_length, chunk_index, salt):
    return MAGIC + struct.pack(">QI", plaintext_length, chunk_index) + salt


def _read_magic(path):
    path = str(path or "")
    if not path or not os.path.isfile(path):
        return b""
    with open(path, "rb") as handle:
        return handle.read(len(MAGIC))


def is_encrypted_path(path):
    header = _read_magic(path)
    return header in (MAGIC, LEGACY_MAGIC)


def _decrypt_v2_stream(stream):
    header_bytes = stream.read(_V2_HEADER_STRUCT.size)
    if len(header_bytes) != _V2_HEADER_STRUCT.size:
        frappe.throw("Encrypted document header is corrupted.")
    expected_size, chunk_size, salt = _V2_HEADER_STRUCT.unpack(header_bytes)
    if chunk_size <= 0 or chunk_size > 64 * 1024 * 1024:
        frappe.throw("Encrypted document header is invalid.")

    aesgcm = AESGCM(_derive_v2_key(salt))
    chunk_index = 0
    total_decrypted = 0
    while True:
        len_bytes = stream.read(_V2_CHUNK_LEN_STRUCT.size)
        if not len_bytes:
            break
        if len(len_bytes) != _V2_CHUNK_LEN_STRUCT.size:
            frappe.throw("Encrypted document chunk framing is corrupted.")
        (ct_len,) = _V2_CHUNK_LEN_STRUCT.unpack(len_bytes)
        if ct_len < _V2_TAG_SIZE or ct_len > chunk_size + _V2_TAG_SIZE:
            frappe.throw("Encrypted document chunk length is invalid.")
        nonce = stream.read(_V2_NONCE_SIZE)
        ciphertext = stream.read(ct_len)
        if len(nonce) != _V2_NONCE_SIZE or len(ciphertext) != ct_len:
            frappe.throw("Encrypted document is truncated.")
        try:
            plaintext = aesgcm.decrypt(
                nonce,
                ciphertext,
                _v2_aad(expected_size, chunk_index, salt),
            )
        except InvalidTag:
            frappe.throw(
                "Document file could not be decrypted. Check the site's encryption_key."
            )
        total_decrypted += len(plaintext)
        chunk_index += 1
        yield plaintext

    if total_decrypted != expected_size:
        frappe.throw("Encrypted document is truncated or modified.")


def decrypt_bytes(data):
    if data.startswith(MAGIC):
        stream = io.BytesIO(data[len(MAGIC) :])
        return b"".join(_decrypt_v2_stream(stream))
    if data.startswith(LEGACY_MAGIC):
        try:
            return _legacy_cipher().decrypt(data[len(LEGACY_MAGIC) :])
        except InvalidToken:
            frappe.throw(
                "Document file could not be decrypted. Check the site's encryption_key."
            )
    return data


def iter_decrypted_chunks(path):
    with open(path, "rb") as handle:
        yield from iter_decrypted_stream(handle)


def iter_decrypted_stream(handle):
    """Read cleartext from one open file, including across an atomic file replacement."""
    magic = handle.read(len(MAGIC))
    if magic == MAGIC:
        yield from _decrypt_v2_stream(handle)
        return
    if magic == LEGACY_MAGIC:
        rest = handle.read()
        try:
            yield _legacy_cipher().decrypt(rest)
        except InvalidToken:
            frappe.throw(
                "Document file could not be decrypted. Check the site's encryption_key."
            )
        return
    if magic:
        yield magic
    while True:
        block = handle.read(BLOCK_SIZE)
        if not block:
            break
        yield block


def read_path_bytes(path):
    return b"".join(iter_decrypted_chunks(path))


def read_file_bytes(file_url):
    path = get_file_path(file_url)
    if not path or not os.path.isfile(path):
        frappe.throw(f"File is unavailable: {file_url}")
    return read_path_bytes(path)


def sha256_path(path):
    digest = hashlib.sha256()
    for block in iter_decrypted_chunks(path):
        digest.update(block)
    return digest.hexdigest()


def plaintext_size(path):
    with open(path, "rb") as handle:
        magic = handle.read(len(MAGIC))
        if magic == MAGIC:
            header_bytes = handle.read(_V2_HEADER_STRUCT.size)
            if len(header_bytes) != _V2_HEADER_STRUCT.size:
                frappe.throw("Encrypted document header is corrupted.")
            expected_size, _, _ = _V2_HEADER_STRUCT.unpack(header_bytes)
            return int(expected_size)
        if magic == LEGACY_MAGIC:
            return len(decrypt_bytes(magic + handle.read()))
    return os.path.getsize(path)


def encrypt_path_at_rest(path, *, known_plaintext=False):
    """Encrypt atomically. Content stores can identify cleartext by its expected digest."""
    if not file_encryption_enabled():
        return False
    path = Path(path)
    if not path.is_file() or (not known_plaintext and is_encrypted_path(path)):
        return False

    total_size = path.stat().st_size
    salt = os.urandom(16)
    aesgcm = AESGCM(_derive_v2_key(salt))
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".enc",
        dir=str(path.parent),
    )
    try:
        with open(path, "rb") as source, os.fdopen(fd, "wb") as target:
            target.write(MAGIC)
            target.write(_V2_HEADER_STRUCT.pack(total_size, BLOCK_SIZE, salt))
            chunk_index = 0
            written_plain = 0
            while True:
                block = source.read(BLOCK_SIZE)
                if not block:
                    break
                nonce = os.urandom(_V2_NONCE_SIZE)
                ciphertext = aesgcm.encrypt(
                    nonce,
                    block,
                    _v2_aad(total_size, chunk_index, salt),
                )
                target.write(_V2_CHUNK_LEN_STRUCT.pack(len(ciphertext)))
                target.write(nonce)
                target.write(ciphertext)
                written_plain += len(block)
                chunk_index += 1
            if written_plain != total_size:
                frappe.throw("Source file changed while encrypting.")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
        return True
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def encrypt_file_doc(file_doc):
    if not file_doc or not getattr(file_doc, "is_private", 0):
        return False
    path = get_file_path(file_doc.file_url)
    if not path:
        return False
    return encrypt_path_at_rest(path)


def encrypt_existing_documents(batch_size=500):
    """Retroactively encrypt all unencrypted private files attached to Document Versions."""
    if not file_encryption_enabled():
        return {"encrypted": 0, "skipped": 0, "failed": 0, "disabled": True}

    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": ["in", ["Document", "Document Version"]],
            "is_private": 1,
            "is_folder": 0,
        },
        fields=["name", "file_url", "is_private"],
        limit_page_length=0,
    )
    encrypted = 0
    skipped = 0
    failed = 0
    for file_row in files:
        file_url = getattr(file_row, "file_url", None) or (
            file_row.get("file_url") if isinstance(file_row, dict) else None
        )
        if not file_url:
            skipped += 1
            continue
        try:
            path = get_file_path(file_url)
            if not path or not os.path.isfile(path) or is_encrypted_path(path):
                skipped += 1
                continue
            if encrypt_path_at_rest(path):
                encrypted += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
            frappe.log_error(
                title="Document Retroactive Encryption Error",
                message=frappe.get_traceback(),
            )
    return {
        "encrypted": encrypted,
        "skipped": skipped,
        "failed": failed,
        "disabled": False,
    }


@frappe.whitelist()
def enqueue_encrypt_existing_documents():
    if "System Manager" not in frappe.get_roles():
        frappe.throw("System Manager role is required.", frappe.PermissionError)
    if not file_encryption_enabled():
        frappe.throw("Enable file encryption at rest in Document Management Settings first.")
    job = frappe.enqueue(
        "document_management.frappe_document_management.utils.file_crypto.encrypt_existing_documents",
        queue="long",
        timeout=7200,
        enqueue_after_commit=True,
    )
    return {"job_id": getattr(job, "id", None)}


@contextmanager
def decrypted_temp_file(file_url=None, suffix=None, path=None):
    path = path or get_file_path(file_url)
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    if not is_encrypted_path(path):
        yield path
        return

    suffix = suffix if suffix is not None else Path(path).suffix
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temp_path = handle.name
    try:
        for block in iter_decrypted_chunks(path):
            handle.write(block)
        handle.close()
        yield temp_path
    finally:
        try:
            handle.close()
        except OSError:
            pass
        try:
            os.remove(temp_path)
        except OSError:
            pass


def document_file_response(file_url, filename=None, attachment=False):
    path = get_file_path(file_url)
    if not path or not os.path.isfile(path):
        frappe.throw("File is unavailable.")
    resolved_filename = filename or os.path.basename(path)
    extension = os.path.splitext(resolved_filename)[1].lower()
    force_attachment = bool(attachment) or extension in INLINE_UNSAFE_EXTENSIONS
    frappe.local.response.filecontent = read_path_bytes(path)
    frappe.local.response.filename = resolved_filename
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = (
        "attachment" if force_attachment else "inline"
    )


def document_file_url(
    document,
    file_version="Original",
    attachment=False,
    version_name=None,
):
    args = {
        "document": document,
        "file_version": file_version,
    }
    if attachment:
        args["attachment"] = "1"
    if version_name:
        args["version_name"] = version_name
    query = "&".join(f"{key}={quote(str(value))}" for key, value in args.items())
    return (
        "/api/method/"
        "document_management.frappe_document_management.page."
        "document_management_console.document_management_console."
        f"download_document_file?{query}"
    )
