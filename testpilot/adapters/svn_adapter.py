"""SVN adapter used by change-analysis and Agent RPC tools.

All subprocess execution happens in the TestPilot main process. Agent runtimes never
run svn directly; they can only invoke the bounded agent.svn.* RPC methods.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from testpilot.core.errors import SvnError

_REV_RE = re.compile(r"^(?:HEAD|BASE|COMMITTED|PREV|[0-9]+)$", re.IGNORECASE)


class SvnAdapter:
    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(cfg or {})
        self.executable = str(self.cfg.get("executable") or "svn").strip() or "svn"
        self.timeout_s = max(5, int(self.cfg.get("timeout_s") or 30))
        self.max_output_chars = max(10_000, int(self.cfg.get("max_output_chars") or 800_000))

    def executable_path(self) -> str | None:
        raw = os.path.expandvars(os.path.expanduser(self.executable))
        path = Path(raw)
        if path.is_file():
            return str(path.resolve())
        return shutil.which(raw)

    def health(self) -> dict:
        exe = self.executable_path()
        if not exe:
            return {"ok": False, "detail": f"SVN 不可执行: {self.executable!r}"}
        try:
            out = self._run(["--version", "--quiet"], cwd=None, limit=4096)
            return {"ok": True, "detail": out.strip() or exe}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    def info(self, working_copy: str | Path) -> dict:
        root = _working_copy(working_copy)
        text = self._run(["info", "--xml"], cwd=root, limit=100_000)
        try:
            entry = ET.fromstring(text).find("entry")
        except ET.ParseError as exc:
            raise SvnError(f"svn info XML 无法解析: {exc}") from exc
        if entry is None:
            raise SvnError("svn info 未返回 entry")
        repo = entry.find("repository")
        return {
            "path": str(root),
            "revision": entry.get("revision"),
            "url": (entry.findtext("url") or "").strip(),
            "repository_root": (repo.findtext("root") if repo is not None else "") or "",
            "repository_uuid": (repo.findtext("uuid") if repo is not None else "") or "",
        }

    def status(self, working_copy: str | Path) -> list[dict]:
        root = _working_copy(working_copy)
        text = self._run(["status", "--xml"], cwd=root, limit=400_000)
        try:
            xml = ET.fromstring(text)
        except ET.ParseError as exc:
            raise SvnError(f"svn status XML 无法解析: {exc}") from exc
        out = []
        for entry in xml.findall(".//entry"):
            wc = entry.find("wc-status")
            if wc is None:
                continue
            item = str(wc.get("item") or "")
            if item in ("normal", "none", "unversioned") and item != "unversioned":
                continue
            out.append({
                "path": str(entry.get("path") or ""),
                "item": item,
                "props": str(wc.get("props") or ""),
                "revision": wc.get("revision"),
                "tree_conflicted": str(wc.get("tree-conflicted") or "").lower() == "true",
            })
        return out

    def summarize(self, working_copy: str | Path, *, from_revision=None,
                  to_revision=None) -> dict:
        root = _working_copy(working_copy)
        rev_args = _revision_args(from_revision, to_revision)
        args = ["diff", "--summarize", *rev_args]
        text = self._run(args, cwd=root, limit=300_000)
        changed = []
        for line in text.splitlines():
            raw = line.rstrip()
            if not raw:
                continue
            # SVN summarize starts with text/prop status columns followed by the path.
            status = raw[:2].strip() or raw[:1].strip()
            path = raw[2:].strip() if len(raw) > 2 else raw.strip()
            changed.append({"status": status, "path": path})
        info = self.info(root)
        return {
            "working_copy": str(root),
            "from_revision": _revision(from_revision),
            "to_revision": _revision(to_revision),
            "info": info,
            "changed": changed,
            "working_status": self.status(root) if not rev_args else [],
        }

    def diff(self, working_copy: str | Path, *, from_revision=None, to_revision=None,
             paths: list[str] | None = None, max_chars: int | None = None) -> dict:
        root = _working_copy(working_copy)
        args = ["diff", *_revision_args(from_revision, to_revision)]
        safe_paths = []
        for raw in paths or []:
            rel = _relative_path(root, raw)
            safe_paths.append(rel)
        args.extend(safe_paths)
        limit = min(self.max_output_chars, max(10_000, int(max_chars or self.max_output_chars)))
        text = self._run(args, cwd=root, limit=limit)
        return {
            "text": text,
            "truncated": len(text) >= limit,
            "chars": len(text),
            "paths": safe_paths,
            "from_revision": _revision(from_revision),
            "to_revision": _revision(to_revision),
        }

    def diff_digest(self, working_copy: str | Path, *, from_revision=None,
                    to_revision=None) -> dict:
        """Return an exact digest of the complete SVN diff without retaining its text.

        Change-analysis uses this as a snapshot guard. Unlike diff(), this path is
        intentionally not truncated: a long diff must still produce a stable signature.
        """
        root = _working_copy(working_copy)
        args = ["diff", *_revision_args(from_revision, to_revision)]
        exe = self.executable_path()
        if not exe:
            raise SvnError(f"SVN 不可执行: {self.executable!r}")
        try:
            proc = subprocess.run(
                [exe, *args], cwd=str(root),
                capture_output=True, timeout=self.timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SvnError(f"SVN 命令超时（{self.timeout_s}s）: {args[:4]}") from exc
        except OSError as exc:
            raise SvnError(f"启动 SVN 失败: {exc}") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace")
            stdout = proc.stdout.decode("utf-8", "replace")
            raise SvnError(
                f"SVN 命令失败({proc.returncode}): {(stderr or stdout)[:1000]}")
        return {
            "sha256": hashlib.sha256(proc.stdout).hexdigest(),
            "bytes": len(proc.stdout),
            "from_revision": _revision(from_revision),
            "to_revision": _revision(to_revision),
        }

    def log(self, working_copy: str | Path, *, from_revision=None, to_revision=None,
            limit: int = 20) -> list[dict]:
        root = _working_copy(working_copy)
        args = ["log", "--xml", "-l", str(max(1, min(int(limit), 100)))]
        revs = _revision_args(from_revision, to_revision)
        if revs:
            args.extend(revs)
        text = self._run(args, cwd=root, limit=300_000)
        try:
            xml = ET.fromstring(text)
        except ET.ParseError as exc:
            raise SvnError(f"svn log XML 无法解析: {exc}") from exc
        out = []
        for item in xml.findall("logentry"):
            out.append({
                "revision": item.get("revision"),
                "author": item.findtext("author") or "",
                "date": item.findtext("date") or "",
                "message": item.findtext("msg") or "",
            })
        return out

    def _run(self, args: list[str], *, cwd: Path | None, limit: int) -> str:
        exe = self.executable_path()
        if not exe:
            raise SvnError(f"SVN 不可执行: {self.executable!r}")
        try:
            proc = subprocess.run(
                [exe, *args], cwd=str(cwd) if cwd else None,
                capture_output=True, timeout=self.timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SvnError(f"SVN 命令超时（{self.timeout_s}s）: {args[:4]}") from exc
        except OSError as exc:
            raise SvnError(f"启动 SVN 失败: {exc}") from exc
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise SvnError(f"SVN 命令失败({proc.returncode}): {(stderr or stdout)[:1000]}")
        if len(stdout) > limit:
            return stdout[:limit]
        return stdout


def _working_copy(value: str | Path) -> Path:
    path = Path(str(value or "")).expanduser().resolve()
    if not path.is_dir():
        raise SvnError(f"SVN 工作副本目录不存在: {path}")
    return path


def _revision(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().upper()
    if not _REV_RE.fullmatch(text):
        raise SvnError(f"非法 SVN revision: {value!r}")
    return text


def _revision_args(from_revision, to_revision) -> list[str]:
    start = _revision(from_revision)
    end = _revision(to_revision)
    if start and end:
        return ["-r", f"{start}:{end}"]
    if end and not start:
        return ["-c", end]
    if start and not end:
        return ["-r", f"{start}:HEAD"]
    return []


def _relative_path(root: Path, value: str) -> str:
    raw = Path(str(value or "").strip())
    path = raw if raw.is_absolute() else root / raw
    try:
        rel = path.resolve().relative_to(root)
    except (OSError, ValueError) as exc:
        raise SvnError(f"路径不在工作副本内: {value!r}") from exc
    # RPC/Agent-facing paths use repository-style "/" on every OS. SVN accepts
    # forward slashes on Windows, and this keeps prompts/results stable cross-platform.
    return rel.as_posix()
