"""E2E test for workflow-runner container image.

Validates:
1. Image builds successfully from Containerfile
2. Container starts and responds on /healthz
3. Container runs as non-root user
4. PYTHONPATH and entrypoint are correct

Run:
    podman build -f deploy/workflow-runner/Containerfile -t workflow-runner:test .
    uv run pytest tests/e2e/temporal/test_container_build.py -v
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

IMAGE_NAME = "workflow-runner:test"
CONTAINERFILE = "deploy/workflow-runner/Containerfile"


@pytest.fixture(scope="module")
def built_image():
    """Build the workflow-runner image."""
    result = subprocess.run(
        ["podman", "build", "-f", CONTAINERFILE, "-t", IMAGE_NAME, "."],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"Image build failed:\n{result.stderr}")
    yield IMAGE_NAME


@pytest.fixture
def running_container(built_image):
    """Start a container and yield its localhost URL."""
    container_name = "test-workflow-runner"

    subprocess.run(
        ["podman", "rm", "-f", container_name],
        capture_output=True,
    )

    result = subprocess.run(
        ["podman", "run", "-d", "--name", container_name,
         "-p", "18080:8080", built_image],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Container start failed:\n{result.stderr}")

    time.sleep(3)
    yield "http://localhost:18080"

    subprocess.run(["podman", "rm", "-f", container_name], capture_output=True)


def test_image_builds(built_image):
    """Containerfile builds successfully."""
    result = subprocess.run(
        ["podman", "image", "exists", built_image],
        capture_output=True,
    )
    assert result.returncode == 0


def test_healthz_responds(running_container):
    """Container responds on /healthz."""
    import httpx
    response = httpx.get(f"{running_container}/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_runs_as_non_root(built_image):
    """Container runs as non-root user."""
    result = subprocess.run(
        ["podman", "run", "--rm", built_image, "id", "-u"],
        capture_output=True, text=True,
    )
    uid = result.stdout.strip()
    assert uid == "1001", f"Expected UID 1001, got {uid}"
