from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WindowsUnifiedContractTests(unittest.TestCase):
    def test_default_image_contains_safe_capabilities(self) -> None:
        config = (ROOT / "config.h").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("#define ENABLE_NATIVE_AIC_PASSTHROUGH", config)
        self.assertIn("#define ENABLE_J414S_WINDOWS_MTP_HANDOFF", config)
        self.assertIn("#define ENABLE_J414S_WINDOWS_WIRELESS_HANDOFF", config)
        self.assertIn("#define ENABLE_J414S_WINDOWS_USB_HOST_HANDOFF", config)
        for obj in (
            "mtp_handoff.o",
            "wireless_handoff.o",
            "bcm4388_handoff.o",
            "hv_tpm.o",
            "kboot_gpu.o",
            "tps6598x_host_policy.o",
        ):
            self.assertIn(obj, makefile)

    def test_descriptor_handoff_core_is_linked_but_dormant(self) -> None:
        core = (ROOT / "src/bcm4388_handoff.c").read_text(encoding="utf-8")
        legacy_api = "bcm4388_legacy_dormant_handoff_install("
        self.assertIn(f"int {legacy_api}", core)
        for source in (ROOT / "src").glob("*.c"):
            if source.name != "bcm4388_handoff.c":
                self.assertNotIn(
                    legacy_api,
                    source.read_text(encoding="utf-8"),
                    source.name,
                )
        proxy = (ROOT / "proxyclient/m1n1/proxy.py").read_text(encoding="utf-8")
        self.assertNotIn("legacy_dormant_handoff", proxy)

    def test_mutating_capabilities_are_explicit(self) -> None:
        proxy = (ROOT / "proxyclient/m1n1/proxy.py").read_text(encoding="utf-8")
        wireless = (ROOT / "src/wireless_handoff.c").read_text(encoding="utf-8")
        self.assertIn("def wireless_handoff_init(self, reservation_base=None", proxy)
        self.assertIn("def top_of_memory_alloc(self, size)", proxy)
        self.assertNotIn("0x10022000000ULL", wireless)
        self.assertIn("base < guest_top + SZ_16K", wireless)
        self.assertIn("base + size > physical_top", wireless)

    def test_builder_is_source_addressed_and_fail_closed(self) -> None:
        builder = (ROOT / "tools/build-j414s-windows-unified.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('AURORA_BRANCHES = ("main", "staging")', builder)
        self.assertIn('"schema": "ntasi.j414s.m1n1-unified.v1"', builder)
        self.assertIn(
            "artifact_root = args.output.resolve() / commit / fingerprint", builder
        )
        self.assertIn("len(fingerprint) != 64", builder)
        self.assertIn('artifact_dir = artifact_root / "artifacts"', builder)
        self.assertIn('f"BUILD_DIR={build_dir}"', builder)
        self.assertIn("warning: building modified m1n1 source", builder)
        self.assertIn("BCM4388_DORMANT_ORIGIN", builder)
        self.assertIn(
            '"authoritative_wireless_contract": "dynamic_reserved_wireless_handoff_v2"',
            builder,
        )
        self.assertIn("legacy_reference_fixed_layout_no_current_abi_no_call_site", builder)
        self.assertIn('"mainline_snapshot"', builder)
        self.assertIn('USB_ROLE_SWAP_EXPERIMENT = "f17a15d1"', builder)
        self.assertIn('USB_INTERNAL_PHY_HANDOFF = "da86932a"', builder)
        self.assertIn(
            'J414S_ADT_SHA256 = "93d96b4a3ea736288278606b723f263361c6ae6c3d5c4f24f08f6f7a73f4b66e"',
            builder,
        )
        self.assertIn('"regression_recovery": True', builder)

    def test_xhc2_typec_policy_is_bounded_and_does_not_touch_proxy(self) -> None:
        hv = (ROOT / "src/hv.c").read_text(encoding="utf-8")
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        tps = (ROOT / "src/tps6598x.c").read_text(encoding="utf-8")
        policy = (ROOT / "src/tps6598x_host_policy.c").read_text(encoding="utf-8")

        self.assertIn("platform_is_j414s()", hv)
        self.assertIn("usb_hpm_handoff_host(uartproxy_iodev)", hv)
        self.assertIn("(s32)idx == preserved_index", usb)
        self.assertIn('adt_getprop(adt, node, "rid"', usb)
        self.assertIn('adt_getprop(adt, node, "port-number"', usb)
        self.assertIn('adt_getprop(adt, node, "port-location"', usb)
        self.assertIn("J414S_USB_CONTROLLER_COUNT", usb)
        self.assertIn("TPS_REG_SYSTEM_CONFIG", tps)
        self.assertIn("System Configuration readback mismatch", tps)
        self.assertIn("timeout_expired(timeout)", tps)
        self.assertIn("TPS6598X_HOST_POLICY_ERR_ROLE", policy)
        self.assertNotIn('tps6598x_command(dev, "GAID"', tps)

        verifier = (ROOT / "tools/verify-j414s-usb-host-adt.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"hpm2": {"rid": 2, "port-number": 3, "port-location": "right"}', verifier)
        self.assertIn('hpm5.getprop("port-location") is not None', verifier)

    def test_type5_power_states_match_apple_t6020_contract(self) -> None:
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        pmgr = (ROOT / "src/pmgr.c").read_text(encoding="utf-8")
        initial = runtime.index("static int acio_runtime_power_initial")
        running = runtime.index("static int acio_runtime_power_running")
        off = runtime.index("static int acio_runtime_power_off")
        initial_body = runtime[initial:running]
        running_body = runtime[running:off]
        down1 = initial_body.index("pmgr_adt_power_disable_index(path, 1)")
        down2 = initial_body.index("pmgr_adt_power_disable_index(path, 2)")
        down0 = initial_body.index("pmgr_adt_power_disable_index(path, 0)")
        self.assertLess(down1, down2)
        self.assertLess(down2, down0)
        self.assertIn("pmgr_adt_power_enable_index(path, 0)", running_body)
        self.assertIn("pmgr_adt_power_enable_index(path, 1)", running_body)
        self.assertIn("pmgr_adt_power_enable_index(path, 2)", running_body)
        self.assertNotIn("pmgr_adt_power_enable_index(path, 3)", running_body)
        self.assertNotIn("pmgr_t6020_acio_reconfig_enable", runtime)
        self.assertNotIn("PMGR_T6020_CIO_RECONFIG", pmgr)
        # The PS TARGET/ACTUAL poll must stay far more permissive than m1n1's
        # original 10000 us.  It is deliberately NOT expressed as Apple's
        # 0x2ee00: that constant is a raw mach-tick delta (~8 ms at 24 MHz),
        # not microseconds, and it belongs to enableCioReconfig's pre-wait
        # rather than the PS convergence poll.  Reusing the literal here
        # re-asserted a decoded-Apple-constant claim that is false, so the
        # test now pins the value and forbids the bad justification.
        self.assertIn("#define PMGR_POLL_TIMEOUT 192000", pmgr)
        self.assertNotIn("Apple's TARGET/ACTUAL wait: 192000 us", pmgr)
        self.assertNotIn("#define PMGR_POLL_TIMEOUT 10000", pmgr)

    def test_type5_tunnel_teardown_is_asymmetric(self) -> None:
        """Teardown is deliberately not the mirror of bring-up.

        Activation is Tx path -> Rx path -> UP enable -> 100 ms -> DOWN
        enable. Teardown disables DOWN first then UP, deactivates Rx before
        Tx, has no delay at all, and has no counter-release step. Each of
        those is easy to "tidy up" into a symmetric mirror, which would be
        wrong, so they are pinned here.
        """
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        # Bound each slice to its own function body. Slicing to end-of-file
        # would sweep in mdelay/udelay from later, unrelated functions and the
        # assertions below would test nothing.
        def body(name: str) -> str:
            start = runtime.index(f"int {name}(")
            end = runtime.index("\n}\n", start)
            return runtime[start:end]

        up_body = body("acio_type5_usb3_tunnel_up")
        down_body = body("acio_type5_usb3_tunnel_down")

        # Bring-up: the settle sits BETWEEN the two adapter enables.
        first_enable = up_body.index("ACIO_TYPE5_USB3_ENABLE")
        settle = up_body.index("mdelay(ACIO_TYPE5_USB3_SETTLE_MS)")
        second_enable = up_body.index("ACIO_TYPE5_USB3_ENABLE", first_enable + 1)
        self.assertLess(first_enable, settle)
        self.assertLess(settle, second_enable)

        # Teardown: no delay anywhere, and no counter release. Assert on the
        # APIs, not on the word "counter" -- the function's own comment
        # explains why there is no counter release, and a bare substring
        # search matches that prose rather than any code.
        self.assertNotIn("mdelay", down_body)
        self.assertNotIn("ACIO_TYPE5_CONFIG_COUNTERS", down_body)
        self.assertNotIn("acio_type5_counter_clear_offset", down_body)
        # ...and the explanation for that absence must survive.
        self.assertIn("NO counter-release step", down_body)

        # Teardown disables the DOWN adapter before the UP adapter.
        self.assertLess(down_body.index("down_adapter"), down_body.index("up_adapter"))

    def test_type5_run_id_distinguishes_runs(self) -> None:
        """A result must be attributable to the run that produced it.

        Phase and error names recur across runs, so matching on them can
        report a previous run's residue as the current run's success. The
        monotonic run_id is the only field that can distinguish them, so it
        must be bumped on every entry and must survive an abort's wipe.
        """
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        header = (ROOT / "src/acio.h").read_text(encoding="utf-8")
        self.assertIn("u32 run_id;", header)
        self.assertIn("runtime->run_id++;", runtime)
        # The bump must precede the bad-phase rejection *within
        # firmware_start*, so a caller can tell its request was seen even when
        # it was refused. Scope the search to that function: the same
        # bad-phase code is also emitted by the router executor earlier in the
        # file, and matching that one tests nothing.
        start = runtime.index("int acio_type5_firmware_start(")
        body = runtime[start:]
        bump = body.index("runtime->run_id++;")
        reject = body.index("ACIO_TYPE5_E_BAD_PHASE, runtime->phase")
        self.assertLess(bump, reject)
        # abort() memsets the runtime; run_id must be preserved across it.
        self.assertIn("u32 preserved_run_id = runtime->run_id;", runtime)
        self.assertIn("runtime->run_id = preserved_run_id;", runtime)

    def test_type5_adopts_preloaded_running_firmware(self) -> None:
        discovery = (ROOT / "src/acio.c").read_text(encoding="utf-8")
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        self.assertIn('adt_getprop(adt, nub_node, "pre-loaded", NULL)', discovery)
        self.assertIn('adt_getprop(adt, nub_node, "running", NULL)', discovery)
        self.assertIn("firmware_preloaded_running", runtime)
        discover = runtime.index("acio_discover_resources(index, &runtime->resources)")
        validate = runtime.index("acio_type5_fw_bundle_validate(bundle, bundle_size, &view)")
        self.assertLess(discover, validate)
        self.assertIn("if (!runtime->resources.firmware_preloaded_running &&", runtime)
        branch = runtime.index("if (runtime->resources.firmware_preloaded_running)")
        self.assertGreater(runtime.index("acio_type5_copy_firmware(&runtime->resources", branch),
                           branch)

    def test_type5_leaves_vdd_cio_with_usb_aon_owner(self) -> None:
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        self.assertNotIn("toggle_vdd_cio", runtime)
        self.assertNotIn("smc_write_u32", runtime)
        self.assertIn("VDD_CIO remains owned", runtime)

    def test_type5_matches_apple_power_phy_running_order(self) -> None:
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        start = runtime.index("int acio_type5_firmware_start")
        initial = runtime.index("acio_runtime_power_initial(index)", start)
        phy = runtime.index("atcphy_prepare_routed_mode(index, mode, flipped)", start)
        running = runtime.index("acio_runtime_power_running(index)", start)
        self.assertLess(initial, phy)
        self.assertLess(phy, running)

    def test_type5_control_dma_uses_adt_dart_window(self) -> None:
        runtime = (ROOT / "src/acio_runtime.c").read_text(encoding="utf-8")
        self.assertIn("dart_find_iova(", runtime)
        # The control-ring IOVA must come from the device's own DART window.
        # The exact spelling moved into a local when the failure path started
        # reporting vm_base, so assert the intent rather than the expression.
        self.assertIn("u64 vm_base = dart_vm_base(control->dart);", runtime)
        self.assertIn("vm_base + ACIO_CONTROL_PAGE_SIZE", runtime)
        self.assertNotIn("0x20000000", runtime)
        self.assertIn("control->iova_base = dart_find_iova", runtime)
        self.assertNotIn("ACIO_CONTROL_IOVA_BASE", runtime)


if __name__ == "__main__":
    unittest.main()
