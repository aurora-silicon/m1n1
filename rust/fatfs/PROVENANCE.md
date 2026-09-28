# Vendored rust-fatfs

- Upstream: `https://github.com/rafalh/rust-fatfs`
- Revision: `4eccb50d011146fbed20e133d33b22f3c27292e7`
- Licence: MIT; original `LICENSE.txt` and Cargo author attribution retained.
- Files: upstream `src/*.rs`, `Cargo.toml`, `LICENSE.txt`, and `README.md`.
- Excluded: examples, tests, fixtures, scripts, CI files, changelog and repository metadata.

Local code changes after the import:

1. `explicit-fat32` feature: use the BPB's zero 16-bit sectors-per-FAT field to select FAT32 even
   on a small volume, while retaining the remaining BPB validation. Enabled only by m1n1.
2. Expose `DirEntry::eq_name` so the loader can detect duplicate matches while honoring long
   names, short aliases and the filesystem's case rules.
