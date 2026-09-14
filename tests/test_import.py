from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_importing_package_does_not_import_executable_main_module() -> None:
    environment = os.environ.copy()
    src_root = Path(__file__).resolve().parents[1] / "src"
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(src_root), existing_pythonpath) if path
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import camera_media_service; "
                "assert 'camera_media_service.main' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
