"""Tests for the read_file built-in tool."""

from __future__ import annotations

from pathlib import Path


class TestReadFileRegistration:
    """Tests that read_file is properly registered."""

    def test_registered_after_import(self) -> None:
        """Importing read_file module registers 'read_file' tool."""
        from cloud_agents.workflow.executor.step.tools import list_tools

        import cloud_agents.tools.read_file  # noqa: F401

        assert "read_file" in list_tools()


class TestReadFile:
    """Tests for read_file tool function behavior."""

    def test_reads_file_contents(self, tmp_path: Path) -> None:
        """read_file returns file contents as string."""
        from cloud_agents.tools.read_file import read_file

        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello, world!", encoding="utf-8")

        result = read_file(str(test_file))
        assert result == "Hello, world!"

    def test_nonexistent_file_returns_error(self) -> None:
        """read_file returns error string for missing file."""
        from cloud_agents.tools.read_file import read_file

        result = read_file("/nonexistent/path/file.txt")
        assert "not found" in result.lower()

    def test_size_limit_enforcement(self, tmp_path: Path) -> None:
        """read_file returns error when file exceeds size limit."""
        from cloud_agents.tools.read_file import read_file

        large_file = tmp_path / "large.txt"
        large_file.write_bytes(b"x" * (1_048_577))  # Just over 1 MB

        result = read_file(str(large_file))
        assert "too large" in result.lower()

    def test_custom_encoding(self, tmp_path: Path) -> None:
        """read_file uses the specified encoding."""
        from cloud_agents.tools.read_file import read_file

        test_file = tmp_path / "latin1.txt"
        test_file.write_bytes("caf\xe9".encode("latin-1"))

        result = read_file(str(test_file), encoding="latin-1")
        assert result == "caf\xe9"

    def test_permission_denied_returns_error(self, tmp_path: Path) -> None:
        """read_file returns error string for unreadable file."""
        from cloud_agents.tools.read_file import read_file

        test_file = tmp_path / "secret.txt"
        test_file.write_text("secret", encoding="utf-8")
        test_file.chmod(0o000)

        result = read_file(str(test_file))
        assert "permission denied" in result.lower()

        # Restore permissions for cleanup
        test_file.chmod(0o644)


class TestReadFilePathRestriction:
    """Tests for path traversal protection."""

    def test_blocks_path_outside_base_dir(self, tmp_path: Path, monkeypatch: object) -> None:
        """read_file rejects paths outside CLOUD_AGENTS_READ_FILE_BASE_DIR."""
        import cloud_agents.tools.read_file as mod

        monkeypatch.setattr(mod, "_ALLOWED_BASE_DIR", str(tmp_path))

        result = mod.read_file("/etc/passwd")
        assert "outside allowed directory" in result.lower()

    def test_allows_path_inside_base_dir(self, tmp_path: Path, monkeypatch: object) -> None:
        """read_file allows paths under CLOUD_AGENTS_READ_FILE_BASE_DIR."""
        import cloud_agents.tools.read_file as mod

        monkeypatch.setattr(mod, "_ALLOWED_BASE_DIR", str(tmp_path))

        test_file = tmp_path / "allowed.txt"
        test_file.write_text("allowed content")

        result = mod.read_file(str(test_file))
        assert result == "allowed content"

    def test_blocks_symlink_escape(self, tmp_path: Path, monkeypatch: object) -> None:
        """read_file blocks symlinks that escape the base directory."""
        import cloud_agents.tools.read_file as mod

        monkeypatch.setattr(mod, "_ALLOWED_BASE_DIR", str(tmp_path))

        link = tmp_path / "escape"
        link.symlink_to("/etc/hostname")

        result = mod.read_file(str(link))
        assert "outside allowed directory" in result.lower()

    def test_no_restriction_when_base_dir_unset(self, tmp_path: Path, monkeypatch: object) -> None:
        """read_file has no path restriction when CLOUD_AGENTS_READ_FILE_BASE_DIR is empty."""
        import cloud_agents.tools.read_file as mod

        monkeypatch.setattr(mod, "_ALLOWED_BASE_DIR", "")

        test_file = tmp_path / "anywhere.txt"
        test_file.write_text("content")

        result = mod.read_file(str(test_file))
        assert result == "content"
