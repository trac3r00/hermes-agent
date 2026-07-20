from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_launcher_prefers_project_virtualenv(tmp_path: Path) -> None:
    launcher = tmp_path / "hermes"
    launcher.write_bytes((REPO_ROOT / "hermes").read_bytes())
    launcher.chmod(0o755)

    virtualenv_launcher = tmp_path / "venv" / "bin" / "hermes"
    virtualenv_launcher.parent.mkdir(parents=True)
    virtualenv_launcher.write_text(
        "#!/bin/sh\nprintf 'project-venv:%s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    virtualenv_launcher.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "probe"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "project-venv:probe\n"


def test_repo_launcher_prefers_installed_venv_when_both_exist(tmp_path: Path) -> None:
    launcher = tmp_path / "hermes"
    launcher.write_bytes((REPO_ROOT / "hermes").read_bytes())
    launcher.chmod(0o755)

    for environment, output in (("venv", "installed"), (".venv", "development")):
        target = tmp_path / environment / "bin" / "hermes"
        target.parent.mkdir(parents=True)
        target.write_text(
            f"#!/bin/sh\nprintf '{output}:%s\\n' \"$1\"\n",
            encoding="utf-8",
        )
        target.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "probe"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "installed:probe\n"
