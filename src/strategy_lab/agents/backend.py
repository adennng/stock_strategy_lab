from __future__ import annotations

from pathlib import Path

from deepagents.backends.filesystem import _raise_if_symlink_loop
from deepagents.backends.local_shell import LocalShellBackend


class WindowsSafeLocalShellBackend(LocalShellBackend):
    r"""LocalShellBackend with Windows extended-path normalization.

    On Windows, pathlib may occasionally resolve a path as ``\\?\D:\...`` while
    the backend root remains ``D:\...``. The upstream virtual-mode containment
    check then treats the same directory as outside the root. This subclass keeps
    the same semantics but normalizes both sides before comparing.
    """

    def _resolve_path(self, key: str) -> Path:
        if not self.virtual_mode:
            return super()._resolve_path(key)

        vpath = key if key.startswith("/") else "/" + key
        if ".." in vpath or vpath.startswith("~"):
            raise ValueError("Path traversal not allowed")

        full = (self.cwd / vpath.lstrip("/")).resolve()
        safe_full = Path(_strip_windows_extended_prefix(str(full)))
        safe_root = Path(_strip_windows_extended_prefix(str(self.cwd)))
        try:
            safe_full.relative_to(safe_root)
        except ValueError:
            msg = f"Path:{full} outside root directory: {self.cwd}"
            raise ValueError(msg) from None
        _raise_if_symlink_loop(safe_full)
        return safe_full


def _strip_windows_extended_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path
