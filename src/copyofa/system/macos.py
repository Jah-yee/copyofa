"""macOS device layer: drutil/ioreg identification, mount checks, mount refusal, locks.

The tool never invokes diskutil, hdiutil, drutil eject/tray/burn/erase, and never mounts anything.
"""

import fcntl
import os
import plistlib
import re
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from ..errors import EnvironmentFailure
from .procs import run_capture

DRUTIL_VERBS = {"status", "list"}
REWRITABLE_TYPES = {"CD-RW", "DVD-RW", "DVD+RW", "DVD-RAM", "BD-RE"}


def drutil_argv(product: str, verb: str, xml: bool = True) -> list[str]:
    if verb not in DRUTIL_VERBS:
        raise ValueError(f"drutil verb {verb!r} is not allowed")
    argv = ["drutil", "-drive", product, verb]
    if xml:
        argv.append("-xml")
    return argv


def drutil(product: str, verb: str, xml: bool = True) -> tuple[int, bytes]:
    return run_capture(drutil_argv(product, verb, xml))


# --- drutil status --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackInfo:
    number: int
    session: int
    mode: int
    start: int
    size: int


@dataclass(frozen=True)
class Medium:
    family: str  # CD | DVD | BD | unknown
    media_type: str | None
    recordable: bool
    rewritable: bool
    blank: bool
    sessions: int
    disc_status: int | None
    last_session_state: int | None
    data_tracks: bool
    tracks: tuple[TrackInfo, ...]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["tracks"] = [asdict(t) for t in self.tracks]
        return data


@dataclass(frozen=True)
class MediumStatus:
    present: bool
    in_transition: bool
    media_class: str | None
    media_type: str | None
    blank: bool
    appendable: bool
    erasable: bool
    disc_status: int | None
    last_session_state: int | None
    sessions: int
    tracks: tuple[TrackInfo, ...]

    def descriptor(self) -> Medium:
        media_type = self.media_type or ""
        if media_type.startswith("BD"):
            family = "BD"
        elif self.media_class in ("CD", "DVD"):
            family = self.media_class
        else:
            family = "unknown"
        return Medium(
            family=family,
            media_type=self.media_type,
            recordable=bool(media_type) and not media_type.endswith("-ROM"),
            rewritable=self.erasable or media_type in REWRITABLE_TYPES,
            blank=self.blank or self.disc_status == 0,
            sessions=self.sessions,
            disc_status=self.disc_status,
            last_session_state=self.last_session_state,
            data_tracks=any(t.mode & 4 for t in self.tracks),
            tracks=self.tracks,
        )


def _int_attr(node: ET.Element | None, attr: str = "value") -> int | None:
    if node is None:
        return None
    value = node.get(attr)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_drutil_status(xml_bytes: bytes, product: str | None) -> MediumStatus | None:
    """Parse `drutil -drive <product> status -xml` for the device whose product name equals `product`
    (the first device when product is None)."""
    root = ET.fromstring(xml_bytes)
    for entry in root.iter("statusfordevice"):
        product_node = entry.find("device/product")
        if product is not None and (product_node is None or product_node.get("name") != product):
            continue
        status = entry.find("deviceStatus")
        if status is None:
            return None
        info = status.find("mediaInfo")
        tracks = []
        if info is not None:
            for t in info.iterfind("trackInfoList/trackinfo"):
                tracks.append(
                    TrackInfo(
                        number=_int_attr(t.find("trackNumber")) or 0,
                        session=_int_attr(t.find("sessionNumber")) or 0,
                        mode=_int_attr(t.find("trackMode")) or 0,
                        start=_int_attr(t.find("startAddress"), "blockAddress") or 0,
                        size=_int_attr(t.find("size"), "blockCount") or 0,
                    )
                )
        block = info.find("discInfo/discInfoBlock") if info is not None else None

        def has(path: str) -> bool:
            return info is not None and info.find(path) is not None

        return MediumStatus(
            present=status.find("mediaIsPresent") is not None,
            in_transition=status.find("mediaIsInTransition") is not None,
            media_class=info.find("mediaClass").get("value") if has("mediaClass") else None,
            media_type=info.find("mediaType").get("value") if has("mediaType") else None,
            blank=has("blank"),
            appendable=has("appendable"),
            erasable=has("erasable"),
            disc_status=_int_attr(block.find("discStatus")) if block is not None else None,
            last_session_state=_int_attr(block.find("LastSessionState")) if block is not None else None,
            sessions=(_int_attr(block.find("sessionCount")) or 0) if block is not None else 0,
            tracks=tuple(tracks),
        )
    return None


