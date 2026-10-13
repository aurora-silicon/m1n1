# SPDX-License-Identifier: MIT
# Generated-by: LLM
# Assisted-by: LLM
"""Check complete DP supplier withdrawal with J700's alias-free external DT."""

import os
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def refusal_binary(tmp_path):
    source = (ROOT / "src/kboot.c").read_text()
    iommu = source[source.index("static int dt_get_iommu_node("):
                   source.index("static dart_dev_t *dt_init_dart_by_node(")]
    helpers = source[source.index("static int dt_t8140_disable_refs("):
                     source.index("static int dt_set_display_t8140(void)")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
typedef uint32_t u32;
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
static unsigned char tree[16384];
static void *dt = tree;
static size_t dt_bufsize = sizeof(tree);
static const char *failed_path;
static const char *lookup_fault;
static int guarded_subnode_offset(const void *blob, int parent, const char *name)
{
    if (lookup_fault && !strcmp(lookup_fault, "route-lookup") &&
        !strcmp(name, "typec-routes"))
        return -FDT_ERR_BADSTRUCTURE;
    return fdt_subnode_offset(blob, parent, name);
}
static int guarded_next_subnode(const void *blob, int offset)
{
    if (lookup_fault && !strcmp(lookup_fault, "route-iteration"))
        return -FDT_ERR_BADSTRUCTURE;
    return fdt_next_subnode(blob, offset);
}
static int guarded_compatible(const void *blob, int start, const char *compatible)
{
    if (lookup_fault &&
        ((!strcmp(lookup_fault, "external-lookup") &&
          !strcmp(compatible, "apple,t8140-dcpext")) ||
         (!strcmp(lookup_fault, "whole-iteration") &&
          !strcmp(compatible, "apple,t8140-dcp"))))
        return -FDT_ERR_BADSTRUCTURE;
    return fdt_node_offset_by_compatible(blob, start, compatible);
}
static int guarded_path_offset(const void *blob, const char *path)
{
    if (lookup_fault && !strcmp(lookup_fault, "alias-lookup") &&
        !strcmp(path, "dcpext"))
        return -FDT_ERR_BADSTRUCTURE;
    return fdt_path_offset(blob, path);
}
static const void *guarded_getprop(const void *blob, int offset, const char *name,
                                   int *length)
{
    if (lookup_fault && !strcmp(lookup_fault, "alias-lookup") &&
        !strcmp(name, "dcpext")) {
        *length = -FDT_ERR_BADSTRUCTURE;
        return NULL;
    }
    return fdt_getprop(blob, offset, name, length);
}
static int guarded_setprop_string(void *blob, int offset, const char *name,
                                  const char *value)
{
    char path[256];
    if (failed_path && !strcmp(name, "status") && !strcmp(value, "disabled") &&
        !fdt_get_path(blob, offset, path, sizeof(path)) && !strcmp(path, failed_path))
        return -FDT_ERR_NOSPACE;
    return fdt_setprop_string(blob, offset, name, value);
}
#undef fdt_setprop_string
#define fdt_setprop_string guarded_setprop_string
#define fdt_subnode_offset guarded_subnode_offset
#define fdt_next_subnode guarded_next_subnode
#define fdt_node_offset_by_compatible guarded_compatible
#define fdt_path_offset guarded_path_offset
#define fdt_getprop guarded_getprop
''' + iommu + helpers + r'''
static int node(const char *path)
{
    int offset = fdt_path_offset(dt, path);
    assert(offset >= 0);
    return offset;
}
static void add(const char *parent, const char *name, u32 phandle)
{
    int offset = fdt_add_subnode(dt, node(parent), name);
    assert(offset >= 0);
    if (phandle)
        assert(!fdt_setprop_u32(dt, offset, "phandle", phandle));
}
static void string(const char *path, const char *key, const char *value)
{
    assert(!fdt_setprop_string(dt, node(path), key, value));
}
static void cell(const char *path, const char *key, u32 value)
{
    assert(!fdt_setprop_u32(dt, node(path), key, value));
}
static void iommu(const char *path, u32 provider, u32 stream)
{
    fdt32_t tuple[] = {cpu_to_fdt32(provider), cpu_to_fdt32(stream)};
    assert(!fdt_setprop(dt, node(path), "iommus", tuple, sizeof(tuple)));
}
static void status(const char *path, const char *expected)
{
    const char *actual = fdt_getprop(dt, node(path), "status", NULL);
    if (!actual || strcmp(actual, expected)) {
        fprintf(stderr, "%s status: expected %s, got %s\n", path, expected,
                actual ? actual : "absent");
        exit(1);
    }
}
static void setup(void)
{
    memset(tree, 0, sizeof(tree));
    assert(!fdt_create_empty_tree(dt, sizeof(tree)));
    add("/", "aliases", 0);
    add("/", "chosen", 0);
    add("/chosen", "framebuffer", 0);
    cell("/chosen", "apple,dcp-rtkit-quiesced", 1);
    add("/", "soc", 0);
    add("/soc", "dcp", 1);
    string("/soc/dcp", "compatible", "apple,t8140-dcp");
    string("/soc/dcp", "status", "okay");
    add("/soc", "display", 2);
    string("/soc/display", "compatible", "apple,display-subsystem");
    string("/soc/display", "status", "okay");
    string("/aliases", "dcp", "/soc/dcp");
    string("/aliases", "disp0", "/soc/display");
    /* Public J700 uses these two aliases, with no external resource aliases. */
    add("/soc", "dcp-dart", 3);
    add("/soc", "disp-dart", 4);
    add("/soc", "external-dcp-dart", 5);
    add("/soc", "external-disp-dart", 6);
    const char *darts[] = {"/soc/dcp-dart", "/soc/disp-dart",
                          "/soc/external-dcp-dart", "/soc/external-disp-dart"};
    for (unsigned i = 0; i < ARRAY_SIZE(darts); i++) {
        string(darts[i], "compatible", "apple,t8140-dart");
        cell(darts[i], "#iommu-cells", 1);
        string(darts[i], "status", i < 2 ? "okay" : "disabled");
    }
    iommu("/soc/dcp", 3, 23);
    add("/soc/dcp", "piodma", 0);
    iommu("/soc/dcp/piodma", 4, 16);
    fdt32_t masters[] = {cpu_to_fdt32(4), cpu_to_fdt32(0),
                         cpu_to_fdt32(6), cpu_to_fdt32(0)};
    assert(!fdt_setprop(dt, node("/soc/display"), "iommus", masters, sizeof(masters)));
    add("/soc", "external-mbox", 7);
    string("/soc/external-mbox", "compatible", "apple,t8140-asc-mailbox");
    cell("/soc/external-mbox", "#mbox-cells", 0);
    string("/soc/external-mbox", "status", "disabled");
    add("/soc", "external", 8);
    string("/soc/external", "compatible", "apple,t8140-dcpext");
    string("/soc/external", "status", "disabled");
    iommu("/soc/external", 5, 23);
    cell("/soc/external", "mboxes", 7);
    add("/soc/external", "piodma", 0);
    iommu("/soc/external/piodma", 6, 16);
    add("/soc/external", "typec-routes", 0);
    add("/soc/external/typec-routes", "route@0", 0);
    string("/soc/external/typec-routes/route@0", "status", "okay");
    const char *powers[] = {"power-sys", "power-fe", "power-cpu"};
    for (unsigned i = 0; i < ARRAY_SIZE(powers); i++) {
        add("/soc", powers[i], 10 + i);
        char path[64];
        snprintf(path, sizeof(path), "/soc/%s", powers[i]);
        string(path, "compatible", "apple,t8140-pmgr-pwrstate");
        cell(path, "#power-domain-cells", 0);
        string(path, "status", "disabled");
        if (i)
            cell(path, "power-domains", 9 + i);
    }
    cell("/soc/external", "power-domains", 12);
    cell("/soc/external-mbox", "power-domains", 12);
    assert(!fdt_setprop(dt, node("/soc/power-cpu"), "apple,always-on", NULL, 0));
    assert(!dt_t8140_enable_external("/soc/external"));
    status("/soc/external-disp-dart", "okay");
    status("/soc/power-cpu", "okay");
    status("/soc/power-fe", "okay");
    status("/soc/power-sys", "okay");
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    setup();

    if (!strcmp(argv[1], "whole-master-mailbox") ||
        !strcmp(argv[1], "whole-master-piodma") ||
        !strcmp(argv[1], "duplicate-phandle") ||
        !strcmp(argv[1], "duplicate-power-node")) {
        bool duplicate_name = !strcmp(argv[1], "duplicate-power-node");
        bool duplicate = !strncmp(argv[1], "duplicate-", 10);
        bool mailbox = !strcmp(argv[1], "whole-master-mailbox");
        add("/soc", "unrelated-provider", !duplicate_name && duplicate ? 5 : 30);
        string("/soc/unrelated-provider", "compatible",
               mailbox ? "apple,t8140-asc-mailbox" : "apple,t8140-dart");
        cell("/soc/unrelated-provider", mailbox ? "#mbox-cells" : "#iommu-cells",
             mailbox ? 0 : 1);
        string("/soc/unrelated-provider", "status", "okay");
        add("/soc", "keeper", 31);
        if (duplicate_name) {
            assert(!fdt_delprop(dt, node("/soc/power-cpu"), "power-domains"));
            string("/soc/unrelated-provider", "compatible", "apple,t8140-pmgr-pwrstate");
            cell("/soc/unrelated-provider", "#power-domain-cells", 0);
            assert(!fdt_set_name(dt, node("/soc/unrelated-provider"), "power-cpu"));
        }
        if (mailbox) {
            cell("/soc/display", "mboxes", 30);
            cell("/soc/keeper", "mboxes", 30);
        } else if (!duplicate) {
            add("/soc/display", "piodma", 0);
            iommu("/soc/display/piodma", 30, 0);
            iommu("/soc/keeper", 30, 1);
        }
        unsigned char before[sizeof(tree)];
        memcpy(before, tree, sizeof(tree));
        int ret = duplicate ? dt_t8140_disable_dcpext() : dt_t8140_refuse_display();
        printf("review case %s: ret=%d unchanged=%d\n", argv[1], ret,
               !memcmp(before, tree, sizeof(tree)));
        fflush(stdout);
        if (duplicate) {
            assert(ret < 0);
            assert(!memcmp(before, tree, sizeof(tree)));
        } else {
            assert(!ret);
            status("/soc/display", "disabled");
        }
        status(duplicate_name ? "/soc/power-cpu" : "/soc/unrelated-provider", "okay");
        return 0;
    }

    if (!strncmp(argv[1], "bad-alias:", 10)) {
        const char *kind = argv[1] + 10;
        bool whole = !strncmp(kind, "whole:", 6);
        if (whole)
            kind += 6;
        const char *alias = whole ? "dcp" : "dcpext";
        const char *value;
        int length;
        if (!strcmp(kind, "self")) {
            value = alias;
            length = strlen(alias) + 1;
        } else if (!strcmp(kind, "relative")) {
            value = "soc/external";
            length = strlen(value) + 1;
        } else if (!strcmp(kind, "empty")) {
            value = "";
            length = 0;
        } else if (!strcmp(kind, "unterminated")) {
            value = "/soc/external";
            length = strlen(value);
        } else {
            assert(!strcmp(kind, "embedded-nul"));
            value = "/soc/external\0ignored";
            length = 22;
        }
        assert(!fdt_setprop(dt, node("/aliases"), alias, value, length));
        assert((whole ? dt_t8140_refuse_display() : dt_t8140_disable_dcpext()) < 0);
        assert(node("/chosen/framebuffer") >= 0);
        if (!whole)
            status("/soc/dcp", "okay");
        assert(!fdt_check_header(dt));
        return 0;
    }
    if (!strcmp(argv[1], "valid-external-alias")) {
        string("/aliases", "dcpext", "/soc/external");
        assert(!dt_t8140_disable_dcpext());
        status("/soc/dcp", "okay");
        status("/soc/external", "disabled");
        assert(node("/chosen/framebuffer") >= 0);
        return 0;
    }

    if (!strncmp(argv[1], "owner:", 6)) {
        const char *test = argv[1] + 6;
        bool whole = !strncmp(test, "whole-", 6);
        if (whole) test += 6;
        if (!strcmp(test, "internal-dcp")) string("/aliases", "dcpext", "/soc/dcp");
        else if (!strcmp(test, "keeper")) string("/aliases", "dispext0", "/soc/display");
        else if (!strcmp(test, "internal-dart")) string("/aliases", "dcpext_dart", "/soc/dcp-dart");
        else if (!strcmp(test, "internal-scanout")) string("/aliases", "dispext0_dart", "/soc/disp-dart");
        else if (!strcmp(test, "wrong-role")) string("/aliases", "dcpext", "/soc/external-dcp-dart");
        else if (!strcmp(test, "power-alias")) string("/aliases", "dcpext_dart", "/soc/power-cpu");
        else if (!strcmp(test, "mailbox-role")) string("/aliases", "dcpext_mbox", "/soc/external-dcp-dart");
        else if (!strcmp(test, "repeated-alias")) {
            string("/aliases", "dcpext", "/soc/external");
            string("/aliases", "dcpext_dart", "/soc/external");
        } else if (!strcmp(test, "dangling")) string("/aliases", "dcpext", "/soc/missing");
        else if (!strcmp(test, "ambiguous")) {
            add("/soc", "second-external", 30);
            string("/soc/second-external", "compatible", "apple,t8140-dcpext");
        } else if (!strcmp(test, "ambiguous-role")) {
            const char types[] = "apple,t8140-dcpext\0apple,t8140-dcp";
            assert(!fdt_setprop(dt, node("/soc/external"), "compatible", types, sizeof(types)));
            assert(!fdt_delprop(dt, node("/soc/dcp"), "compatible"));
        } else if (!strcmp(test, "unrelated-provider")) {
            add("/soc", "unrelated-dart", 30);
            string("/soc/unrelated-dart", "compatible", "apple,t8140-dart");
            cell("/soc/unrelated-dart", "#iommu-cells", 1);
            string("/soc/unrelated-dart", "status", "okay");
            string("/aliases", "dcpext_dart", "/soc/unrelated-dart");
        } else if (!strcmp(test, "another-dcp-dart")) {
            add("/soc", "other-dcp", 30);
            string("/soc/other-dcp", "compatible", "apple,t6020-dcp");
            iommu("/soc/other-dcp", 5, 23);
        } else if (!strcmp(test, "shared-power")) cell("/soc/dcp", "power-domains", 12);
        else if (!strcmp(test, "shared-scanout")) iommu("/soc/dcp/piodma", 6, 16);
        else if (!strcmp(test, "malformed-controller")) cell("/soc/external", "iommus", 5);
        else if (!strcmp(test, "malformed-power")) cell("/soc/power-cpu", "power-domains", 99);
        else if (!strcmp(test, "non-provider")) string("/soc/external-dcp-dart", "compatible", "apple,unrelated");
        else if (!strcmp(test, "internal-alias")) {
            whole = true;
            string("/aliases", "dcp", "/soc/external");
        } else assert(0);
        unsigned char before[sizeof(tree)];
        memcpy(before, tree, sizeof(tree));
        int ret = whole ? dt_t8140_refuse_display() : dt_t8140_disable_dcpext();
        assert(ret < 0);
        assert(!memcmp(before, tree, sizeof(tree)));
        status("/soc/dcp", "okay");
        status("/soc/dcp-dart", "okay");
        assert(node("/chosen/framebuffer") >= 0);
        return 0;
    }
    if (!strcmp(argv[1], "whole-piodma-only")) {
        assert(!fdt_delprop(dt, node("/soc/display"), "iommus"));
        assert(!fdt_delprop(dt, node("/aliases"), "disp0"));
        assert(!dt_t8140_refuse_display());
        status("/soc/disp-dart", "disabled");
        status("/soc/dcp", "disabled");
        return 0;
    }
    if (!strcmp(argv[1], "static-aliases") || !strcmp(argv[1], "unrelated-whole") ||
        !strcmp(argv[1], "repeated-refusal")) {
        add("/soc", "external-display", 30);
        string("/soc/external-display", "compatible", "apple,display-subsystem");
        string("/soc/external-display", "status", "okay");
        iommu("/soc/external-display", 6, 0);
        string("/aliases", "dcpext", "/soc/external");
        string("/aliases", "dispext0", "/soc/external-display");
        string("/aliases", "dcpext_dart", "/soc/external-dcp-dart");
        string("/aliases", "dispext0_dart", "/soc/external-disp-dart");
        string("/aliases", "dcpext_mbox", "/soc/external-mbox");
        if (!strcmp(argv[1], "unrelated-whole")) {
            add("/soc", "other-dart", 31);
            string("/soc/other-dart", "compatible", "apple,t8140-dart");
            cell("/soc/other-dart", "#iommu-cells", 1);
            string("/soc/other-dart", "status", "okay");
            add("/soc", "other-display", 32);
            string("/soc/other-display", "compatible", "apple,display-subsystem");
            string("/soc/other-display", "status", "okay");
            iommu("/soc/other-display", 31, 0);
            assert(!dt_t8140_refuse_display());
            status("/soc/other-display", "okay");
            status("/soc/other-dart", "okay");
        } else {
            assert(!dt_t8140_disable_dcpext());
            status("/soc/dcp", "okay");
            status("/soc/display", "okay");
            status("/soc/external-display", "disabled");
            if (!strcmp(argv[1], "repeated-refusal")) {
                /* A retired component master has no remaining external tuples. */
                assert(!fdt_delprop(dt, node("/aliases"), "dispext0"));
                assert(!dt_t8140_disable_dcpext());
            }
        }
        return 0;
    }
    if (!strcmp(argv[1], "external")) {
        assert(!dt_t8140_disable_dcpext());
        status("/soc/dcp", "okay");
        status("/soc/display", "okay");
        status("/soc/external", "disabled");
        status("/soc/external-dcp-dart", "disabled");
        status("/soc/external-mbox", "disabled");
        status("/soc/external/typec-routes/route@0", "disabled");
        status("/soc/power-cpu", "disabled");
        int length;
        const fdt32_t *refs = fdt_getprop(dt, node("/soc/display"), "iommus", &length);
        assert(refs && length == 8 && fdt32_to_cpu(refs[0]) == 4);
        assert(fdt_getprop(dt, node("/chosen"), "apple,dcp-rtkit-quiesced", NULL));
        status("/soc/external-disp-dart", "disabled");
    } else if (!strcmp(argv[1], "scanout-write-failure")) {
        failed_path = "/soc/external-disp-dart";
        assert(dt_t8140_disable_dcpext() < 0);
        status("/soc/external-disp-dart", "okay");
        assert(node("/chosen/framebuffer") >= 0);
    } else if (!strcmp(argv[1], "whole-power-write-failure")) {
        failed_path = "/soc/power-cpu";
        assert(dt_t8140_refuse_display() < 0);
        status("/soc/power-cpu", "okay");
        assert(node("/chosen/framebuffer") >= 0);
    } else if (!strcmp(argv[1], "malformed-piodma")) {
        cell("/soc/external-disp-dart", "#iommu-cells", 2);
        unsigned char before[sizeof(tree)];
        memcpy(before, tree, sizeof(tree));
        assert(dt_t8140_disable_dcpext() < 0);
        assert(!memcmp(before, tree, sizeof(tree)));
    } else if (!strncmp(argv[1], "write:", 6)) {
        failed_path = argv[1] + 6;
        assert(dt_t8140_disable_dcpext() < 0);
        assert(node("/chosen/framebuffer") >= 0);
    } else if (!strcmp(argv[1], "long-route")) {
        /* Both names are legal FDT names. The 31-byte name is the prefix of
         * the longer one, so silent truncation can disable the wrong route. */
        const char *short_name = "route01234567890123456789012345";
        const char *long_name = "route01234567890123456789012345x";
        assert(strlen(short_name) == 31);
        add("/soc/external/typec-routes", short_name, 0);
        add("/soc/external/typec-routes", long_name, 0);
        string("/soc/external/typec-routes/route01234567890123456789012345", "status", "okay");
        string("/soc/external/typec-routes/route01234567890123456789012345x", "status", "okay");
        assert(dt_t8140_disable_dcpext() < 0);
        status("/soc/external/typec-routes/route01234567890123456789012345x", "okay");
    } else if (!strcmp(argv[1], "route-lookup") ||
               !strcmp(argv[1], "route-iteration") ||
               !strcmp(argv[1], "external-lookup") ||
               !strcmp(argv[1], "alias-lookup")) {
        lookup_fault = argv[1];
        assert(dt_t8140_disable_dcpext() < 0);
        assert(node("/chosen/framebuffer") >= 0);
    } else if (!strcmp(argv[1], "whole-iteration")) {
        lookup_fault = argv[1];
        assert(dt_t8140_refuse_display() < 0);
        assert(node("/chosen/framebuffer") >= 0);
    } else {
        assert(!strcmp(argv[1], "whole"));
        assert(!dt_t8140_refuse_display());
        status("/soc/dcp", "disabled");
        status("/soc/display", "disabled");
        status("/soc/external", "disabled");
        status("/soc/external-dcp-dart", "disabled");
        status("/soc/external-disp-dart", "disabled");
        status("/soc/external-mbox", "disabled");
        status("/soc/external/typec-routes/route@0", "disabled");
        assert(!fdt_getprop(dt, node("/chosen"), "apple,dcp-rtkit-quiesced", NULL));
        assert(node("/chosen/framebuffer") >= 0);
        status("/soc/power-cpu", "disabled");
        status("/soc/power-fe", "disabled");
        status("/soc/power-sys", "disabled");
    }
    assert(!fdt_check_header(dt));
    return 0;
}
'''
    c = tmp_path / "refusal.c"
    c.write_text(harness)
    lib = ROOT / "src/libfdt"
    sources = [lib / name for name in (
        "fdt.c", "fdt_ro.c", "fdt_rw.c", "fdt_sw.c", "fdt_wip.c",
        "fdt_empty_tree.c", "fdt_check.c",
    )]
    binary = tmp_path / "refusal"
    compiler = shlex.split(os.environ.get("CC", "cc"))
    subprocess.run(compiler + ["-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror",
                              "-I", str(lib), str(c), *map(str, sources),
                              "-o", str(binary)], check=True)
    return binary


def test_external_refusal_withdraws_scanout_provider_without_alias(refusal_binary):
    subprocess.run([str(refusal_binary), "external"], check=True)


def test_whole_refusal_withdraws_published_external_power_chain(refusal_binary):
    subprocess.run([str(refusal_binary), "whole"], check=True)


def test_external_refusal_rejects_scanout_provider_write_failure(refusal_binary):
    subprocess.run([str(refusal_binary), "scanout-write-failure"], check=True)


def test_external_refusal_rejects_malformed_piodma_tuple_without_edits(refusal_binary):
    subprocess.run([str(refusal_binary), "malformed-piodma"], check=True)


def test_whole_refusal_rejects_power_supplier_write_failure(refusal_binary):
    subprocess.run([str(refusal_binary), "whole-power-write-failure"], check=True)


@pytest.mark.parametrize("scenario", [
    "long-route", "route-lookup", "route-iteration", "external-lookup",
    "alias-lookup", "whole-iteration",
])
def test_refusal_rejects_incomplete_topology(refusal_binary, scenario):
    subprocess.run([str(refusal_binary), scenario], check=True)


@pytest.mark.parametrize("path", [
    "/soc/external-dcp-dart", "/soc/external-disp-dart", "/soc/external-mbox",
    "/soc/external", "/soc/external/typec-routes/route@0",
    "/soc/power-cpu", "/soc/power-fe", "/soc/power-sys",
])
def test_external_refusal_rejects_every_supplier_and_route_write_failure(refusal_binary, path):
    subprocess.run([str(refusal_binary), "write:" + path], check=True)


@pytest.mark.parametrize("whole", [False, True])
@pytest.mark.parametrize("kind", ["self", "relative", "empty", "unterminated", "embedded-nul"])
def test_refusal_rejects_malformed_alias_before_resolution(refusal_binary, kind, whole):
    scenario = "bad-alias:" + ("whole:" if whole else "") + kind
    subprocess.run([str(refusal_binary), scenario], check=True)


def test_external_refusal_accepts_valid_optional_alias(refusal_binary):
    subprocess.run([str(refusal_binary), "valid-external-alias"], check=True)


@pytest.mark.parametrize("scenario", [
    "internal-dcp", "keeper", "internal-dart", "internal-scanout", "wrong-role",
    "power-alias", "mailbox-role", "repeated-alias", "dangling", "ambiguous",
    "unrelated-provider", "another-dcp-dart", "shared-power", "shared-scanout",
    "malformed-controller", "malformed-power", "non-provider", "internal-alias",
    "whole-internal-dcp", "whole-internal-dart", "whole-unrelated-provider", "ambiguous-role",
])
def test_refusal_rejects_wrong_ownership_before_any_edit(refusal_binary, scenario):
    subprocess.run([str(refusal_binary), "owner:" + scenario], check=True)


@pytest.mark.parametrize("scenario", ["static-aliases", "unrelated-whole", "repeated-refusal", "whole-piodma-only"])
def test_refusal_preserves_valid_static_and_unrelated_topology(refusal_binary, scenario):
    subprocess.run([str(refusal_binary), scenario], check=True)


@pytest.mark.parametrize("scenario", ["whole-master-mailbox", "whole-master-piodma", "duplicate-phandle", "duplicate-power-node"])
def test_refusal_does_not_claim_unvalidated_providers(refusal_binary, scenario):
    subprocess.run([str(refusal_binary), scenario], check=True)
