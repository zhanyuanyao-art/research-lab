"""Fetching public replication data (card 0159, docs/design/REPLICATION-v2.md 第二步).

A plan's data source is usually a Dataverse DOI, an OSF project, a GitHub repository or a file link. list_source()
reads what that source offers without downloading anything; fetch() downloads only the files Leo picked, unpacks a
zip safely, imports every readable data file through datasets.import_file, tags it with the plan's direction and
paper and attaches it to the plan.

Safety: every request (and every redirect hop) passes feeds.net's public-address check, with timeouts; a file is
refused above a size cap (500 MB unless set); a zip is unpacked member by member with no absolute or parent paths,
no links, a member count and a total size cap. Only data formats are listed; .rds needs R, which the lab does not
have (Leo, 10-13), and .xls needs a reader the lab does not ship, so both are listed but not fetched.

Card 0183 (REPLICATION-v3.md §2A): a source is listed as a package, not as data alone. Beside the data files the
listing holds the author's code (.R .Rmd .do .py .sql .sh) and the README / codebook documents; neither is ever
executed. A picked script or document is saved as text under the plan's workspace (author/, same paths as in the
repository, a PDF as its extracted text), data is imported as before. A file over its size cap is listed with the
reason and cannot be picked. plan_auto() reads the code (text only), asks package.py which scripts are the main
analysis and which data they read, and returns the default pick; fetch_auto() downloads exactly that.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from ..feeds import net
from . import datasets, package

log = logging.getLogger("research_lab.replication.fetch")
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_MB = 500                                   # a data file larger than this is refused (fetch(max_bytes=…) to change)
MB = 1024 * 1024
CODE_MAX = 2 * MB                              # a script or a plain-text document larger than this is not saved
PDF_MAX = 25 * MB                              # a PDF document (a codebook) larger than this is not saved
AUTO_MAX_MB = 50                               # the default pick leaves out a data file larger than this
SCAN_FILES = 300                               # scripts read to find the main analysis
SCAN_BYTES = 8 * MB                            # …and the most script bytes read for it, all files together
DOC_BYTES = 30 * MB                            # …and the most README / codebook bytes read for the hints
PDF_PAGES = 80
DOC_CHARS = 400_000
MAX_LIST = 2000                                # files listed from one source; folders deeper than this are left out
MAX_DEPTH = 6
ZIP_MEMBERS = 500
CHUNK = 1 << 16
DATA_EXTS = (".csv", ".tsv", ".xlsx", ".xls", ".dta", ".sav", ".rds", ".parquet", ".json", ".zip")
UNSUPPORTED = {".rds": "需要 R", ".xls": "旧版 Excel，另存为 .xlsx 后拖进来"}
RESTRICTED_NOTE = "来源标为受限，需要向作者申请"
DATAVERSE_HOSTS = {"10.7910": "dataverse.harvard.edu"}   # DOI prefix → host for a bare Dataverse DOI
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#\"'<>]+)", re.I)


class FetchError(ValueError):
    """A message in Chinese that can be shown to Leo as it is."""


@dataclass
class Remote:
    name: str                      # path inside the source, e.g. "data/panel.csv"
    url: str                       # where the bytes are
    size: int | None = None
    note: str = ""                 # why it cannot be fetched (需要 R, over the size cap), empty when it can
    kind: str = "data"             # data | code | doc | other
    restricted: bool = False       # the source itself marks the file as restricted (Dataverse "restricted"); note says so

    @property
    def ext(self) -> str:
        return PurePosixPath(self.name.lower()).suffix

    @property
    def fetchable(self) -> bool:
        return not self.note


@dataclass
class Listing:
    source: str                    # dataverse | osf | github | direct
    title: str
    files: list[Remote] = field(default_factory=list)      # data files (what the numbers of --pick count)
    code: list[Remote] = field(default_factory=list)       # scripts, kept as text only
    docs: list[Remote] = field(default_factory=list)       # README, codebook
    other: list[Remote] = field(default_factory=list)      # everything else (figures, .RData…): named, never offered

    def add(self, remote: Remote) -> None:
        bucket = {"data": self.files, "code": self.code, "doc": self.docs}.get(remote.kind, self.other)
        if bucket is not self.other or len(self.other) < 2 * MAX_LIST:
            bucket.append(remote)

    @property
    def count(self) -> int:
        return len(self.files) + len(self.code) + len(self.docs)

    @property
    def everything(self) -> list[Remote]:
        return [*self.files, *self.code, *self.docs, *self.other]


@dataclass
class Imported:
    name: str                      # the file (or zip member) it came from
    dataset: str | None = None     # DS-YYYY-NNNN when imported
    error: str | None = None
    saved: str | None = None       # where a script or document went, under the plan's workspace (author/…)


def is_data(name: str) -> bool:
    return PurePosixPath(name.lower()).suffix in DATA_EXTS


def classify(name: str) -> str:
    if package.is_code(name):
        return "code"
    if is_data(name):
        return "data"
    return "doc" if package.is_doc(name) else "other"


def cap_of(kind: str, name: str, data_cap: int) -> int | None:
    """The largest size a file of this kind is downloaded at; None for files that are never downloaded."""
    if kind == "data":
        return data_cap
    if kind == "code" or (kind == "doc" and not name.lower().endswith(".pdf")):
        return CODE_MAX
    return PDF_MAX if kind == "doc" else None


def _remote(name: str, url: str, size: Any = None, cap: int | None = MAX_MB * MB, restricted: bool = False) -> Remote:
    """A listed file. `cap` (the data cap) greys out a file over its kind's size limit with the reason; None skips it.
    A file the source marks restricted is greyed out whatever its size: a download would only be refused."""
    ext = PurePosixPath(name.lower()).suffix
    kind = classify(name)
    size = int(size) if isinstance(size, (int, float)) or str(size or "").isdigit() else None
    note = UNSUPPORTED.get(ext, "") if kind == "data" else ""
    limit = cap_of(kind, name, cap) if cap is not None else None
    if not note and limit and size and size > limit:
        note = f"超过 {limit // MB} MB 上限，不下载"
    if restricted:
        note = RESTRICTED_NOTE
    return Remote(name, url, size, note, kind, restricted)


def _client(http: httpx.Client | None) -> httpx.Client:
    return net.guard_client(http) if http is not None else net.new_client(timeout=TIMEOUT)


def _get_json(http: httpx.Client, url: str) -> Any:
    if not net.public_url(url):
        raise FetchError("这个地址不是公开网址，不去访问")
    try:
        resp = http.get(url, headers={"User-Agent": net.BROWSER_UA, "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise FetchError(f"连不上：{net.error_reason(e)}") from e
    if resp.status_code == 404:
        raise FetchError("没找到：链接可能写错了，或者数据还没公开")
    if resp.status_code != 200:
        raise FetchError(f"对方返回 HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise FetchError("对方返回的不是 JSON") from e


# ---------------------------------------------------------------- resolvers

def _dataverse_target(src: str) -> tuple[str, str] | None:
    """(host, doi) for a bare Dataverse DOI or any Dataverse dataset URL with persistentId."""
    s = src.strip()
    parts = urlsplit(s)
    if parts.scheme in ("http", "https") and parts.hostname and parts.hostname not in ("doi.org", "dx.doi.org"):
        pid = (parse_qs(parts.query).get("persistentId") or [""])[0]
        if pid.lower().startswith("doi:"):
            return parts.hostname.lower(), pid[4:]
        return None
    m = DOI_RE.search(unquote(s))
    if m and m.group(1).split("/")[0] in DATAVERSE_HOSTS:
        return DATAVERSE_HOSTS[m.group(1).split("/")[0]], m.group(1).rstrip(".")
    return None


def list_dataverse(http: httpx.Client, host: str, doi: str, cap: int | None = MAX_MB * MB) -> Listing:
    data = _get_json(http, f"https://{host}/api/datasets/:persistentId/?persistentId={quote('doi:' + doi, safe=':/')}")
    version = ((data or {}).get("data") or {}).get("latestVersion") or {}
    fields = ((version.get("metadataBlocks") or {}).get("citation") or {}).get("fields") or []
    title = next((f.get("value") for f in fields if f.get("typeName") == "title" and isinstance(f.get("value"), str)), doi)
    out = Listing("dataverse", title)
    for f in version.get("files") or []:
        df = f.get("dataFile") or {}
        if not df.get("id"):
            continue
        # Dataverse turns tabular uploads into .tab; the original (.dta, .sav, .csv) is one parameter away
        original = df.get("originalFileName")
        name = original or df.get("filename") or f.get("label") or str(df["id"])
        folder = f.get("directoryLabel")
        path = f"{folder}/{name}" if folder else name
        url = f"https://{host}/api/access/datafile/{df['id']}" + ("?format=original" if original else "")
        out.add(_remote(path, url, df.get("originalFileSize") if original else df.get("filesize"), cap,
                        restricted=f.get("restricted") is True))
    return out


def list_osf(http: httpx.Client, node: str, cap: int | None = MAX_MB * MB) -> Listing:
    meta = _get_json(http, f"https://api.osf.io/v2/nodes/{node}/")
    title = (((meta or {}).get("data") or {}).get("attributes") or {}).get("title") or node
    out = Listing("osf", title)
    queue = [(f"https://api.osf.io/v2/nodes/{node}/files/osfstorage/", 0)]
    while queue and out.count < MAX_LIST:
        url, depth = queue.pop(0)
        while url and out.count < MAX_LIST:
            page = _get_json(http, url)
            for item in (page or {}).get("data") or []:
                a = item.get("attributes") or {}
                if a.get("kind") == "folder":
                    related = (((item.get("relationships") or {}).get("files") or {}).get("links") or {}).get("related") or {}
                    href = related.get("href") if isinstance(related, dict) else related
                    if href and depth < MAX_DEPTH:
                        queue.append((href, depth + 1))
                elif a.get("kind") == "file" and a.get("name"):
                    download = (item.get("links") or {}).get("download")
                    if download:
                        out.add(_remote((a.get("materialized_path") or a["name"]).lstrip("/"), download, a.get("size"), cap))
            url = ((page or {}).get("links") or {}).get("next")
    return out


def list_github(http: httpx.Client, owner: str, repo: str, ref: str | None, path: str,
                cap: int | None = MAX_MB * MB) -> Listing:
    out = Listing("github", f"{owner}/{repo}")
    queue = [(path.strip("/"), 0)]
    while queue and out.count < MAX_LIST:
        folder, depth = queue.pop(0)
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(folder)}" + (f"?ref={quote(ref)}" if ref else "")
        items = _get_json(http, url)
        if isinstance(items, dict):        # the path is a single file
            items = [items]
        for item in items or []:
            if item.get("type") == "dir" and depth < MAX_DEPTH:
                queue.append((item.get("path") or "", depth + 1))
            elif item.get("type") == "file" and item.get("name") and item.get("download_url"):
                out.add(_remote(item.get("path") or item["name"], item["download_url"], item.get("size"), cap))
    return out


def list_source(src: str, http: httpx.Client | None = None, cap: int | None = MAX_MB * MB) -> Listing:
    """What the source offers (data, code, documents), without downloading any. A file over its size limit is listed
    with the reason and cannot be picked; `cap` is the data limit, None leaves every file pickable."""
    client = _client(http)
    try:
        dv = _dataverse_target(src)
        if dv:
            return list_dataverse(client, *dv, cap)
        parts = urlsplit(src.strip())
        host = (parts.hostname or "").lower()
        bits = [b for b in parts.path.split("/") if b]
        if host in ("osf.io", "www.osf.io") and bits:
            return list_osf(client, bits[0], cap)
        if host in ("github.com", "www.github.com") and len(bits) >= 2:
            ref = bits[3] if len(bits) >= 4 and bits[2] in ("tree", "blob") else None
            path = "/".join(bits[4:]) if ref else ""
            return list_github(client, bits[0], bits[1].removesuffix(".git"), ref, path, cap)
        if parts.scheme in ("http", "https") and is_data(parts.path):
            if not net.public_url(src):
                raise FetchError("这个地址不是公开网址，不去访问")
            return Listing("direct", PurePosixPath(unquote(parts.path)).name,
                           [_remote(PurePosixPath(unquote(parts.path)).name, src, None, cap)])
        raise FetchError("认不出这个来源：支持 Dataverse DOI 或链接、OSF 项目、GitHub 仓库，以及直接指向数据文件的链接")
    finally:
        if http is None:
            client.close()


# ---------------------------------------------------------------- download, unzip, import

def _inside(dest: Path, name: str) -> Path:
    """A flattened file name for `name` (a path the source chose) under `dest`, never outside it. The parts are
    joined by "__" so a/data.csv and b/data.csv stay apart; import takes the part after the last "__". Root, empty,
    "." and ".." parts are dropped and control characters replaced, so "/etc/x.csv" becomes "etc__x.csv"."""
    parts = [re.sub(r"[\x00-\x1f]", "_", part) for part in PurePosixPath(name.replace("\\", "/")).parts
             if part not in ("/", "", ".", "..")]
    target = dest / ("__".join(parts)[-120:] or "download")
    if target.resolve().parent != dest.resolve():
        raise FetchError(f"文件名不安全，不保存：{name}")
    return target


def download(remote: Remote, dest: Path, http: httpx.Client, max_bytes: int) -> Path:
    """Stream one file into `dest` (a folder), refusing it above `max_bytes`."""
    if not net.public_url(remote.url):
        raise FetchError("这个地址不是公开网址，不去访问")
    if remote.size and remote.size > max_bytes:
        raise FetchError(f"文件 {remote.size // (1024 * 1024)} MB，超过上限 {max_bytes // (1024 * 1024)} MB")
    out = _inside(dest, remote.name)
    try:
        with http.stream("GET", remote.url, headers={"User-Agent": net.BROWSER_UA}) as resp:
            if resp.status_code != 200:
                raise FetchError(f"下载失败：HTTP {resp.status_code}")
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchError(f"文件超过上限 {max_bytes // (1024 * 1024)} MB")
            size = 0
            with open(out, "wb") as fh:
                for chunk in resp.iter_bytes(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"文件超过上限 {max_bytes // (1024 * 1024)} MB，已停止下载")
                    fh.write(chunk)
    except httpx.HTTPError as e:
        out.unlink(missing_ok=True)
        raise FetchError(f"下载失败：{net.error_reason(e)}") from e
    except FetchError:
        out.unlink(missing_ok=True)
        raise
    return out


def unzip(archive: Path, dest: Path, max_bytes: int) -> list[Path]:
    """The data files of a zip, extracted under `dest` with flattened, sanitised names. Refuses the whole archive on
    an absolute or parent path, a link, too many members or more than `max_bytes` unpacked."""
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, ValueError, OSError) as e:
        raise FetchError("压缩包坏了，打不开") from e
    with zf:
        members = zf.infolist()
        if len(members) > ZIP_MEMBERS:
            raise FetchError(f"压缩包里有 {len(members)} 个文件，超过 {ZIP_MEMBERS} 个")
        total = 0
        for m in members:
            name = m.filename.replace("\\", "/")
            p = PurePosixPath(name)
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in p.parts:
                raise FetchError(f"压缩包里有越界路径：{m.filename}")
            if stat.S_ISLNK(m.external_attr >> 16):
                raise FetchError(f"压缩包里有链接文件：{m.filename}")
            total += m.file_size
        if total > max_bytes:
            raise FetchError(f"压缩包解开后超过上限 {max_bytes // (1024 * 1024)} MB")
        out, used = [], set()
        for m in members:
            p = PurePosixPath(m.filename.replace("\\", "/"))
            if m.is_dir() or not is_data(p.name) or p.suffix.lower() in (".zip", *UNSUPPORTED) or p.name.startswith("._"):
                continue
            target = _inside(dest, str(p))
            while target.name in used:
                target = target.with_name("_" + target.name)
            used.add(target.name)
            written = 0
            try:
                with zf.open(m) as src, open(target, "wb") as dst:
                    while chunk := src.read(CHUNK):
                        written += len(chunk)
                        if written > max_bytes:        # the header lied about the size
                            raise FetchError("压缩包解开后超过上限")
                        dst.write(chunk)
            except FetchError:
                raise
            except RuntimeError as e:                  # zipfile raises it for an encrypted member
                raise FetchError(f"压缩包里的 {p.name} 加了密码，打不开") from e
            except NotImplementedError as e:
                raise FetchError(f"压缩包里的 {p.name} 用了不支持的压缩方式") from e
            except (zipfile.BadZipFile, zlib.error, ValueError, OSError) as e:
                raise FetchError(f"压缩包里的 {p.name} 坏了，读不出来") from e
            out.append(target)
        return out


def _importable(path: Path) -> Path:
    """A path import_file can read: .json becomes a CSV next to it; everything else as it is."""
    if path.suffix.lower() != ".json":
        return path
    import pandas as pd
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise FetchError("JSON 文件读不了") from e
    rows = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows[:50]):
        raise FetchError("JSON 不是一张表（需要一组对象）")
    out = path.with_suffix(".csv")
    pd.json_normalize(rows).to_csv(out, index=False)
    return out


def pick(listing: Listing, picks: list[str]) -> list[Remote]:
    """The listed files Leo chose: a data file by number (1-based, as listed) or any file by name; an unknown pick is
    an error, never a guess."""
    pool = [*listing.files, *listing.code, *listing.docs]
    chosen = []
    for p in picks:
        if p.isdigit() and 1 <= int(p) <= len(listing.files):
            r = listing.files[int(p) - 1]
        else:
            matches = [f for f in pool if f.name == p or PurePosixPath(f.name).name == p]
            if len(matches) != 1:
                raise FetchError(f"没有这个文件，或者名字不唯一：{p}")
            r = matches[0]
        if not r.fetchable:
            raise FetchError(f"{r.name} 暂时取不了：{r.note}")
        if r not in chosen:
            chosen.append(r)
    return chosen


# ---------------------------------------------------------------- code and documents: text only

class _Budget:
    """A byte allowance shared by the reads of several threads: a read stops as soon as the allowance is spent."""

    def __init__(self, total: int):
        self.left = total
        self.lock = threading.Lock()

    def take(self, n: int) -> bool:
        with self.lock:
            self.left -= n
            return self.left >= 0


def _read_bytes(remote: Remote, http: httpx.Client, limit: int, budget: _Budget | None = None) -> bytes:
    """A small file's bytes, refused above `limit` (checked on the way in, whatever the server declared). With a
    budget the read also stops when the shared allowance is spent."""
    if not net.public_url(remote.url):
        raise FetchError("这个地址不是公开网址，不去访问")
    if remote.size and remote.size > limit:
        raise FetchError(f"文件 {remote.size // MB} MB，超过 {limit // MB} MB 上限")
    buf = bytearray()
    try:
        with http.stream("GET", remote.url, headers={"User-Agent": net.BROWSER_UA}) as resp:
            if resp.status_code != 200:
                raise FetchError(f"下载失败：HTTP {resp.status_code}")
            for chunk in resp.iter_bytes(CHUNK):
                buf += chunk
                if len(buf) > limit:
                    raise FetchError(f"文件超过 {limit // MB} MB 上限，已停止下载")
                if budget and not budget.take(len(chunk)):
                    raise FetchError("读代码的总量已到上限，停止")
    except httpx.HTTPError as e:
        raise FetchError(f"下载失败：{net.error_reason(e)}") from e
    return bytes(buf)


def _as_text(raw: bytes, name: str) -> str:
    """The text of a script, README or PDF. A file with NUL bytes is not text and is refused; a script in a legacy
    encoding is read as Latin-1 rather than dropped."""
    if name.lower().endswith(".pdf"):
        try:
            import pymupdf
            with pymupdf.open(stream=raw, filetype="pdf") as doc:
                pages = [page.get_text() for page in doc if page.number < PDF_PAGES]
        except Exception as e:  # noqa: BLE001 - pymupdf raises its own error types for a damaged file
            raise FetchError("PDF 读不出文字，可能是扫描件或坏了") from e
        return "\n".join(pages)[:DOC_CHARS]
    if b"\x00" in raw[:8192]:
        raise FetchError("不是文本文件，不保存")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _author_path(name: str, text_of_pdf: bool) -> Path:
    """Where a script or document lives under the workspace: author/<its path in the source>, each part cleaned, a
    PDF as <name>.pdf.txt. Root, empty, "." and ".." parts are dropped."""
    parts = [re.sub(r"[\x00-\x1f]", "_", part)[:120] for part in PurePosixPath(name.replace("\\", "/")).parts
             if part not in ("/", "", ".", "..")]
    if not parts:
        raise FetchError(f"文件名不安全，不保存：{name}")
    if text_of_pdf:
        parts[-1] += ".txt"
    return Path("author", *parts)


def _make_dirs(ws: Path, rel: Path) -> None:
    """mkdir -p ws/rel one folder at a time, looking at each before going on: a link or a file where a folder should be
    (one planted in the workspace) is refused before anything is created beyond it, so no empty folders appear outside."""
    if os.path.islink(ws):
        raise FetchError(f"工作区是链接，不存：{ws.name}")
    ws.mkdir(parents=True, exist_ok=True)
    here = ws
    for part in rel.parts:
        here = here / part
        try:
            os.mkdir(here)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(os.lstat(here).st_mode):
            raise FetchError(f"工作区里的 {part} 是链接或文件，不存")


def save_text(plan_id: str, remote: Remote, text: str) -> str:
    """Write a script or document into the plan's workspace as text and return its path under it. The file is data
    for the AI to read: it is never given an execute bit and nothing here runs it."""
    from . import workspace
    rel = _author_path(remote.name, remote.name.lower().endswith(".pdf"))
    ws = workspace.workspace_dir(plan_id)
    try:
        _make_dirs(ws, rel.parent)
        workspace.safe_write(ws, rel, text)
    except (workspace.WorkspaceError, OSError) as e:
        raise FetchError(f"存不进工作区：{e}") from e
    return rel.as_posix()


def _fetch_text(conn: sqlite3.Connection, remote: Remote, plan_id: str | None, client: httpx.Client,
                texts: dict[str, str] | None = None) -> Imported:
    if not plan_id:
        return Imported(remote.name, error="代码和说明书要存进复现方案的工作区，请先指定方案")
    try:
        text = (texts or {}).get(remote.name)
        if text is None:
            text = _as_text(_read_bytes(remote, client, cap_of(remote.kind, remote.name, 0) or CODE_MAX), remote.name)
        return Imported(remote.name, saved=save_text(plan_id, remote, text))
    except FetchError as e:
        return Imported(remote.name, error=str(e))


def _fetch_data(conn: sqlite3.Connection, remote: Remote, src: str, plan: dict[str, Any] | None, plan_id: str | None,
                client: httpx.Client, work: Path, max_bytes: int) -> list[Imported]:
    try:
        path = download(remote, work, client, max_bytes)
    except FetchError as e:
        return [Imported(remote.name, error=str(e))]
    return _import_path(conn, path, remote.name, f"取自 {src}" + (f"（{remote.name}）" if remote.name else ""),
                        plan, plan_id, work, max_bytes)


def _import_path(conn: sqlite3.Connection, path: Path, name: str, note: str, plan: dict[str, Any] | None,
                 plan_id: str | None, work: Path, max_bytes: int, original_name: str | None = None) -> list[Imported]:
    """Import a data file already under `work` (a zip member by member) as datasets tagged with the plan's paper and
    direction, and attach each to the plan. `name` labels the results; `original_name` names a single file's dataset
    (a zip member or a JSON turned CSV is named after the file import reads)."""
    from . import plans
    try:
        if path.suffix.lower() == ".zip":
            inner = work / f"{path.name}.d"
            inner.mkdir()
            files = unzip(path, inner, max_bytes)
        else:
            files = [path]
    except FetchError as e:
        return [Imported(name, error=str(e))]
    if not files:
        return [Imported(name, error="压缩包里没有能读的数据文件")]
    results: list[Imported] = []
    for f in files:
        label = name if f == path else f"{name} › {f.name.split('__')[-1]}"
        try:
            ready = _importable(f)
            named = ready.name.split("__")[-1]
            if f == path and original_name and PurePosixPath(original_name.lower()).suffix == ready.suffix.lower():
                named = original_name
            ds_id = datasets.import_file(conn, ready, original_name=named,
                                         paper_id=(plan or {}).get("paper_id"), direction=(plan or {}).get("direction"),
                                         note=note)
        except (FetchError, datasets.DatasetError) as e:
            results.append(Imported(label, error=str(e)))
            continue
        if plan_id:
            plans.attach_dataset(conn, plan_id, ds_id)
        results.append(Imported(label, dataset=ds_id))
    return results


def _download_all(conn: sqlite3.Connection, src: str, chosen: list[Remote], plan_id: str | None, client: httpx.Client,
                  max_bytes: int, texts: dict[str, str] | None = None) -> list[Imported]:
    from . import plans
    plan = plans.get(conn, plan_id) if plan_id else None
    work = Path(tempfile.mkdtemp(prefix="rl-fetch-"))
    try:
        results: list[Imported] = []
        for remote in chosen:
            if remote.kind in ("code", "doc"):
                results.append(_fetch_text(conn, remote, plan_id, client, texts))
            else:
                results.extend(_fetch_data(conn, remote, src, plan, plan_id, client, work, max_bytes))
        return results
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fetch(conn: sqlite3.Connection, src: str, picks: list[str], *, plan_id: str | None = None,
          http: httpx.Client | None = None, max_bytes: int = MAX_MB * 1024 * 1024) -> list[Imported]:
    """List the source again, download the picked files and import them; attach each dataset to the plan. A picked
    script or document is saved as text under the plan's workspace instead (it needs a plan)."""
    from . import plans
    if plan_id and plans.get(conn, plan_id) is None:
        raise FetchError(f"没有这份复现方案：{plan_id}")
    client = _client(http)
    try:
        listing = list_source(src, client, cap=None)
        if plan_id:
            record_listing(plan_id, src, listing)
        chosen = pick(listing, picks)
        return _download_all(conn, src, chosen, plan_id, client, max_bytes)
    finally:
        if http is None:
            client.close()


