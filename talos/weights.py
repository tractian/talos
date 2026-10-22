# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TRACTIAN
"""Student weights: where they are (`resolve`) and how to fetch them (`talos-pull`).

The gate's ONNX judge needs three files in one directory: `student.int8.onnx`, `vocab.json`, `heads.json`.
`talos-pull` downloads them from a Hugging Face model repo; `resolve()` is what the proxy uses to pick a
weights directory when none is given on the command line.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT_REPO = "tractian-ai/talos-student"
FILES = ("student.int8.onnx", "vocab.json", "heads.json")
ENV_WEIGHTS = "TALOS_WEIGHTS"
ENV_REPO = "TALOS_HF_REPO"


def has_weights(d: Path) -> bool:
    return d.is_dir() and all((d / f).exists() for f in FILES)


def resolve(path_or_none: str | None) -> str | None:
    """explicit path > $TALOS_WEIGHTS > ./weights (if it exists) > None. The explicit path is returned untouched even
    when it looks wrong, so the judge's own error names the missing file."""
    if path_or_none:
        return str(path_or_none)
    env = os.environ.get(ENV_WEIGHTS)
    if env:
        return env
    local = Path("weights")
    if has_weights(local):
        return str(local)
    return None


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_urllib(repo: str, name: str, dest: Path, revision: str = "main") -> Path:
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{name}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
    tmp.replace(dest)
    return dest


def pull(repo: str, dest: str | Path, revision: str = "main", force: bool = False, log=print) -> dict[str, str]:
    """Download FILES from the HF repo into `dest`; returns {name: sha256}. Uses huggingface_hub when installed
    (resumable, cached), else plain urllib."""
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        hf_hub_download = None
    out: dict[str, str] = {}
    for name in FILES:
        target = d / name
        if target.exists() and not force:
            log(f"[talos-pull] {name}: present, skipping (use --force to re-download)")
        elif hf_hub_download is not None:
            got = Path(hf_hub_download(repo_id=repo, filename=name, revision=revision, local_dir=str(d)))
            if got.resolve() != target.resolve():
                target.write_bytes(got.read_bytes())
            log(f"[talos-pull] {name}: downloaded via huggingface_hub")
        else:
            _download_urllib(repo, name, target, revision)
            log(f"[talos-pull] {name}: downloaded")
        out[name] = sha256_of(target)
        log(f"[talos-pull] {name}  sha256={out[name]}  bytes={target.stat().st_size}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="talos-pull", description="Download the Talos student weights from Hugging Face.")
    ap.add_argument("--repo", default=os.environ.get(ENV_REPO, DEFAULT_REPO), help=f"HF model repo (default ${ENV_REPO} or {DEFAULT_REPO})")
    ap.add_argument("--dest", default="weights", help="destination directory (default weights/)")
    ap.add_argument("--revision", default="main")
    ap.add_argument("--force", action="store_true", help="re-download files that are already present")
    a = ap.parse_args(argv)
    try:
        pull(a.repo, a.dest, revision=a.revision, force=a.force)
    except Exception as e:   # noqa: BLE001  (network errors of several kinds; one clear line is what the user needs)
        print(f"[talos-pull] failed: {e}", file=sys.stderr)
        return 1
    print(f"[talos-pull] weights ready in {a.dest}/ (talos-proxy --weights {a.dest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
