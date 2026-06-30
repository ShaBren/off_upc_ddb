"""Download the OpenFoodFacts Parquet database from Hugging Face.

Uses the ``huggingface_hub`` library which provides:

* **Metadata without download** – the dataset commit revision is fetched
  from the HF API in a single lightweight HTTP call.
* **Cached, resumable downloads** – ``hf_hub_download`` checks the local HF
  cache first and copies the verified file rather than re-downloading.
  Partial downloads are resumed automatically.
* **Built-in LFS verification** – the library verifies Git LFS integrity
  internally; no manual hash computation is needed or performed.

Safeguards
----------
* **Exists / up-to-date check** – queries the remote revision and compares
  it against the stored manifest.  If they match the file is skipped.
* **Update detection** – warns when the remote revision has changed.
* **Disk-space guard** – refuses to start if free space < 1.2× the expected
  file size (~9 GB).
* **Atomic write** – ``hf_hub_download`` downloads to a temporary location
  and moves on success; a failed download never clobbers a good file.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.utils import HfHubHTTPError, get_session

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ID = "openfoodfacts/product-database"
_FILENAME = "food.parquet"
_REPO_TYPE = "dataset"

# ~7.6 GB × 1.2 safety margin
_MIN_FREE_SPACE = int(7.6 * 1024**3 * 1.2)


# ---------------------------------------------------------------------------
# Remote metadata (lightweight — one API call, no download)
# ---------------------------------------------------------------------------


def _get_remote_revision() -> str | None:
    """Return the HEAD commit SHA of the dataset on Hugging Face."""
    try:
        resp = get_session().get(
            f"https://huggingface.co/api/datasets/{_REPO_ID}", timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("sha")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _manifest_path(parquet_path: str) -> Path:
    return Path(f"{parquet_path}.manifest.json")


def _read_manifest(parquet_path: str) -> dict | None:
    mp = _manifest_path(parquet_path)
    if not mp.is_file():
        return None
    try:
        return json.loads(mp.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_manifest(parquet_path: str, revision: str, size: int) -> None:
    _manifest_path(parquet_path).write_text(
        json.dumps(
            {
                "file": os.path.basename(parquet_path),
                "size": size,
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "revision": revision,
            },
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def download_db(target_path: str, *, force: bool = False) -> bool:
    """Download *food.parquet* from Hugging Face to *target_path*.

    Returns ``True`` if a download (or cache-copy) was performed,
    ``False`` if the existing file was already up-to-date.
    """
    target = Path(target_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # ── Fetch remote revision (no download) ──────────────────────────
    remote_rev = _get_remote_revision()
    if remote_rev:
        print(f"   Remote revision: {remote_rev[:12]}…")
    else:
        print("   ⚠  Could not fetch remote revision; will attempt download.")

    # ── Check if we already have a good copy ─────────────────────────
    manifest = _read_manifest(str(target))

    # File exists but was never registered (no manifest) — register it
    # without re-downloading so we don't waste 7.6 GB of bandwidth.
    if target.is_file() and not manifest and not force:
        size = target.stat().st_size
        _write_manifest(str(target), remote_rev or "unknown", size)
        print(f"✓ Registered existing file: {target}")
        print(f"   Size: {size / (1024**3):.2f} GB")
        if remote_rev:
            print(f"   Revision: {remote_rev[:12]}…")
        return True

    if target.is_file() and manifest and not force:
        stored_rev = manifest.get("revision")
        if remote_rev and stored_rev and remote_rev == stored_rev:
            print(f"✓ Already up-to-date (revision {remote_rev[:12]}…): {target}")
            return False
        if remote_rev and stored_rev and remote_rev != stored_rev:
            print(f"⚠  Update available!  Remote: {remote_rev[:12]}…  "
                  f"Local: {stored_rev[:12]}…")
            print("   Re-downloading…")
        elif not remote_rev:
            print("   Cannot verify currency — re-downloading to be safe.")

    # ── Disk-space guard ──────────────────────────────────────────────
    free = shutil.disk_usage(target.parent).free
    if free < _MIN_FREE_SPACE:
        free_gb = free / (1024**3)
        need_gb = _MIN_FREE_SPACE / (1024**3)
        raise OSError(
            f"Insufficient disk space: {free_gb:.1f} GB free, "
            f"need at least {need_gb:.1f} GB."
        )

    # ── Download via huggingface_hub ─────────────────────────────────
    # hf_hub_download handles: cache check, resume, LFS verification,
    # progress bar, and atomic write — all internally.
    label = "Downloading (--force)" if force else f"Downloading {_FILENAME} (~7.6 GB)"
    print(f"   {label} …")

    try:
        downloaded_path = hf_hub_download(
            repo_id=_REPO_ID,
            filename=_FILENAME,
            repo_type=_REPO_TYPE,
            local_dir=str(target.parent),
            local_dir_use_symlinks=False,
            force_download=force,
            resume_download=True,
        )
    except HfHubHTTPError as exc:
        raise RuntimeError(f"Download failed: {exc}") from exc

    downloaded = Path(downloaded_path)

    # If the downloaded file landed elsewhere, move it to the target.
    if downloaded.resolve() != target:
        shutil.move(str(downloaded), str(target))

    size = target.stat().st_size
    print(f"   Size: {size / (1024**3):.2f} GB")

    # ── Persist manifest ──────────────────────────────────────────────
    _write_manifest(str(target), remote_rev or "unknown", size)
    print(f"✓ Ready: {target}")
    return True


def status(target_path: str) -> dict:
    """Return a dict describing the current state of *target_path*.

    Queries the Hugging Face API for the remote revision (no download).
    """
    target = Path(target_path)
    manifest = _read_manifest(str(target))
    remote_rev = _get_remote_revision()

    result: dict = {
        "path": str(target),
        "exists": target.is_file(),
        "size": target.stat().st_size if target.is_file() else None,
    }

    if manifest:
        result["manifest"] = {
            "downloaded_at": manifest.get("downloaded_at"),
            "revision": manifest.get("revision"),
        }
    else:
        result["manifest"] = None

    if remote_rev:
        result["remote_revision"] = remote_rev
        if manifest and manifest.get("revision"):
            result["update_available"] = remote_rev != manifest["revision"]

    return result