# ---------------------------------------------------------------- the whole package

@dataclass
class AutoPlan:
    """What a package's code says about what to fetch: the default pick, and why."""
    listing: Listing
    pkg: package.Package | None = None
    data: list[Remote] = field(default_factory=list)       # data the main scripts read, pickable, small enough
    code: list[Remote] = field(default_factory=list)       # the main scripts
    docs: list[Remote] = field(default_factory=list)       # README, codebook
    texts: dict[str, str] = field(default_factory=dict)    # script and document text read on the way, by name
    note: str = ""                                         # why the code could not be read (shown, not fatal)

    @property
    def picks(self) -> list[str]:
        return [r.name for r in [*self.data, *self.code, *self.docs]]

    def readers(self, name: str) -> list[str]:
        return self.pkg.readers_of(name) if self.pkg else []

    def models_text(self, script: str) -> str:
        return package.format_models(self.pkg.models.get(script, {})) if self.pkg else ""

    def big(self, remote: Remote) -> bool:
        return remote.kind == "data" and remote.fetchable and bool(remote.size) and remote.size > AUTO_MAX_MB * MB

    def label(self, remote: Remote) -> str:
        """The few words next to a listed file on the plan page: why it is, or is not, in the default pick."""
        names = lambda paths: "、".join(PurePosixPath(x).name for x in paths)  # noqa: E731
        if remote.kind == "data":
            if (main := self.readers(remote.name)):
                return f"主分析读它：{names(main)}"
            if self.big(remote):
                return f"大于 {AUTO_MAX_MB} MB，没有默认勾选"
            return "只有附录或稳健性脚本读它" if self.pkg and self.pkg.readers_of(remote.name, all_scripts=True) else ""
        if remote.kind == "code" and self.pkg:
            if remote.name in self.pkg.main:
                return "主分析脚本"
            if remote.name in self.pkg.helpers:
                return "被别的脚本调用"
            return self.pkg.not_main.get(remote.name, "")
        return ""


