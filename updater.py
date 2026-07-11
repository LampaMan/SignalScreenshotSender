import os
import re
import subprocess
import sys
from typing import Optional, Tuple


def parse_version(value: str) -> Tuple[int, ...]:
    if not value:
        return (0,)
    cleaned = value.strip().lower()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts)


def compare_versions(local_version: str, remote_version: str) -> int:
    local = parse_version(local_version)
    remote = parse_version(remote_version)
    max_len = max(len(local), len(remote))
    local_padded = local + (0,) * (max_len - len(local))
    remote_padded = remote + (0,) * (max_len - len(remote))
    if local_padded < remote_padded:
        return -1
    if local_padded > remote_padded:
        return 1
    return 0


class RepositoryUpdater:
    def __init__(self, repo_url: str, repo_dir: Optional[str] = None):
        self.repo_url = repo_url.rstrip("/")
        self.repo_dir = repo_dir or os.getcwd()

    def current_version(self) -> str:
        version_file = os.path.join(self.repo_dir, "version.txt")
        if not os.path.exists(version_file):
            return "0"
        with open(version_file, "r", encoding="utf-8") as fh:
            content = fh.read()
        match = re.search(r"(\d+(?:\.\d+){1,3})", content)
        if match:
            return match.group(1)
        return "0"

    def fetch_remote_version(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--tags", self.repo_url],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            tags = []
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].startswith("refs/tags/"):
                    tag = parts[1].split("/", 2)[-1]
                    tags.append(tag)
            if not tags:
                return None
            return sorted(tags, key=parse_version)[-1]
        except Exception:
            return None

    def needs_update(self) -> bool:
        remote = self.fetch_remote_version()
        if not remote:
            return False
        return compare_versions(self.current_version(), remote) < 0

    def update(self, target_version: Optional[str] = None) -> dict:
        if not os.path.exists(os.path.join(self.repo_dir, ".git")):
            return {"ok": False, "error": "Repository is not a git checkout"}

        version_to_install = target_version or self.fetch_remote_version()
        if not version_to_install:
            return {"ok": False, "error": "No remote version available"}

        ref_name = version_to_install if version_to_install.startswith("refs/tags/") else f"refs/tags/{version_to_install}"

        try:
            subprocess.run(["git", "fetch", "--tags", "origin"], cwd=self.repo_dir, capture_output=True, text=True, timeout=120, check=True)
            subprocess.run(["git", "checkout", "--force", ref_name], cwd=self.repo_dir, capture_output=True, text=True, timeout=120, check=True)
            return {"ok": True, "message": f"Repository updated successfully to {version_to_install}"}
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": str(exc.stderr or exc.stdout or exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_self_update(self, target_version: Optional[str] = None) -> dict:
        if target_version is None and not self.needs_update():
            return {"ok": False, "message": "No update available"}
        return self.update(target_version=target_version)
