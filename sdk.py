"""Android SDK（platform-tools と Desktop Head Unit）の検出とインストール。

Android Studio が無くても、Google の SDK リポジトリから必要な 2 パッケージだけを
取得して、Android Studio と同じ標準の場所（%LOCALAPPDATA%\\Android\\Sdk）に展開する。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_BASE = "https://dl.google.com/android/repository/"
REPO_XML = REPO_BASE + "repository2-3.xml"
PKG_PLATFORM_TOOLS = "platform-tools"
PKG_DHU = "extras;google;auto"
GEARHEAD_PKG = "com.google.android.projection.gearhead"


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Android" / "Sdk"


def sdk_candidates(custom: str | None = None) -> list[Path]:
    paths: list[Path] = []
    if custom:
        paths.append(Path(custom))
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(key):
            paths.append(Path(os.environ[key]))
    paths.append(default_install_dir())
    return paths


def adb_in(sdk: Path) -> Path:
    return sdk / "platform-tools" / "adb.exe"


def dhu_dir_in(sdk: Path) -> Path:
    return sdk / "extras" / "google" / "auto"


def dhu_in(sdk: Path) -> Path:
    return dhu_dir_in(sdk) / "desktop-head-unit.exe"


def read_revision(pkg_dir: Path) -> str | None:
    props = pkg_dir / "source.properties"
    try:
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Pkg.Revision="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


@dataclass
class SdkStatus:
    root: Path
    adb: Path | None
    dhu: Path | None
    adb_rev: str | None
    dhu_rev: str | None


def detect(custom: str | None = None) -> SdkStatus:
    """候補の中で最初に何か見つかった SDK を返す。何も無ければ既定のインストール先。"""
    for root in sdk_candidates(custom):
        adb, dhu = adb_in(root), dhu_in(root)
        if adb.exists() or dhu.exists():
            return SdkStatus(
                root=root,
                adb=adb if adb.exists() else _adb_on_path(),
                dhu=dhu if dhu.exists() else None,
                adb_rev=read_revision(adb.parent) if adb.exists() else None,
                dhu_rev=read_revision(dhu.parent) if dhu.exists() else None,
            )
    root = Path(custom) if custom else default_install_dir()
    return SdkStatus(root=root, adb=_adb_on_path(), dhu=None, adb_rev=None, dhu_rev=None)


def _adb_on_path() -> Path | None:
    found = shutil.which("adb")
    return Path(found) if found else None


# ---- リモートリポジトリ ----------------------------------------------------

@dataclass
class RemotePackage:
    path: str
    revision: str
    url: str
    size: int
    sha1: str
    license_id: str


def _name(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _name(c) == name:
            return c
    return None


def _text(el: ET.Element | None, name: str, default: str = "") -> str:
    c = _child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None and c.text else default


def fetch_remote(timeout: float = 30) -> tuple[dict[str, RemotePackage], dict[str, str]]:
    """安定版チャンネル（channel-0）の Windows 用パッケージ情報とライセンス文を返す。"""
    with urllib.request.urlopen(REPO_XML, timeout=timeout) as r:
        root = ET.fromstring(r.read())

    licenses = {el.get("id", ""): (el.text or "").strip() for el in root if _name(el) == "license"}
    packages: dict[str, RemotePackage] = {}
    for pkg in root:
        if _name(pkg) != "remotePackage" or pkg.get("path") not in (PKG_PLATFORM_TOOLS, PKG_DHU):
            continue
        channel = _child(pkg, "channelRef")
        if channel is None or channel.get("ref") != "channel-0":
            continue
        rev_el = _child(pkg, "revision")
        rev = ".".join(p for p in (_text(rev_el, "major"), _text(rev_el, "minor"), _text(rev_el, "micro")) if p)
        lic = _child(pkg, "uses-license")
        archives = _child(pkg, "archives")
        for arc in list(archives) if archives is not None else []:
            if _text(arc, "host-os") != "windows" or _text(arc, "host-arch", "x64") != "x64":
                continue
            complete = _child(arc, "complete")
            checksum = _child(complete, "checksum") if complete is not None else None
            packages[pkg.get("path", "")] = RemotePackage(
                path=pkg.get("path", ""),
                revision=rev,
                url=REPO_BASE + _text(complete, "url"),
                size=int(_text(complete, "size", "0")),
                sha1=(checksum.text or "").strip() if checksum is not None else "",
                license_id=lic.get("ref", "") if lic is not None else "",
            )
    return packages, licenses


def install(pkg: RemotePackage, sdk_root: Path, progress: Callable[[str, int, int], None]) -> Path:
    """パッケージをダウンロード・検証して SDK 内の決まった場所へ展開する。展開先を返す。"""
    sdk_root.mkdir(parents=True, exist_ok=True)
    if pkg.path == PKG_PLATFORM_TOOLS:
        dest, inner = sdk_root / "platform-tools", "platform-tools"
    elif pkg.path == PKG_DHU:
        dest, inner = dhu_dir_in(sdk_root), ""
    else:
        raise ValueError(pkg.path)

    with tempfile.TemporaryDirectory(dir=sdk_root, prefix=".aa-dl-") as tmp:
        zip_path = Path(tmp) / "pkg.zip"
        sha1 = hashlib.sha1()
        done = 0
        with urllib.request.urlopen(pkg.url, timeout=60) as r, open(zip_path, "wb") as f:
            while chunk := r.read(256 * 1024):
                f.write(chunk)
                sha1.update(chunk)
                done += len(chunk)
                progress(pkg.path, done, pkg.size)
        if pkg.sha1 and sha1.hexdigest() != pkg.sha1:
            raise RuntimeError(f"{pkg.path}: チェックサムが一致しません（ダウンロードが壊れています）")

        staging = Path(tmp) / "x"
        _safe_extract(zip_path, staging)
        src = staging / inner if inner else staging
        if not src.is_dir():
            raise RuntimeError(f"{pkg.path}: 想定外のアーカイブ構成です")

        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    return dest


def _safe_extract(zip_path: Path, out: Path) -> None:
    out = out.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            target = (out / member.filename).resolve()
            if out != target and out not in target.parents:
                raise RuntimeError(f"不正なパスを含むアーカイブです: {member.filename}")
        z.extractall(out)