def _text_of(remote: Remote, http: httpx.Client, budget: _Budget) -> tuple[str, str | None]:
    if budget.left <= 0:                                  # the allowance is spent: do not even ask for the file
        return remote.name, None
    try:
        raw = _read_bytes(remote, http, cap_of(remote.kind, remote.name, 0) or CODE_MAX, budget)
        return remote.name, _as_text(raw, remote.name)
    except FetchError:
        return remote.name, None


def plan_auto(listing: Listing, http: httpx.Client | None = None, *, main: list[str] | None = None) -> AutoPlan:
    """Read the listing's scripts and documents (text only, nothing is run), find the main analysis and the data it
    reads, and return the default pick. `main` names the scripts by hand and replaces the rule. Without any script
    to read there is no network and the pick is only the documents."""
    auto = AutoPlan(listing, docs=[d for d in listing.docs if d.fetchable and not package.is_side(d.name)])
    scan = [c for c in listing.code if c.fetchable and package.lang_of(c.name)][:SCAN_FILES]
    if not scan:
        return auto
    client = _client(http)
    code_budget, doc_budget = _Budget(SCAN_BYTES), _Budget(DOC_BYTES)
    try:        # the allowance is enforced while reading: once spent, the files still queued are skipped
        with ThreadPoolExecutor(max_workers=6) as pool:
            fetched = list(pool.map(lambda r: _text_of(r, client, doc_budget if r.kind == "doc" else code_budget),
                                    [*scan, *auto.docs]))
    finally:
        if http is None:
            client.close()
    auto.texts.update({name: text for name, text in fetched if text is not None})
    code = {r.name: auto.texts[r.name] for r in scan if r.name in auto.texts}
    docs = {r.name: auto.texts[r.name] for r in auto.docs if r.name in auto.texts}
    by_name = {r.name: r for r in listing.everything}
    unusable = {r.name: r.note or ("需要 R" if r.name.lower().endswith((".rdata", ".rda")) else "不是能导入的数据格式")
                for r in listing.everything if r.kind != "data" or not r.fetchable}
    unusable.update({r.name: f"大于 {AUTO_MAX_MB} MB，不默认勾选" for r in listing.files
                     if r.fetchable and r.size and r.size > AUTO_MAX_MB * MB})
    try:
        auto.pkg = package.analyze(code, by_name, docs=docs, unusable=unusable, main=main)
    except ValueError as e:
        raise FetchError(str(e)) from e
    auto.code = [by_name[m] for m in auto.pkg.main if m in by_name and by_name[m].fetchable]
    auto.data = [by_name[d] for d in auto.pkg.default_data if d in by_name and by_name[d].kind == "data"]
    return auto


