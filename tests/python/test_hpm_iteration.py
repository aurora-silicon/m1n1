"""A matching SPMI node must not report an action that never ran."""

from pathlib import Path
import subprocess


def test_spmi_match_does_not_imply_action_success(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/tps6598x.c").read_text()
    iterate = source[source.index("int tps6598x_foreach_hpm("):
                     source.index("static int tps6598x_enable_debugusb_one(")]
    header = (root / "src/tps6598x.h").read_text()
    declarations = header[header.index("typedef bool(hpm_match_t)"):
                          header.index("int tps6598x_enter_kis(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef int i2c_dev_t;
typedef int spmi_dev_t;
typedef int tps6598x_dev_t;
static void *adt;
static int failure, calls, result, device;
#define ADT_FOREACH_CHILD(a, n) for (int end = (n) + 1; ++(n) == end;)
static int adt_path_offset(void *a, const char *p) { return 0; }
static int adt_first_child_offset(void *a, int n) { return n + 1; }
static bool adt_is_compatible(void *a, int n, const char *c)
{
    return (n == 1 && !strcmp(c, "aapl,spmi")) ||
           (n == 2 && !strcmp(c, "usbc,sn201202x,spmi"));
}
static const char *adt_get_name(void *a, int n) { return n == 1 ? "spmi" : "hpm0"; }
static i2c_dev_t *i2c_init(const char *p) { assert(0); return NULL; }
static void i2c_shutdown(i2c_dev_t *d) { assert(0); }
static spmi_dev_t *spmi_init(const char *p) { return failure == 1 ? NULL : &device; }
static void spmi_shutdown(spmi_dev_t *d) {}
static tps6598x_dev_t *tps6598x_init_i2c(const char *p, i2c_dev_t *d)
{ assert(0); return NULL; }
static tps6598x_dev_t *tps6598x_init_spmi(const char *p, spmi_dev_t *d)
{ return failure == 2 ? NULL : &device; }
static void tps6598x_shutdown(tps6598x_dev_t *d) {}
''' + declarations + iterate + r'''
static bool match(char *p, void *data) { return failure != 3; }
static int action(char *p, tps6598x_dev_t *d, void *data) { calls++; return result; }
int main(void)
{
    result = HPM_ACTION_STOP;
    for (failure = 1; failure <= 3; failure++) {
        assert(tps6598x_foreach_hpm(match, action, NULL) != HPM_ACTION_STOP);
        assert(!calls);
    }
    failure = 0;
    for (result = HPM_ACTION_ERROR; result <= HPM_ACTION_STOP; result++) {
        assert(tps6598x_foreach_hpm(match, action, NULL) == result);
        assert(calls == result + 2);
    }
    return 0;
}
'''
    binary = tmp_path / "hpm-iteration"
    subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
         "-x", "c", "-", "-o", str(binary)], input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
