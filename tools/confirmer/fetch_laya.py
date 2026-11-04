#!/usr/bin/env python3
"""Download Laya for AnyUse's optional Laya checks, one command, every file checked.

Laya (https://github.com/NandhaKishorM/laya) is a small open decision model by
Nandakishor M / Convai Innovations, licensed Apache-2.0. AnyUse does not ship its
weights (about 840 MB per checkpoint) and never downloads them on its own: the
confirmer, the model checks and the REA chooser run Laya in an offline container
from a local folder (``ANYUSE_CONFIRMER_LAYA_DIR`` or ``ANYUSE_REA_LAYA_DIR``).
This script fills that folder with exactly the files AnyUse was tested with, each
one checked against its SHA-256 below:

    DIR/wheels/               Laya 0.3.20 and its dependencies for the container
                              (Linux arm64, Python 3.12, CPU-only PyTorch)
    DIR/models/en/            convaiinnovations/laya
    DIR/models/typed/         convaiinnovations/laya-typed-decisions
    DIR/models/multilingual/  convaiinnovations/laya-multilingual

Run it from an AnyUse checkout:

    python tools/confirmer/fetch_laya.py                 # into ~/.anyuse/laya, about 2.7 GB
    python tools/confirmer/fetch_laya.py DIR --checkpoint en --checkpoint typed

``DIR`` defaults to ``laya`` in the AnyUse data directory (``~/.anyuse`` unless
``ANYUSE_DATA_DIR`` is set). Files already present with the right checksum are
kept, so an interrupted run can simply be started again. A file whose checksum
does not match is deleted and the script stops with an error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
PYPI_JSON = "https://pypi.org/pypi/{project}/{version}/json"
TORCH_CPU = "https://download.pytorch.org/whl/cpu/"
USER_AGENT = "anyuse-fetch-laya"

#: the five files each checkpoint needs (the same set ``execution/rea_laya.py`` checks)
CHECKPOINT_FILES = ("model.safetensors", "rl_agent_config.json", "encoder/config.json",
                    "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json")

#: checkpoint -> (Hugging Face repository, pinned revision, {file: sha256})
CHECKPOINTS = {
    "en": ("convaiinnovations/laya", "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851", {
        "model.safetensors": "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c",
        "rl_agent_config.json": "ae287b56bbcf5f8c4f4541ae9dfd00c914c4c48b940b8398c3058af37ba92bbd",
        "encoder/config.json": "bf3ab80598fdccf414855a2ce80f22859e4492d06ca8a62ddd1cfb63972f8979",
        "tokenizer/tokenizer.json": "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
        "tokenizer/tokenizer_config.json": "50044de60daaa73df97d262e15a40d4faf0160e7d742df64b377877a1320dd12",
    }),
    "typed": ("convaiinnovations/laya-typed-decisions", "1a793eb568e6718f15941d08f85432581df534e3", {
        "model.safetensors": "4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e",
        "rl_agent_config.json": "ebf0cd524d92342a6be5e48e9fca3d7c2babfb5a56ccd79d2171ef5d8c7f7be8",
        "encoder/config.json": "5268d24ad3b77c8151de5dcb0762ba4391619aad9ab0bda33e36fb083cfeae6d",
        "tokenizer/tokenizer.json": "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
        "tokenizer/tokenizer_config.json": "08d4cf3ac4dca381759441b85b91a6d40e688471dcd33d15d6649eb0a9a854d1",
    }),
    "multilingual": ("convaiinnovations/laya-multilingual", "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67", {
        "model.safetensors": "9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204",
        "rl_agent_config.json": "25061739243b617ad88d1219ba6f8a9c86c5881ca28df024fa2d9b3b2fcc30c6",
        "encoder/config.json": "83f6916d13ef0f556ac461f28308dc2bffa7ebeadee8ec9e2db5812020ea5bb4",
        "tokenizer/tokenizer.json": "609d8f4c067cd3950f88594c5a802616cea245823836ef5848ee4fc40aab5b6f",
        "tokenizer/tokenizer_config.json": "424b69444bf7b5809dc2cd2e36d0bd71b8055124dd24274d6db3c655d38205e7",
    }),
}

#: (PyPI project, version, wheel file, sha256); torch comes from PyTorch's CPU index
WHEELS = (
    ("annotated-doc", "0.0.5", "annotated_doc-0.0.5-py3-none-any.whl",
     "117bac03a25ede5df5440e855b32d556049ca169ead221505badf432fed4b101"),
    ("anyio", "4.15.1", "anyio-4.15.1-py3-none-any.whl",
     "6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101"),
    ("certifi", "2026.7.22", "certifi-2026.7.22-py3-none-any.whl",
     "62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775"),
    ("click", "8.5.0", "click-8.5.0-py3-none-any.whl",
     "255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360"),
    ("cuda-bindings", "13.4.3", "cuda_bindings-13.4.3-cp312-cp312-manylinux_2_24_aarch64.manylinux_2_28_aarch64.whl",
     "bfbd3f7d4ac04dd41dc49121b9e408c8283992f47124c2290ecb79bbbadcca8e"),
    ("cuda-pathfinder", "1.8.2", "cuda_pathfinder-1.8.2-py3-none-any.whl",
     "4e65059febdb4d19d5cbc4798677e19db2b582f2f702f457b609e571690d357e"),
    ("cuda-toolkit", "13.0.3.0", "cuda_toolkit-13.0.3.0-py2.py3-none-any.whl",
     "d693caaa261214ddd7dbb60d68e71cbed884e68c2be7509778f3051da0b91c3f"),
    ("filelock", "4.0.3", "filelock-4.0.3-py3-none-any.whl",
     "30cd166e2aee2c7534ce2c33c6367c4cd051b8368e20e2c4eb0c34f699bacfab"),
    ("fsspec", "2026.9.0", "fsspec-2026.9.0-py3-none-any.whl",
     "8dd6e646e99ea382bd85f97a45e6b526a442d79423a7dc673f1e2756d05fcb5f"),
    ("h11", "0.16.0", "h11-0.16.0-py3-none-any.whl",
     "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86"),
    ("hf-xet", "1.6.0", "hf_xet-1.6.0-cp38-abi3-manylinux_2_28_aarch64.whl",
     "0e6e21fa3cdfcdcd76748564bf593870a5e013f47d97cf10aed63aa222cff5b7"),
    ("httpcore", "1.0.9", "httpcore-1.0.9-py3-none-any.whl",
     "2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55"),
    ("httpx", "0.28.1", "httpx-0.28.1-py3-none-any.whl",
     "d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad"),
    ("huggingface-hub", "1.33.0", "huggingface_hub-1.33.0-py3-none-any.whl",
     "04e434b06e100eddbce9a6e817d72693a7884b10a79bd67ab48080d5c07eb899"),
    ("idna", "3.20", "idna-3.20-py3-none-any.whl",
     "ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c"),
    ("jinja2", "3.1.6", "jinja2-3.1.6-py3-none-any.whl",
     "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67"),
    ("laya", "0.3.20", "laya-0.3.20-py3-none-any.whl",
     "6039e802fa5effb8dd492061cd7ad39a43087beadc4a4fa4a649614e77eb83d4"),
    ("markdown-it-py", "4.2.0", "markdown_it_py-4.2.0-py3-none-any.whl",
     "9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a"),
    ("markupsafe", "3.0.3",
     "markupsafe-3.0.3-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl",
     "3a7e8ae81ae39e62a41ec302f972ba6ae23a5c5396c8e60113e9066ef893da0d"),
    ("mdurl", "0.1.2", "mdurl-0.1.2-py3-none-any.whl",
     "84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8"),
    ("mpmath", "1.3.0", "mpmath-1.3.0-py3-none-any.whl",
     "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c"),
    ("networkx", "3.7", "networkx-3.7-py3-none-any.whl",
     "e3fd2c13a7814cee3746340d8d7f8598a67f16a58bf47fb7f8793fab6efca1b0"),
    ("numpy", "2.5.3", "numpy-2.5.3-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
     "76c2c1e6bfa5c84adc6434dfbf013aa92096a7985221762c8f11fedfd20fff58"),
    ("packaging", "26.3", "packaging-26.3-py3-none-any.whl",
     "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"),
    ("pygments", "2.21.0", "pygments-2.21.0-py3-none-any.whl",
     "2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9"),
    ("pyyaml", "6.0.3",
     "pyyaml-6.0.3-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl",
     "9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28"),
    ("regex", "2026.9.10",
     "regex-2026.9.10-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl",
     "4c66d54042a14a503907d81861b8a5235e6d1f03d4fbc1d8767f652eaf957ac1"),
    ("rich", "15.0.0", "rich-15.0.0-py3-none-any.whl",
     "33bd4ef74232fb73fe9279a257718407f169c09b78a87ad3d296f548e27de0bb"),
    ("safetensors", "0.8.0", "safetensors-0.8.0-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
     "7a46e5ff292c356d6991e60942ba7f79817682d3a2cef0702136448cb9c4d235"),
    ("setuptools", "84.0.0", "setuptools-84.0.0-py3-none-any.whl",
     "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670"),
    ("shellingham", "1.5.4", "shellingham-1.5.4-py2.py3-none-any.whl",
     "7ecfff8f2fd72616f7481040475a65b2bf8af90a56c89140852d1120324e8686"),
    ("sympy", "1.14.0", "sympy-1.14.0-py3-none-any.whl",
     "e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5"),
    ("tokenizers", "0.23.2", "tokenizers-0.23.2-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
     "a37039b5dfc4af84eb3ef0a92f4307e28936c8f9adccba2629d36f652e9bf7a2"),
    ("torch", "2.14.0+cpu", "torch-2.14.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl",
     "da8140c3d4a41500d29710e35bf8e66a4295ec31026487c69f54a3f583310f8d"),
    ("tqdm", "4.70.1", "tqdm-4.70.1-py3-none-any.whl",
     "c293e525e6fef9c20e8728fd4612df02a0aa31bb5fe91ecd93e123b1b7bffa73"),
    ("transformers", "5.17.0", "transformers-5.17.0-py3-none-any.whl",
     "78ec1ce21579b38dfb83950a0658cd119f87212a2fcfdff478096ce9d6c03801"),
    ("typer", "0.27.2", "typer-0.27.2-py3-none-any.whl",
     "b3a5fc4342d5fc8fda8fc3010b1cf117e9249aab7fae800c2eff62fd3842d97d"),
    ("typing-extensions", "4.16.0", "typing_extensions-4.16.0-py3-none-any.whl",
     "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"),
)


class ChecksumError(RuntimeError):
    """A download did not match its pinned SHA-256."""


def default_dir() -> Path:
    """``laya`` in the AnyUse data directory (``paths.anyuse_data_dir``, read here
    without importing the package, so the script runs from a bare checkout)."""
    raw = os.environ.get("ANYUSE_DATA_DIR", "").strip()
    return (Path(raw).expanduser() if raw else Path.home() / ".anyuse") / "laya"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                  timeout=60)


def download(url: str, dest: Path, sha256: str, opener=_open) -> bool:
    """Writes ``url`` to ``dest`` unless ``dest`` already has ``sha256``. Returns
    True when it downloaded. A mismatch leaves no file behind and raises."""
    if dest.is_file() and sha256_of(dest) == sha256:
        return False
    if dest.is_file():                                   # a stale or corrupt copy never stays
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(url) as resp, open(part, "wb") as out:
            for block in iter(lambda: resp.read(1 << 20), b""):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != sha256:
            raise ChecksumError(f"{dest.name}: SHA-256 {digest.hexdigest()} is not the pinned {sha256}")
        os.replace(part, dest)
    finally:
        if part.exists():                                # a failed or wrong download leaves nothing
            part.unlink()
    return True


def checkpoint_url(repo: str, revision: str, name: str) -> str:
    return f"{HF}/{repo}/resolve/{revision}/{name}"


def wheel_url(project: str, version: str, filename: str, sha256: str, opener=_open) -> str:
    """Where the pinned wheel lives: PyTorch's CPU index for torch, else the URL
    PyPI lists for that exact file, after checking PyPI states the same SHA-256."""
    if project == "torch":
        return TORCH_CPU + urllib.parse.quote(filename)
    with opener(PYPI_JSON.format(project=project, version=version)) as resp:
        release = json.load(resp)
    for entry in release.get("urls", []):
        if entry.get("filename") == filename:
            if entry.get("digests", {}).get("sha256") != sha256:
                raise ChecksumError(f"{filename}: PyPI lists another SHA-256 than the pinned one")
            return entry["url"]
    raise FileNotFoundError(f"PyPI does not list {filename} for {project} {version}")


def fetch(target: Path, checkpoints=tuple(CHECKPOINTS), wheels: bool = True,
          opener=_open, log=print) -> dict:
    """Fills ``target`` and returns how many files were downloaded and kept."""
    unknown = [c for c in checkpoints if c not in CHECKPOINTS]
    if unknown:
        raise ValueError(f"unknown checkpoint(s): {', '.join(unknown)}")
    counts = {"downloaded": 0, "kept": 0}
    if wheels:
        for project, version, filename, sha in WHEELS:
            dest = target / "wheels" / filename
            if dest.is_file() and sha256_of(dest) == sha:
                counts["kept"] += 1
                continue
            log(f"wheel  {filename}")
            download(wheel_url(project, version, filename, sha, opener), dest, sha, opener)
            counts["downloaded"] += 1
    for name in checkpoints:
        repo, revision, files = CHECKPOINTS[name]
        for rel in CHECKPOINT_FILES:
            dest = target / "models" / name / rel
            if dest.is_file() and sha256_of(dest) == files[rel]:
                counts["kept"] += 1
                continue
            log(f"model  {name}/{rel}  ({repo}@{revision[:8]})")
            download(checkpoint_url(repo, revision, rel), dest, files[rel], opener)
            counts["downloaded"] += 1
    return counts


def main(argv=None, opener=_open) -> int:
    parser = argparse.ArgumentParser(
        description="Download Laya (Apache-2.0, Convai Innovations) for AnyUse's optional "
                    "Laya checks; every file is checked against a pinned SHA-256.")
    parser.add_argument("dir", nargs="?", type=Path, default=None,
                        help="target folder (default: laya in the AnyUse data directory)")
    parser.add_argument("--checkpoint", action="append", choices=sorted(CHECKPOINTS),
                        help="only this checkpoint (repeatable; default: all three)")
    parser.add_argument("--no-wheels", action="store_true",
                        help="skip the container's Python wheels")
    args = parser.parse_args(argv)
    target = (args.dir or default_dir()).expanduser().resolve()
    checkpoints = tuple(dict.fromkeys(args.checkpoint or CHECKPOINTS))
    try:
        counts = fetch(target, checkpoints, wheels=not args.no_wheels, opener=opener)
    except (OSError, ChecksumError, ValueError) as exc:
        print(f"fetch_laya: {exc}", file=sys.stderr)
        return 1
    print(f"{counts['downloaded']} downloaded, {counts['kept']} already there: {target}")
    print("Laya is by Nandakishor M / Convai Innovations, Apache-2.0: "
          "https://github.com/NandhaKishorM/laya")
    print("Point AnyUse at it, for example:")
    print(f"  export ANYUSE_CONFIRMER_LAYA_DIR={target}   # model checks and the confirmer")
    print(f"  export ANYUSE_REA_LAYA_DIR={target}         # the REA saved-action chooser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