def fetch_auto(conn: sqlite3.Connection, src: str, plan_id: str, *, http: httpx.Client | None = None,
               main: list[str] | None = None, max_bytes: int = MAX_MB * MB) -> tuple[AutoPlan, list[Imported]]:
    """Download the default pick of a package: the data the main scripts read (imported and attached to the plan),
    the main scripts and the README / codebook (saved as text under the plan's workspace)."""
    from . import plans
    if plans.get(conn, plan_id) is None:
        raise FetchError(f"没有这份复现方案：{plan_id}")
    client = _client(http)
    try:
        listing = list_source(src, client, cap=max_bytes)
        record_listing(plan_id, src, listing)
        auto = plan_auto(listing, client, main=main)
        chosen = [*auto.data, *auto.code, *auto.docs]
        return auto, _download_all(conn, src, chosen, plan_id, client, max_bytes, auto.texts)
    finally:
        if http is None:
            client.close()


# ---------------------------------------------------------------- what the source offered, and files Leo uploads (card 0188)
# 待补数据 (missing.py) needs to say why a file a script reads is not here: too big, restricted, never downloaded. Only
# this module sees the listing, so every fetch leaves a copy beside the plan's workspace (not inside it: the AI's
# sandbox must not be able to edit it). The caps stay here too; missing.py reads the sizes against them.

