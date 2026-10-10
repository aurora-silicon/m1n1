"""Run the production Rust ADT accessors against malformed host fixtures."""

from pathlib import Path
import subprocess


def test_malformed_adt_rejected(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    binary = tmp_path / "adt-malformed"
    subprocess.run(
        ["rustc", "--edition=2021", str(repo / "tests/rust/adt_malformed.rs"), "-o", str(binary)],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
