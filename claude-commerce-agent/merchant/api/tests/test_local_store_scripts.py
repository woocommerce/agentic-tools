# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Local setup output must not disclose the credentials it creates."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3] / "wordpress" / "local-store" / "scripts"


def _script_fixture(tmp_path: Path, name: str, lib: str) -> Path:
    scripts = tmp_path / "wordpress" / "local-store" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / name, scripts / name)
    (scripts / "lib.sh").write_text(lib, encoding="utf-8")
    return scripts / name


def test_setup_output_does_not_print_the_admin_password(tmp_path: Path) -> None:
    secret = "generated-admin-password"
    script = _script_fixture(
        tmp_path,
        "setup.sh",
        f"""\
STORE_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
REPO_ROOT="$(cd "${{STORE_DIR}}/../.." && pwd)"
load_env() {{ export WORDPRESS_ADMIN_USER=operator WORDPRESS_ADMIN_PASSWORD={secret}; }}
require_cmd() {{ :; }}
wp() {{ :; }}
""",
    )
    scripts = script.parent
    for child in ("install-woocommerce.sh", "create-api-keys.sh"):
        path = scripts / child
        path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    merchant_scripts = tmp_path / "merchant" / "scripts"
    merchant_scripts.mkdir(parents=True)
    (merchant_scripts / "seed_store.py").write_text("", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script), "--no-images"],
        cwd=tmp_path,
        env={**os.environ, "PYTHON": "/usr/bin/true"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert secret not in result.stdout
    assert "Admin:" in result.stdout


_KEY_LIB = """\
STORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${STORE_DIR}/../.." && pwd)"
load_env() { :; }
require_cmd() { :; }
get_env_value() { printf '%s' "${STORED:-}"; }
set_env_value() { printf '%s=%s\\n' "$1" "$2" >> "${REPO_ROOT}/written.txt"; }
"""


def _key_fixture(tmp_path: Path, lib_tail: str) -> Path:
    script = _script_fixture(tmp_path, "create-api-keys.sh", _KEY_LIB + lib_tail)
    merchant = tmp_path / "merchant"
    merchant.mkdir(parents=True, exist_ok=True)
    (merchant / ".env.example").write_text("WOOCOMMERCE_LOCAL_STORE=1\n", encoding="utf-8")
    return script


def test_api_key_failure_does_not_echo_generated_credentials(tmp_path: Path) -> None:
    secret = "ck_generated_secret"
    script = _key_fixture(
        tmp_path,
        f"""\
curl() {{ printf '000'; return 1; }}
wp() {{ printf '%s\\n' '{{"consumer_key":"{secret}"}}'; }}
""",
    )

    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path, text=True, capture_output=True, check=False
    )

    assert result.returncode == 1
    assert secret not in result.stderr
    assert "Failed to generate WooCommerce API credentials" in result.stderr


def test_a_redirect_does_not_pass_for_a_working_key(tmp_path: Path) -> None:
    """``curl -f`` exits 0 on a 3xx, so a redirect would read as proof of authentication the
    store never performed. Only a 200 counts, and redirects are not followed at all, because
    the credentials ride in the query string."""
    script = _key_fixture(
        tmp_path,
        """\
curl() { printf '302'; }
wp() { echo "went on to mint" >&2; exit 9; }
""",
    )

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={**os.environ, "STORED": "ck_existing"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 9
    assert "went on to mint" in result.stderr


def test_a_working_key_is_kept_instead_of_minting_another(tmp_path: Path) -> None:
    """Re-running setup.sh must not rotate a key that still works: WooCommerce keeps only a
    hash, so a replacement would strand the copy a running merchant host already loaded."""
    script = _key_fixture(
        tmp_path,
        """\
curl() { printf '200'; }
wp() { echo "minted a new key" >&2; exit 9; }
""",
    )

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={**os.environ, "STORED": "ck_existing"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "minted" not in result.stderr
    assert "Kept the REST API key" in result.stdout
    written = (tmp_path / "written.txt").read_text(encoding="utf-8")
    assert "WOOCOMMERCE_CONSUMER_KEY=" not in written
    assert "WOOCOMMERCE_LOCAL_STORE=0" in written