LISTING_FILE = "listing.json"
NOT_DATA_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".html", ".css", ".js", ".tex", ".bib", ".docx",
                 ".pptx", ".md", ".log", ".bak")
_listing_lock = threading.Lock()


def _listing_path(plan_id: str) -> Path:
    from . import workspace
    return workspace.plan_root(plan_id) / LISTING_FILE


def record_listing(plan_id: str, src: str, listing: Listing) -> None:
    """Keep what `src` offered (a name, its size, why it cannot be fetched, whether the source marks it restricted) in
    <plan>/listing.json, replacing what an earlier listing of the same source said. Data files and the files read.csv
    could still be pointed at (.RData, .txt …) are kept; code, documents and pictures are not. Never fails a fetch."""
    try:
        path = _listing_path(plan_id)
        with _listing_lock:
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            files = {n: e for n, e in (old.get("files") or {}).items() if isinstance(e, dict) and e.get("src") != src}
            for r in [*listing.files, *listing.other]:
                if r.kind != "data" and r.ext in NOT_DATA_EXTS:
                    continue
                files[r.name] = {"size": r.size, "note": r.note, "restricted": r.restricted, "src": src}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"files": files}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - a note about the listing must never stop the download
        log.warning("could not record the listing of %s for plan %s", src, plan_id, exc_info=True)