# --- ioreg ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bridge:
    vendor_id: str
    product_id: str
    name: str | None
    serial: str | None

    @property
    def key(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"


@dataclass(frozen=True)
class DriveLocation:
    vendor: str
    product: str
    firmware: str
    features: dict
    bsd_name: str | None
    bridge: Bridge | None
    interconnect: str


def _children(node: dict) -> list[dict]:
    return node.get("IORegistryEntryChildren", [])


def _descendants(node: dict):
    for child in _children(node):
        yield child
        yield from _descendants(child)


def _ioreg(cls: str) -> list[dict]:
    code, out = run_capture(["ioreg", "-a", "-l", "-r", "-c", cls])
    if code != 0 or not out.strip():
        return []
    try:
        data = plistlib.loads(out)
    except Exception:
        return []
    return data if isinstance(data, list) else [data]


def _is_drive(node: dict, vendor: str, product: str) -> bool:
    return (
        str(node.get("Vendor Identification", "")).rstrip() == vendor.rstrip()
        and str(node.get("Product Identification", "")).rstrip() == product.rstrip()
    )


def find_drive(vendor: str, product: str) -> DriveLocation | None:
    nub = next((n for n in _ioreg("IOSCSILogicalUnitNub") if _is_drive(n, vendor, product)), None)
    if nub is None:
        return None
    features: dict = {}
    bsd_name = None
    for node in _descendants(nub):
        if "Device Characteristics" in node:
            chars = node["Device Characteristics"]
            features = {k: chars.get(k) for k in ("CD Features", "DVD Features", "BD Features") if k in chars}
        name = node.get("BSD Name")
        if bsd_name is None and name and re.fullmatch(r"disk\d+", name):
            bsd_name = name
    bridge = None
    for device in _ioreg("IOUSBHostDevice"):
        if any(_is_drive(n, vendor, product) for n in _descendants(device)):
            bridge = Bridge(
                vendor_id=f"0x{int(device.get('idVendor', 0)):04X}",
                product_id=f"0x{int(device.get('idProduct', 0)):04X}",
                name=device.get("USB Product Name") or device.get("IORegistryEntryName"),
                serial=device.get("kUSBSerialNumberString"),
            )
            break
    return DriveLocation(
        vendor=str(nub.get("Vendor Identification", "")).rstrip(),
        product=str(nub.get("Product Identification", "")).rstrip(),
        firmware=str(nub.get("Product Revision Level", "")).strip(),
        features=features,
        bsd_name=bsd_name,
        bridge=bridge,
        interconnect="USB" if bridge else "unknown",
    )


def ioreg_record_argv(location: DriveLocation) -> list[str]:
    if location.bridge and location.bridge.name:
        return ["ioreg", "-w0", "-l", "-r", "-n", location.bridge.name]
    return ["ioreg", "-w0", "-l", "-r", "-c", "IOSCSILogicalUnitNub"]


# --- mounts ---------------------------------------------------------------------------------------

_MOUNT_RE = re.compile(r"^(/dev/\S+) on (.+) \(([^)]*)\)$")


@dataclass(frozen=True)
class Mount:
    device: str
    mountpoint: str
    fstype: str
    read_only: bool


def parse_mounts(text: str, bsd: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        match = _MOUNT_RE.match(line)
        if not match or not re.fullmatch(rf"/dev/{re.escape(bsd)}(s\d+)?", match.group(1)):
            continue
        options = [o.strip() for o in match.group(3).split(",")]
        mounts.append(Mount(match.group(1), match.group(2), options[0], "read-only" in options))
    return mounts


def mounts_for(bsd: str) -> list[Mount]:
    code, out = run_capture(["mount"])
    if code != 0:
        raise EnvironmentFailure("mount failed: cannot check how the medium is mounted")
    return parse_mounts(out.decode("utf-8", "replace"), bsd)


# --- mount refusal -----------------------------------------------------------------------


def _collapse(value) -> str:
    return " ".join(str(value or "").split())


def drive_predicate(drives) -> Callable[[dict], bool]:
    targets = {(_collapse(d.vendor), _collapse(d.product)) for d in drives}

    def predicate(desc: dict) -> bool:
        vendor = _collapse(desc.get("DADeviceVendor"))
        model = _collapse(desc.get("DADeviceModel"))
        return (vendor, model) in targets

    return predicate


def bsd_predicate(names) -> Callable[[dict], bool]:
    names = set(names)

    def predicate(desc: dict) -> bool:
        bsd = str(desc.get("DAMediaBSDName") or "")
        base = re.sub(r"s\d+$", "", bsd)
        return bsd in names or base in names

    return predicate


class MountGuard:
    """Refuse every mount of matching disks while active (DiskArbitration mount-approval callback)."""

    def __init__(self, predicate: Callable[[dict], bool]) -> None:
        self.predicate = predicate
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._runloop = None
        self._session = None
        self._callback = None
        self.refused: list[str] = []

    def _approve(self, disk, context):
        import CoreFoundation as CF
        import DiskArbitration as DA

        desc = None
        try:
            raw = DA.DADiskCopyDescription(disk)
            desc = {str(k): raw[k] for k in raw} if raw is not None else None
        except Exception:
            desc = None
        try:
            refuse = desc is None or self.predicate(desc)
        except Exception:
            refuse = True
        if not refuse:
            return None
        bsd = (desc or {}).get("DAMediaBSDName") or "an unidentified disk"
        self.refused.append(str(bsd))
        print(f"coa: refused mount of {bsd}", file=sys.stderr, flush=True)
        dissenter = DA.DADissenterCreate(None, DA.kDAReturnNotPermitted, "coa controls this drive")
        # DiskArbitration releases the returned dissenter; PyObjC also releases its own reference when the
        # Python wrapper dies, so hand DA an extra retain (without it the process crashes, SIGTRAP).
        CF.CFRetain(dissenter)
        return dissenter

    def _run(self) -> None:
        import CoreFoundation as CF
        import DiskArbitration as DA
        import objc

        self._session = DA.DASessionCreate(None)

        @objc.callbackFor(DA.DARegisterDiskMountApprovalCallback)
        def callback(disk, context):
            return self._approve(disk, context)

        self._callback = callback
        DA.DARegisterDiskMountApprovalCallback(self._session, None, self._callback, None)
        self._runloop = CF.CFRunLoopGetCurrent()
        DA.DASessionScheduleWithRunLoop(self._session, self._runloop, CF.kCFRunLoopDefaultMode)
        self._ready.set()
        CF.CFRunLoopRun()
        DA.DASessionUnscheduleFromRunLoop(self._session, self._runloop, CF.kCFRunLoopDefaultMode)

    def __enter__(self) -> "MountGuard":
        self._thread = threading.Thread(target=self._run, name="coa-mount-guard", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise EnvironmentFailure("could not register the DiskArbitration mount-approval callback")
        return self

    def __exit__(self, *exc) -> None:
        import CoreFoundation as CF
        import DiskArbitration as DA
        import objc

        try:
            DA.DAUnregisterApprovalCallback(self._session, objc.callbackPointer(self._callback), None)
        except Exception:
            pass
        if self._runloop is not None:
            CF.CFRunLoopStop(self._runloop)
        if self._thread is not None:
            self._thread.join(10)


# --- locks ----------------------------------------------------------------------------------------


class FileLock:
    """Exclusive non-blocking flock(2) on a file; the lock, not the file, grants ownership."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise EnvironmentFailure(f"{self.path} is held by another coa process", code="lock-busy") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
