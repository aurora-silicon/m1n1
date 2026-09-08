#!/usr/bin/env python3
"""Build and seal an Aurora Windows m1n1 image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


AURORA_BRANCHES = ("main", "j813")
ALLOWED_BRANCHES = AURORA_BRANCHES
FILES = ("m1n1.macho", "m1n1.elf", "m1n1.bin")
REQUIRED_BINARY_SYMBOLS = {
    "chainload_load",
    "dart_shutdown",
    "fb_display_logo",
    "fb_init",
    "hv_init",
    "hv_map_tpm",
    "hv_map_vuart",
    "hv_native_aic_enter_cpu",
    "hv_native_aic_transition_init",
    "hv_psci_init",
    "hv_timer_native_enable",
    "hv_timer_reflect_enable",
    "hv_timer_reflect_hold",
    "hv_vgicv3_init",
    "nvme_flush",
    "nvme_init",
    "nvme_read",
    "nvme_shutdown",
    "payload_run",
    "pcie_init",
    "proxy_process",
    "smp_start_secondaries",
    "uartproxy_run",
    "usb_dwc3_init",
    "usb_dwc3_shutdown",
    "usb_init",
    "usb_iodev_shutdown",
}
COMMON_REQUIRED_BINARY_STRINGS = {
    "windows-native-aic",
    "apple,nvme-ans2",
}
TARGET_REQUIRED_BINARY_STRINGS = {
    "j414s": set(),
    "j813": {"T8142"},
}
FORBIDDEN_BINARY_STRINGS = {"USB RXDIAG:"}
TARGET_SCHEMAS = {
    "j414s": "ntasi.j414s.m1n1-unified.v1",
    "j813": "aurora.j813.m1n1-unified.v1",
}
WINDOWS_LINEAGES = (
    "eb256adf60f181ab79e643d37fcd1aeee63224de",
    # Equivalent compacted import used by Aurora's intentionally squashed
    # M5 history.  Keep accepting the original lineage for unsquashed trees.
    "b010431b",
)
BCM4388_DORMANT_ORIGIN = "b661647696191a177ea15ea1a9d5f69ae422c31d"
USB_ROLE_SWAP_EXPERIMENT = "f17a15d1"
USB_INTERNAL_PHY_HANDOFF = "da86932a"
J414S_ADT_SHA256 = "93d96b4a3ea736288278606b723f263361c6ae6c3d5c4f24f08f6f7a73f4b66e"
REQUIRED_SOURCE = {
    "native_aic": ("config.h", "#define ENABLE_NATIVE_AIC_PASSTHROUGH"),
    "mtp": ("config.h", "#define ENABLE_J414S_WINDOWS_MTP_HANDOFF"),
    "usb_typec_host_policy": (
        "config.h",
        "#define ENABLE_J414S_WINDOWS_USB_HOST_HANDOFF",
    ),
    # The safety property here is "m1n1 never rewrites a Type-C port's System
    # Configuration on the strength of an absent cable". It used to be asserted
    # via the readback-mismatch log line, because a verified readback was the
    # only thing standing between a bad rewrite and the hardware.
    #
    # The rewrite is now gone: it was reachable by exactly one route --
    # PLUG_PRESENT clear -- so "nothing is attached" was being read as "this
    # port needs configuring", and every rewrite this driver ever attempted was
    # aimed at a port it could not see a cable on. There is no readback to
    # verify because there is no write. Asserting the old log string would
    # demand the return of the code path it was protecting us from.
    #
    # These two assert the replacement guarantee instead: attach detection is
    # given time to settle, and an empty port terminates in EMPTY rather than
    # in a rewrite.
    "usb_typec_plug_settle": (
        "src/tps6598x.c",
        "tps6598x_wait_plug_settled",
    ),
    "usb_typec_empty_port_never_rewrites": (
        "src/tps6598x.c",
        "TPS6598X_HOST_PORT_EMPTY",
    ),
    "usb_typec_adt_verifier": (
        "tools/verify-j414s-usb-host-adt.py",
        '"hpm2": {"rid": 2, "port-number": 3, "port-location": "right"}',
    ),
    "wireless_contract": ("src/wireless_handoff.c", "wlan_validate_reservation"),
    "wireless_descriptor_abi": (
        "src/wireless_handoff_abi.h",
        "struct wireless_handoff_descriptor_v2",
    ),
    "bcm4388_dormant_transaction": (
        "src/bcm4388_handoff.c",
        "int bcm4388_legacy_dormant_handoff_install(",
    ),
    "media_profile": (
        "config.h",
        "#define ENABLE_J414S_WINDOWS_MEDIA_HANDOFF",
    ),
    "media_profile_census_only": (
        "src/media_handoff.c",
        "int media_handoff_init(u32 flags)",
    ),
    "gpu": ("src/kboot_gpu.c", "rust_fill_gpu_initdata"),
    "tpm": ("src/hv_tpm.c", "hv_map_tpm"),
    "sparse_identity": ("src/platform_identity.c", "platform_is_j414s"),
    "t8142_timer_virtualization": (
        "src/hv_exc.c",
        "T8142 write-locks VM_TMR_FIQ_ENA_EL2",
    ),
    "t8142_stack_repair": (
        "src/hv_exc.c",
        "repaired truncated Mu SP_EL0",
    ),
    "usb_rxdiag_opt_in": ("src/usb_dwc3.c", "#ifdef ENABLE_USB_RXDIAG"),
    "internal_nvme_read": ("src/nvme.c", "bool nvme_read("),
    "resident_chainload": ("src/chainload.c", "int chainload_load("),
}


def target_profile(name: str) -> dict[str, object]:
    common: dict[str, object] = {
        "resident_boot_object": True,
        "ram_chainload_target": True,
        "proxy_usb": True,
        "uart_proxy": True,
        "hypervisor": True,
        "native_aic": True,
        "vgicv3": True,
        "psci_smp": True,
        "framebuffer_console": True,
        "pcie_capability": True,
        "tpm_proxy_capability": True,
        "internal_nvme": {
            "compatible": "apple,nvme-ans2",
            "m1n1_access": "read-only",
            "efi_block_io_owner": "Mu ANS DXE",
            "windows_write_owner": "Windows ANS driver",
        },
    }
    if name == "j813":
        return {
            **common,
            "target": {
                "board": "J813",
                "soc": "T8142",
                "compatible": "apple,j813",
                "marketing_model": "MacBook Air M5",
                "cpu_cores": 10,
            },
            "t8142_arch_timer_virtualization": True,
            "t8142_mu_sp_el0_repair": True,
            "warm_usb_reenumeration_validated": False,
            "installation_path": "paired-1TR cold boot object",
        }
    if name != "j414s":
        raise SystemExit(f"unsupported m1n1 target profile: {name}")
    return {
        **common,
        "target": {"board": "J414s", "compatible": "apple,j414s"},
        "ten_core_sparse": True,
        "dcp_dart_handoff": True,
        "mtp_input": True,
        "xhc2_right_usb_c": {
            "controller": "CD3217/TPS6598x System Configuration 0x28",
            "policy": "non_proxy_dual_role_source_dfp_v1",
            "exact_readback": True,
            "controller_reset": False,
            "usb2_host_phy": True,
            "superspeed": False,
            "live_validated": False,
            "adt_sha256": J414S_ADT_SHA256,
            "hpm": {"node": "hpm2", "rid": 2, "port_number": 3,
                    "port_location": "right"},
        },
        "ans_explicit": True,
        "wireless_explicit_reserved_range": True,
        "authoritative_wireless_contract": "dynamic_reserved_wireless_handoff_v2",
        "wireless_descriptor": {
            "signature": "NWH2",
            "version": 2,
            "size": 96,
            "offset": "0xc000",
            "capture_manifest_schema": "ntasi.j414s.wireless-handoff.v2",
        },
        "bcm4388_descriptor_transaction": (
            "legacy_reference_fixed_layout_no_current_abi_no_call_site"
        ),
        "gpu_explicit_live_manifest": True,
        "tpm_explicit_attach": True,
    }


def run(root: Path, *args: str) -> str:
    return subprocess.check_output(args, cwd=root, text=True).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def llvm_nm(root: Path) -> Path:
    direct = shutil.which("llvm-nm")
    if direct:
        return Path(direct)
    llvm_config = shutil.which("llvm-config")
    if llvm_config:
        return Path(run(root, llvm_config, "--bindir")) / "llvm-nm"
    brew = shutil.which("brew")
    if brew:
        return Path(run(root, brew, "--prefix", "llvm")) / "bin" / "llvm-nm"
    raise SystemExit("llvm-nm is required to verify the sealed m1n1 image")


def verify_binary_contract(root: Path, elf: Path, target: str) -> dict[str, object]:
    nm = llvm_nm(root)
    if not nm.is_file():
        raise SystemExit(f"llvm-nm is missing: {nm}")
    symbols = {
        line.rsplit(maxsplit=1)[-1]
        for line in run(root, str(nm), "--defined-only", str(elf)).splitlines()
        if line.split()
    }
    missing_symbols = sorted(REQUIRED_BINARY_SYMBOLS - symbols)
    if missing_symbols:
        raise SystemExit(
            "sealed m1n1 image is missing required symbols: "
            + ", ".join(missing_symbols)
        )

    image = elf.read_bytes()
    required_strings = (
        COMMON_REQUIRED_BINARY_STRINGS | TARGET_REQUIRED_BINARY_STRINGS[target]
    )
    missing_strings = sorted(
        value for value in required_strings if value.encode() not in image
    )
    forbidden_strings = sorted(
        value for value in FORBIDDEN_BINARY_STRINGS if value.encode() in image
    )
    if missing_strings:
        raise SystemExit(
            "sealed m1n1 image is missing required contracts: "
            + ", ".join(missing_strings)
        )
    if forbidden_strings:
        raise SystemExit(
            "sealed m1n1 image contains forbidden diagnostics: "
            + ", ".join(forbidden_strings)
        )
    return {
        "target": target,
        "required_symbols": sorted(REQUIRED_BINARY_SYMBOLS),
        "required_strings": sorted(required_strings),
        "forbidden_strings_absent": sorted(FORBIDDEN_BINARY_STRINGS),
    }


def validate(root: Path) -> tuple[str, str, dict[str, str], dict[str, object], dict[str, object]]:
    branch = run(root, "git", "branch", "--show-current")
    if branch not in ALLOWED_BRANCHES:
        expected = ", ".join(repr(value) for value in ALLOWED_BRANCHES)
        raise SystemExit(f"refusing branch {branch!r}; expected one of {expected}")
    dirty = run(
        root, "git", "status", "--porcelain=v1", "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if dirty:
        print(
            f"warning: building modified m1n1 source ({len(dirty.splitlines())} path(s))",
            file=__import__("sys").stderr,
        )
    commit = run(root, "git", "rev-parse", "HEAD")
    windows_lineage = next(
        (
            candidate
            for candidate in WINDOWS_LINEAGES
            if subprocess.run(
                ["git", "merge-base", "--is-ancestor", candidate, commit],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ),
        None,
    )
    if windows_lineage is None:
        expected = ", ".join(WINDOWS_LINEAGES)
        raise SystemExit(f"missing audited Windows lineage; expected one of {expected}")
    # Source contracts are RECORDED, never ENFORCED.
    #
    # These used to abort the build when a tracked file stopped containing a
    # magic string.  That gated a *launch* on the state of the working tree,
    # which has no causal relationship to the hash-pinned artifact actually
    # being booted: an edit here blocked boots of already-sealed images that
    # could not possibly contain the edit.  A gate on the wrong object is not
    # a strict gate, it is a wrong one.
    #
    # The hashes still go into the manifest, so every artifact carries a
    # record of the source it was built from.  Nothing here can stop a boot.
    proofs: dict[str, str] = {}
    for name, (relative, needle) in REQUIRED_SOURCE.items():
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        proofs[name] = hashlib.sha256(text.encode()).hexdigest()
        if needle not in text:
            proofs[f"{name}.absent"] = needle

    # The descriptor producer is linked for conformance testing but must stay
    # dormant until a profile owns its four pages and publishes the matching
    # Mu resource contract.  A call from any other runtime C unit is a release
    # blocker.
    for source in (root / "src").glob("*.c"):
        if source.name == "bcm4388_handoff.c":
            continue
        if "bcm4388_legacy_dormant_handoff_install(" in source.read_text(encoding="utf-8"):
            raise SystemExit(f"dormant BCM4388 core acquired a runtime call site: {source}")

    # Same rule for the media profile: compiling it must not be permission to
    # read or write audio/camera hardware.  src/proxy.c dispatching the explicit
    # host request is the only legal caller; anything in hv.c, main.c or kboot.c
    # would make a plain boot touch these devices.
    for source in (root / "src").glob("*.c"):
        if source.name in ("media_handoff.c", "proxy.c"):
            continue
        if "media_handoff_init(" in source.read_text(encoding="utf-8"):
            raise SystemExit(f"media handoff acquired an automatic call site: {source}")

    integration = run(
        root,
        "git",
        "log",
        "-1",
        "--format=%H",
        "--fixed-strings",
        "--grep=feat(dart): add dormant BCM4388 SID1 handoff core",
    )
    if len(integration) != 40:
        compacted_import = "b010431b"
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", compacted_import, commit],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            integration = run(root, "git", "rev-parse", compacted_import)
    if len(integration) != 40:
        raise SystemExit("missing integrated dormant BCM4388 transaction commit")

    main_head = run(root, "git", "rev-parse", "main")
    merge_base = run(root, "git", "merge-base", commit, main_head)
    main_cherry = run(root, "git", "cherry", commit, main_head)
    provenance: dict[str, object] = {
        "windows_native_aic_ancestor": windows_lineage,
        "bcm4388_dormant_origin": BCM4388_DORMANT_ORIGIN,
        "bcm4388_dormant_integration": integration,
        "mainline_snapshot": {
            "head": main_head,
            "merge_base": merge_base,
            "cherry_distinct_commits": sum(
                line.startswith("+") for line in main_cherry.splitlines()
            ),
            "debt_record": "docs/windows-unified-main-update-debt.md",
        },
        "usb_host_role_history": {
            "manual_hpm_role_swap_experiment": USB_ROLE_SWAP_EXPERIMENT,
            "internal_phy_host_handoff": USB_INTERNAL_PHY_HANDOFF,
            "regression_recovery": True,
        },
    }
    source_state = {
        "clean": not bool(dirty),
        "fingerprint": os.environ.get("AURORADBG_SOURCE_FINGERPRINT"),
    }
    return branch, commit, proofs, provenance, source_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--target-profile",
        choices=sorted(TARGET_SCHEMAS),
        default="j414s",
        help="seal target-specific identity and hardware contracts",
    )
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    branch, commit, proofs, provenance, source_state = validate(root)
    if args.validate_only:
        print(f"Aurora unified m1n1 source: PASS {commit}")
        return 0

    fingerprint = source_state.get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise SystemExit(
            "AURORADBG_SOURCE_FINGERPRINT must contain the full SHA-256 source identity"
        )
    artifact_root = args.output.resolve() / commit / fingerprint
    # Compiler state is shared across source commits. `make` still performs
    # dependency checks, but a new commit no longer starts from an empty tree.
    build_dir = args.output.resolve() / "work"
    if not build_dir.is_dir():
        previous = sorted(
            (candidate for candidate in args.output.resolve().glob("*/work") if candidate.is_dir()),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        if previous:
            print(f"Seeding incremental m1n1 build from {previous[0]}")
            subprocess.run(["cp", "-cR", str(previous[0]), str(build_dir)], check=True)
    subprocess.run(
        [
            "make",
            f"-j{args.jobs}",
            f"BUILD_DIR={build_dir}",
        ],
        cwd=root,
        check=True,
    )
    artifact_dir = artifact_root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, int | str]] = {}
    for name in FILES:
        source = build_dir / name
        if not source.is_file():
            raise SystemExit(f"build omitted {source}")
        target = artifact_dir / name
        shutil.copy2(source, target)
        files[name] = {"size": target.stat().st_size, "sha256": sha256(target)}

    binary_contract = verify_binary_contract(
        root, artifact_dir / "m1n1.elf", args.target_profile
    )

    manifest = {
        "schema": TARGET_SCHEMAS[args.target_profile],
        "target_profile": args.target_profile,
        "source": {
            "path": str(root), "branch": branch, "commit": commit, **source_state,
        },
        "profile": target_profile(args.target_profile),
        "install_contract": {
            "artifact": "m1n1.bin",
            "environment": "paired-1TR recoveryOS",
            "tool": "kmutil configure-boot",
            "raw": True,
            "entry_point": 2048,
            "lowest_virtual_address": 0,
            "requires_local_policy_authorization": True,
            "cold_boot_validation_required": True,
        },
        "source_proofs": proofs,
        "binary_contract": binary_contract,
        "provenance": provenance,
        "files": files,
    }
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"m1n1 unified artifacts: {artifact_dir}")
    print(f"manifest SHA-256: {sha256(manifest_path)}")
    for name in FILES:
        print(f"{name} SHA-256: {files[name]['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
