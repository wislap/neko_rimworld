"""Immutable image artifacts and explicit, metered model input preparation.

References contain provenance, never filesystem paths or inline image bytes. Keeping
request/plan owners is independent of the bounded historical report collection.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .serialization import identity_digest

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_777_216
DEFAULT_MEDIA_CAPACITY_BYTES = 256 * 1024 * 1024


class MediaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if not isinstance(data, bytes) or not 45 <= len(data) <= MAX_IMAGE_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise MediaError("invalid_image", "A bounded PNG image is required.")
    offset, dimensions, image_data, ended = 8, None, False, False
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + size
        if end > len(data):
            raise MediaError("invalid_image", "PNG chunk exceeds the image bytes.")
        body = data[offset + 8 : offset + 8 + size]
        if zlib.crc32(kind + body) & 0xFFFFFFFF != struct.unpack_from(">I", data, end - 4)[0]:
            raise MediaError("invalid_image", "PNG chunk checksum does not match.")
        if dimensions is None:
            if kind != b"IHDR" or size != 13:
                raise MediaError("invalid_image", "PNG must start with its image header.")
            dimensions = struct.unpack_from(">II", body)
            if not all(dimensions) or dimensions[0] * dimensions[1] > MAX_IMAGE_PIXELS:
                raise MediaError("image_too_large", "PNG dimensions exceed the supported image area.")
        elif kind == b"IHDR":
            raise MediaError("invalid_image", "PNG contains a duplicate image header.")
        image_data |= kind == b"IDAT"
        offset = end
        if kind == b"IEND":
            ended = size == 0 and offset == len(data)
            break
    if not dimensions or not image_data or not ended:
        raise MediaError("invalid_image", "PNG image data or end marker is missing.")
    return dimensions


def _reference_identity(digest: str, metadata: Mapping[str, Any]) -> str:
    return identity_digest({"sha256": digest, "metadata": metadata})


def _digest(reference: Mapping[str, Any]) -> str:
    digest = reference.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or reference.get("media_id") != digest:
        raise MediaError("invalid_media_reference", "Use an exact content-addressed media reference.")
    metadata = reference.get("metadata")
    if not isinstance(metadata, dict):
        raise MediaError("invalid_media_reference", "Image provenance must be an object.")
    try:
        identity = _reference_identity(digest, metadata)
    except (TypeError, ValueError) as error:
        raise MediaError("invalid_media_reference", "Image provenance must be ordinary JSON.") from error
    if reference.get("reference_id") != identity or reference.get("evidence_id") != "image:" + identity:
        raise MediaError(
            "media_provenance_integrity", "Image provenance does not match its immutable reference identity."
        )
    return digest


class MediaStore:
    def __init__(self, root: Path | None = None, *, capacity_bytes: int = DEFAULT_MEDIA_CAPACITY_BYTES):
        if type(capacity_bytes) is not int or capacity_bytes <= 0:
            raise ValueError("Media capacity must be a positive byte count.")
        self.root = root.resolve() if root is not None else None
        self.capacity_bytes = capacity_bytes
        self._lock = threading.RLock()
        self._memory: dict[str, bytes] = {}
        self._owners: dict[str, dict[str, dict[str, Any]]] | None = {} if root is None else None

    def _path(self, digest: str) -> Path:
        assert self.root is not None
        path = self.root / (digest + ".png")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise MediaError("invalid_media_path", "Invalid media artifact path.")
        return path

    def _write(self, target: Path, data: bytes) -> None:
        assert self.root is not None
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load_owners(self) -> None:
        if self._owners is not None:
            return
        assert self.root is not None
        path = self.root / "owners.json"
        owners = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(owners, dict) or any(
            not isinstance(owner, str) or not isinstance(refs, dict) for owner, refs in owners.items()
        ):
            raise MediaError("invalid_media_index", "Media ownership index is invalid.")
        self._owners = owners

    def _save_owners(self) -> None:
        if self.root is not None:
            self._write(
                self.root / "owners.json",
                json.dumps(self._owners, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8"),
            )

    def put(
        self, data: bytes, *, metadata: Mapping[str, Any] | None = None, owner: str | None = None
    ) -> dict[str, Any]:
        width, height = _png_dimensions(data)
        encoded_metadata = json.dumps(dict(metadata or {}), ensure_ascii=False, allow_nan=False)
        if len(encoded_metadata.encode("utf-8")) > 32768:
            raise MediaError("invalid_media_metadata", "Image provenance exceeds its size limit.")
        digest = hashlib.sha256(data).hexdigest()
        source = json.loads(encoded_metadata)
        identity = _reference_identity(digest, source)
        reference = {
            "media_id": digest,
            "reference_id": identity,
            "evidence_id": "image:" + identity,
            "sha256": digest,
            "mime_type": "image/png",
            "byte_length": len(data),
            "width": width,
            "height": height,
            "metadata": source,
        }
        with self._lock:
            exists = digest in self._memory if self.root is None else self._path(digest).exists()
            if not exists:
                used = (
                    sum(map(len, self._memory.values()))
                    if self.root is None
                    else sum(p.stat().st_size for p in self.root.glob("*.png"))
                )
                if used + len(data) > self.capacity_bytes:
                    raise MediaError(
                        "media_capacity",
                        "Media storage capacity cannot retain another immutable image; release unused owners and collect unowned artifacts explicitly.",
                    )
            if self.root is None:
                self._memory.setdefault(digest, data)
            elif self._path(digest).exists():
                self.read(reference)
            else:
                self._write(self._path(digest), data)
            if owner is not None:
                self.retain(owner, [reference])
        return reference

    def read(self, reference: Mapping[str, Any]) -> bytes:
        digest = _digest(reference)
        with self._lock:
            try:
                if self.root is None:
                    data = self._memory[digest]
                else:
                    path = self._path(digest)
                    if path.stat().st_size > MAX_IMAGE_BYTES:
                        raise MediaError("image_too_large", "Media artifact exceeds its size limit.")
                    data = path.read_bytes()
            except (KeyError, FileNotFoundError) as error:
                raise MediaError("media_not_found", "The referenced image is not retained.") from error
            width, height = _png_dimensions(data)
            if hashlib.sha256(data).hexdigest() != digest or any(
                reference.get(key) != expected
                for key, expected in (
                    ("mime_type", "image/png"),
                    ("byte_length", len(data)),
                    ("width", width),
                    ("height", height),
                )
            ):
                raise MediaError("media_integrity", "Image bytes do not match their immutable reference.")
            return data

    def retain(self, owner: str, references: Sequence[Mapping[str, Any]]) -> None:
        if not isinstance(owner, str) or not owner or len(owner) > 512:
            raise MediaError("invalid_media_owner", "A bounded explicit retention owner is required.")
        with self._lock:
            for ref in references:
                self.read(ref)
            self._load_owners()
            assert self._owners is not None
            previous = copy.deepcopy(self._owners)
            for ref in references:
                self._owners.setdefault(owner, {})[ref["reference_id"]] = copy.deepcopy(dict(ref))
            try:
                self._save_owners()
            except BaseException:
                self._owners = previous
                raise

    def release(self, owner: str) -> None:
        """Release one owner only; reclamation is deliberately explicit."""
        with self._lock:
            self._load_owners()
            assert self._owners is not None
            previous = self._owners.pop(owner, None)
            if previous is None:
                return
            try:
                self._save_owners()
            except BaseException:
                if previous is not None:
                    self._owners[owner] = previous
                raise

    def references(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            self._load_owners()
            assert self._owners is not None
            return copy.deepcopy(list(self._owners.get(owner, {}).values()))

    def collect_unowned(self) -> list[str]:
        """Explicit reclamation never removes bytes retained by a plan or request."""
        with self._lock:
            self._load_owners()
            assert self._owners is not None
            retained = {
                reference["sha256"] for references in self._owners.values() for reference in references.values()
            }
            candidates = (
                set(self._memory)
                if self.root is None
                else {path.stem for path in self.root.glob("*.png") if re.fullmatch(r"[0-9a-f]{64}", path.stem)}
            )
            removed = sorted(candidates - retained)
            for digest in removed:
                if self.root is None:
                    del self._memory[digest]
                else:
                    self._path(digest).unlink()
            return removed


@dataclass(frozen=True, slots=True)
class ImageInputConfig:
    enabled: bool = False
    tokens_per_image: int = 0
    max_images: int = 4
    detail: str = "auto"

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.tokens_per_image) is not int or self.tokens_per_image < 0:
            raise ValueError("Image input requires explicit enablement and a nonnegative token reservation.")
        if self.enabled and self.tokens_per_image == 0:
            raise ValueError("Enabled image input requires an explicit positive per-image token reservation.")
        if (
            type(self.max_images) is not int
            or not 1 <= self.max_images <= 16
            or self.detail not in {"auto", "low", "high"}
        ):
            raise ValueError("Invalid image input limits or detail.")

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "status": "explicitly_enabled" if self.enabled else "pending_endpoint",
            "tokens_per_image": self.tokens_per_image,
            "max_images": self.max_images,
            "detail": self.detail,
            "provider_qualification": "pending",
        }


def prepare_model_messages(messages: Sequence[Mapping[str, Any]], store: MediaStore | None, config: ImageInputConfig):
    """Return wire messages, manifest and reserved image tokens without changing history."""
    wire, manifest = copy.deepcopy(list(messages)), []
    for message in wire:
        parts = message.get("content")
        if not isinstance(parts, list):
            continue
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                raise MediaError("invalid_media_input", "Message content parts must be objects.")
            if part.get("type") == "image_url":
                raise MediaError("invalid_media_input", "Image input requires a retained immutable media reference.")
            if part.get("type") != "neko_image_ref":
                continue
            if not config.enabled:
                raise MediaError(
                    "image_input_disabled", "This model endpoint has not been explicitly enabled for image input."
                )
            if store is None or message.get("role") != "user":
                raise MediaError(
                    "invalid_media_input", "Image references require a media store and user content parts."
                )
            if len(manifest) >= config.max_images:
                raise MediaError("image_input_limit", "The model request exceeds its image count limit.")
            ref = part.get("media")
            if not isinstance(ref, dict):
                raise MediaError("invalid_media_reference", "Missing immutable image reference.")
            data = store.read(ref)
            manifest.append(
                {**copy.deepcopy(ref), "input_tokens_reserved": config.tokens_per_image, "detail": config.detail}
            )
            parts[index] = {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
                    "detail": config.detail,
                },
            }
    return wire, manifest, len(manifest) * config.tokens_per_image
