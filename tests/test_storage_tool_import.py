import subprocess
import sys
from pathlib import Path


def test_mount_guard_is_importable_with_only_the_host_standard_library():
    source = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import sys; sys.path.insert(0, " + repr(str(source)) + "); "
        "from camera_media_service.storage_mount import StorageMountGuard; "
        "print(StorageMountGuard.__name__)"
    )
    result = subprocess.run([sys.executable, "-S", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "StorageMountGuard"
