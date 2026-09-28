"""Release package contents must include the FAT provenance and logo terms."""

import hashlib
from pathlib import Path
import sys

from tools import package_j700


def test_package_includes_fatfs_provenance(tmp_path, monkeypatch):
    commit = package_j700.subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=package_j700.REPO, text=True
    ).strip()
    cfg = "\n".join((*package_j700.REQUIRED, "#define BUILTIN_LOGO aurora")) + "\n"
    for name in ("stage1", "stage2"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "BUILD-INFO.txt").write_text(
            f"kind: m1n1\ncommit: {commit}\ndirty-files: 0\n"
            "build_tag: package-test\n"
            "make-vars: RELEASE=1 CHAINLOADING=1 T8140_KIS_PROXY=1 BUILTIN_LOGO=aurora\n"
        )
        (folder / "build_cfg.h").write_text(cfg)
        (folder / "m1n1.bin").write_bytes(b"AURORA-S1-CFG01\0STACKBOT")
        (folder / "m1n1-raw.elf").write_bytes(b"test raw ELF")

    output = tmp_path / "package"
    monkeypatch.setattr(sys, "argv", [
        "package_j700.py", "--stage1-out", str(tmp_path / "stage1"),
        "--stage2-out", str(tmp_path / "stage2"), "--output", str(output),
    ])
    package_j700.main()

    source = package_j700.REPO / "rust/fatfs/PROVENANCE.md"
    packaged = output / "fatfs/PROVENANCE.md"
    assert packaged.read_bytes() == source.read_bytes()
    digest = hashlib.sha256(packaged.read_bytes()).hexdigest()
    assert f"{digest}  fatfs/PROVENANCE.md" in (output / "SHA256SUMS").read_text().splitlines()
    logo_license = package_j700.REPO / "3rdparty_licenses/LICENSE.AURORA-LOGO"
    assert (output / "3rdparty_licenses/LICENSE.AURORA-LOGO").read_bytes() == \
        logo_license.read_bytes()