def read_listing(plan_id: str) -> dict[str, dict[str, Any]]:
    """name → {size, note, restricted, src} of every source listed for the plan; {} when none was."""
    try:
        raw = json.loads(_listing_path(plan_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):                # an unreadable record is no record
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    return {str(n): e for n, e in files.items() if isinstance(e, dict)} if isinstance(files, dict) else {}


def upload_problem(name: str) -> str | None:
    """Why a file of this name cannot be uploaded as data, in Chinese; None when it can. The formats are the ones a
    fetch imports: .rds needs R and .xls a reader the lab does not ship, so they are refused with the same reason."""
    ext = PurePosixPath(name.lower()).suffix
    if ext in UNSUPPORTED:
        return f"{ext} 文件暂时不能导入：{UNSUPPORTED[ext]}"
    if not is_data(name):
        return "不支持的文件类型：只收 CSV、TSV、Excel（.xlsx）、Stata、SPSS、Parquet、JSON，或装着这些的 ZIP"
    return None


def import_upload(conn: sqlite3.Connection, plan_id: str, path: Path, filename: str, *, want: str = "",
                  max_bytes: int = MAX_MB * MB) -> list[Imported]:
    """Import a data file Leo uploaded for a file the plan's scripts read: the same fences as a fetch (size cap, allowed
    formats, a zip unpacked member by member with no outside paths or links, a safe flattened name) and the same
    result, a dataset attached to the plan. `path` is the received file, in a folder this call may add to and clear;
    it is moved, so the caller must not use it afterwards. `want` is the path the script reads: the dataset is named
    after it when the extension agrees, so a read of that name finds it."""
    from . import plans
    plan = plans.get(conn, plan_id)
    if plan is None:
        raise FetchError(f"没有这份复现方案：{plan_id}")
    shown = PurePosixPath(filename.replace("\\", "/")).name or "upload"
    problem = upload_problem(shown)
    if problem:
        raise FetchError(problem)
    size = path.stat().st_size
    if size > max_bytes:
        raise FetchError(f"文件 {size // MB} MB，超过上限 {max_bytes // MB} MB")
    wanted = PurePosixPath(want.replace("\\", "/")).name
    named = wanted if wanted and PurePosixPath(wanted.lower()).suffix == PurePosixPath(shown.lower()).suffix else shown
    work = Path(tempfile.mkdtemp(prefix="rl-upload-", dir=path.parent))
    try:
        target = _inside(work, named)
        os.replace(path, target)
        return _import_path(conn, target, shown, f"人工补交（{want or shown}）", plan, plan_id, work, max_bytes,
                            original_name=target.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
