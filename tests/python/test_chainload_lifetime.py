"""Check ownership of FAT buffers after copying into the chainload image."""

from pathlib import Path
import subprocess


def test_chainload_releases_loaded_image(tmp_path):
    source = (Path(__file__).resolve().parents[2] / "src/chainload.c").read_text()
    start = source.index("int chainload_load(")
    source = source[start:source.index("#else", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
static struct { void *entry; } next_stage;
static char image[16];
static int load_error, copy_error, freed, copied;
static bool shutdown_ok;
static bool nvme_init(void) { return true; }
static bool nvme_has_live_post_m4_session(void) { return true; }
static bool nvme_shutdown(void) { return shutdown_ok; }
static int rust_load_image(const char *spec, void **data, size_t *size)
{
    *data = image;
    *size = sizeof(image);
    return load_error;
}
static int chainload_image(void *data, size_t size, char **vars, size_t count)
{
    assert(data == image && size == sizeof(image) && !freed);
    assert(count == 1 && !strcmp(vars[0], "nvme.adopt=live-rtkit-v1"));
    copied++;
    next_stage.entry = copy_error ? NULL : (void *)1;
    return copy_error;
}
static void rust_free_image(void *data, size_t size)
{
    assert(data == image && size == sizeof(image) && copied && !freed);
    freed++;
}
''' + source + r'''
int main(void)
{
    for (int failure = 0; failure < 4; failure++) {
        char *vars[1];
        size_t count = 0;
        freed = copied = 0;
        load_error = failure == 1 ? -1 : 0;
        copy_error = failure == 2 ? -1 : 0;
        shutdown_ok = failure != 3;
        assert(chainload_load("test", vars, &count, 1) == (failure ? -1 : 0));
        assert(freed == (failure != 1));
        assert(copied == (failure != 1));
        assert(!!next_stage.entry == !failure);
    }
    return 0;
}
'''
    binary = tmp_path / "chainload-lifetime"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
         "-x", "c", "-", "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
