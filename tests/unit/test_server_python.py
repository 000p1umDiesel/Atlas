import shutil
import subprocess
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).parents[2]


def test_server_export_is_cpu_only_and_keeps_the_actual_editable_application():
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv CLI is required for the frozen deployment export")
    result = subprocess.run(
        [uv, "--no-cache", "export", "--project", str(ROOT / "deploy/shared-host/python"),
         "--frozen", "--no-dev", "--no-hashes", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    requirements = [Requirement(line.strip()) for line in result.stdout.splitlines()
                    if line.strip() and not line.strip().startswith(("#", "-"))]
    assert not any(r.name.lower().startswith("nvidia-") for r in requirements)
    versions = {r.name: str(r.specifier) for r in requirements}
    assert versions["torch"] == "==2.14.1+cpu"
    assert versions["torchvision"] == "==0.29.1+cpu"
    editable = [line[3:] for line in result.stdout.splitlines() if line.startswith("-e ")]
    assert len(editable) == 1
    assert (ROOT / editable[0]).resolve() == ROOT
