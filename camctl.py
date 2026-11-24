#!/usr/bin/env python3
"""
Autodarts Camera Config – a small web UI for tuning the USB cameras of a
headless Autodarts board (brightness, contrast, saturation, exposure, ...).

Wraps `v4l2-ctl`, uses only the Python 3 standard library.
  Run:           python3 camctl.py            -> http://<pi-ip>:8088
  Apply saved:   python3 camctl.py --apply    (apply saved settings and exit)

Environment variables:
  CAMCTL_PORT         web port (default: 8088)
  CAMCTL_CONF         settings file (default: settings.json next to this script)
  CAMCTL_APPLY_DELAY  seconds to wait after start before applying saved settings (default: 45)
  CAMCTL_ALL=1        list every capture device, not only USB (uvcvideo) cameras
  CAMCTL_AUTODARTS_URL     Autodarts board API (default: http://localhost:3180)
  CAMCTL_AUTODARTS_CONFIG  Autodarts config file (default: ~/.config/autodarts/config.toml)
  CAMCTL_AUTODARTS_SYNC=0  do not sync with Autodarts

Autodarts integration: Autodarts keeps its own camera controls ([cam_controls] in
config.toml) and applies them when it starts. When a camera is used by Autodarts,
"Save" also sends the values to Autodarts (which stores them in its config), and the
values stored by Autodarts are treated as the saved state for that camera.
"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("CAMCTL_PORT", "8088"))
CONF = os.environ.get(
    "CAMCTL_CONF",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json"),
)
APPLY_DELAY = int(os.environ.get("CAMCTL_APPLY_DELAY", "45"))
SHOW_ALL = os.environ.get("CAMCTL_ALL") == "1"
AD_URL = os.environ.get("CAMCTL_AUTODARTS_URL", "http://localhost:3180").rstrip("/")
AD_CONF = os.path.expanduser(os.environ.get("CAMCTL_AUTODARTS_CONFIG", "~/.config/autodarts/config.toml"))
AD_SYNC = os.environ.get("CAMCTL_AUTODARTS_SYNC", "1") != "0"

LOCK = threading.Lock()
CTRL_RE = re.compile(r"^\s*(\w+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.*)$")
MENU_RE = re.compile(r"^\s+(\d+):\s+(.*)$")
NAME_RE = re.compile(r"^\w+$")
USABLE_TYPES = {"int", "bool", "menu", "intmenu", "int64"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "v4l2-ctl not found – install it: sudo apt install v4l-utils"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


# ---------------------------------------------------------------- devices

def _device_info(dev):
    rc, out, _ = run(["v4l2-ctl", "-d", dev, "-D"])
    if rc:
        return None
    driver = re.search(r"Driver name\s*:\s*(.+)", out)
    card = re.search(r"Card type\s*:\s*(.+)", out)
    bus = re.search(r"Bus info\s*:\s*(.+)", out)
    caps = ""
    if "Device Caps" in out:
        tail = out.split("Device Caps", 1)[1].splitlines()[1:]
        for line in tail:
            if not line.startswith("\t\t"):
                break
            caps += line.strip() + "\n"
    else:
        caps = out
    return {
        "driver": driver.group(1).strip() if driver else "",
        "card": card.group(1).strip() if card else dev,
        "bus": bus.group(1).strip() if bus else "",
        "capture": "Video Capture" in caps and "Memory-to-Memory" not in caps,
    }


_cache = {"t": 0, "cams": []}


def discover(force=False):
    """List cameras with a stable key (the USB-port based /dev/v4l/by-path name)."""
    if not force and time.time() - _cache["t"] < 5:
        return _cache["cams"]
    by_path = {}
    for p in glob.glob("/dev/v4l/by-path/*"):
        by_path.setdefault(os.path.realpath(p), os.path.basename(p))
    cams = []
    devs = sorted(glob.glob("/dev/video*"), key=lambda d: int(re.sub(r"\D", "", d) or 0))
    for dev in devs:
        info = _device_info(dev)
        if not info or not info["capture"]:
            continue
        if not SHOW_ALL and info["driver"] != "uvcvideo":
            continue
        key = by_path.get(os.path.realpath(dev), dev)
        port = info["bus"].rsplit("-", 1)[-1] if info["bus"].startswith("usb-") else ""
        label = info["card"].split(":")[0].strip()
        cams.append({
            "key": key,
            "dev": dev,
            "label": label,
            "bus": info["bus"],
            "port": port,
        })
    _cache.update(t=time.time(), cams=cams)
    return cams


def dev_for(key):
    for c in discover():
        if c["key"] == key:
            return c["dev"]
    for c in discover(force=True):
        if c["key"] == key:
            return c["dev"]
    return None


# ---------------------------------------------------------------- controls

def list_ctrls(dev):
    rc, out, err = run(["v4l2-ctl", "-d", dev, "--list-ctrls-menus"])
    ctrls, cur = [], None
    for line in out.splitlines():
        m = CTRL_RE.match(line)
        if m:
            name, typ, rest = m.groups()
            cur = None
            if typ not in USABLE_TYPES:
                continue
            c = {"name": name, "type": typ}
            for k, v in re.findall(r"(\w+)=(-?\d+)\b", rest):
                if k in ("min", "max", "step", "default", "value"):
                    c[k] = int(v)
            c["inactive"] = "inactive" in rest
            c["readonly"] = "read-only" in rest
            if typ == "bool":
                c.setdefault("min", 0)
                c.setdefault("max", 1)
            if typ in ("menu", "intmenu"):
                c["menu"] = []
            ctrls.append(c)
            cur = c
            continue
        m = MENU_RE.match(line)
        if m and cur is not None and "menu" in cur:
            cur["menu"].append([int(m.group(1)), m.group(2).strip()])
    return ctrls


def set_ctrl(dev, name, value):
    rc, _, err = run(["v4l2-ctl", "-d", dev, "-c", f"{name}={int(value)}"])
    return rc == 0, err.strip()


def apply_values(dev, values):
    """Set auto switches (bool/menu) first, then numeric values – otherwise e.g.
    exposure time is rejected while auto exposure is still on."""
    ctrls = {c["name"]: c for c in list_ctrls(dev)}
    order = sorted(
        (n for n in values if n in ctrls and not ctrls[n]["readonly"]),
        key=lambda n: 0 if ctrls[n]["type"] in ("bool", "menu", "intmenu") else 1,
    )
    errors = []
    autos = [n for n in order if ctrls[n]["type"] in ("bool", "menu", "intmenu")]
    for n in autos:
        ok, err = set_ctrl(dev, n, values[n])
        if not ok:
            errors.append(f"{n}: {err}")
    # re-read: controls driven by an auto mode are now inactive and cannot be set
    inactive = {c["name"] for c in list_ctrls(dev) if c["inactive"]} if autos else set()
    for n in order:
        if n in autos or n in inactive:
            continue
        ok, err = set_ctrl(dev, n, values[n])
        if not ok:
            errors.append(f"{n}: {err}")
    return errors


# ---------------------------------------------------------------- Autodarts integration

# v4l2 control name -> Autodarts control name (others: snake_case -> camelCase)
AD_NAMES = {
    "white_balance_automatic": "autoWhiteBalance",
    "white_balance_temperature_auto": "autoWhiteBalance",
    "white_balance_temperature": "whiteBalanceTemperature",
    "auto_exposure": "exposureAuto",
    "exposure_auto": "exposureAuto",
    "exposure_time_absolute": "exposureAbsolute",
    "exposure_absolute": "exposureAbsolute",
    "exposure_dynamic_framerate": "exposureAutoPriority",
    "exposure_auto_priority": "exposureAutoPriority",
}


def ad_name(name):
    return AD_NAMES.get(name) or re.sub(r"_(\w)", lambda m: m.group(1).upper(), name)


def ad_request(method, path, body=None, timeout=4):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(AD_URL + "/api" + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def ad_read_config():
    """Autodarts config.toml as a dict, or None (missing file / Python < 3.11)."""
    try:
        import tomllib
        with open(AD_CONF, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def ad_cam_index():
    """{realpath of camera device: Autodarts camera index}. Empty if Autodarts is not found."""
    if not AD_SYNC:
        return {}
    cams = None
    try:
        cams = ad_request("GET", "/config")["cam"]["cams"]
    except Exception:
        conf = ad_read_config()
        if conf:
            cams = conf.get("cam", {}).get("cams")
    return {os.path.realpath(c): i for i, c in enumerate(cams or [])}


def ad_saved_values(idx, ctrls):
    """Values Autodarts has stored for camera idx, as {v4l2 name: value}.
    Controls Autodarts has not stored are at their default. None if unreadable."""
    conf = ad_read_config()
    if conf is None:
        return None
    stored = conf.get("cam_controls", {}).get(str(idx), {})
    vals = {}
    for c in ctrls:
        if c["readonly"]:
            continue
        k = ad_name(c["name"])
        if k in stored:
            vals[c["name"]] = int(stored[k])
        elif "default" in c:
            vals[c["name"]] = c["default"]
    return vals


def ad_push(idx, values, ctrls):
    """Send values to Autodarts; it applies them and stores them in config.toml.
    Returns an error string or None."""
    try:
        known = ad_request("GET", f"/cams/controls/{idx}") or {}
    except Exception as e:
        return f"autodarts_unreachable ({e})"
    types = {c["name"]: c["type"] for c in ctrls}
    autos, rest = {}, {}
    for n, v in values.items():
        k = ad_name(n)
        if k in known:
            (autos if types.get(n) in ("bool", "menu", "intmenu") else rest)[k] = int(v)
    try:
        for part in (autos, rest):  # auto switches first, like apply_values()
            if part:
                ad_request("PATCH", f"/cams/controls/{idx}", part)
    except Exception as e:
        return f"autodarts_unreachable ({e})"
    return None


# ---------------------------------------------------------------- persistence

def load_conf():
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(data):
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONF)


def current_values(dev):
    return {c["name"]: c["value"] for c in list_ctrls(dev)
            if not c["readonly"] and "value" in c}


def apply_saved(include_autodarts=False):
    """Apply saved settings. Cameras used by Autodarts are skipped unless
    include_autodarts=True (Autodarts applies its own stored values when it starts);
    for those, the values stored in the Autodarts config are used."""
    conf = load_conf()
    idx = ad_cam_index()
    report = {}
    for cam in discover(force=True):
        i = idx.get(os.path.realpath(cam["dev"]))
        vals = conf.get(cam["key"])
        if i is not None:
            if not include_autodarts:
                continue
            vals = ad_saved_values(i, list_ctrls(cam["dev"])) or vals
        if vals:
            errs = apply_values(cam["dev"], vals)
            report[cam["key"]] = errs
            log(f"applied: {cam['dev']} ({cam['key']})", "errors: " + "; ".join(errs) if errs else "OK")
    return report


def watcher():
    """Apply saved settings after start-up (delayed) and whenever a camera (re)appears."""
    time.sleep(APPLY_DELAY)
    with LOCK:
        apply_saved()
    known = {c["key"] + c["dev"] for c in discover(force=True)}
    while True:
        time.sleep(30)
        now = {c["key"] + c["dev"] for c in discover(force=True)}
        if now - known:
            log("new/reconnected camera – applying saved settings")
            time.sleep(5)
            with LOCK:
                apply_saved()
        known = now


# ---------------------------------------------------------------- web

def cams_payload():
    conf = load_conf()
    idx = ad_cam_index()
    out = []
    for cam in discover():
        i = idx.get(os.path.realpath(cam["dev"]))
        out.append({**cam, "ctrls": list_ctrls(cam["dev"]), "saved": cam["key"] in conf, "ad": i})
    # same order and numbering as in Autodarts; other cameras after them
    out.sort(key=lambda c: (c["ad"] is None, c["ad"] if c["ad"] is not None else 0))
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE.encode(), "text/html")
        if path.startswith("/fonts/") and re.fullmatch(r"/fonts/[\w.-]+\.woff2", path):
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), path.lstrip("/"))
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Cache-Control", "max-age=31536000")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
        if path == "/api/cams":
            with LOCK:
                return self._send(200, cams_payload())
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad_request"})
        with LOCK:
            try:
                return self._send(200, self.route(path, req))
            except LookupError as e:
                return self._send(404, {"error": str(e)})

    def route(self, path, req):
        if path == "/api/set":
            dev = dev_for(req.get("key"))
            name = str(req.get("name", ""))
            if not dev:
                raise LookupError("camera_unavailable")
            if not NAME_RE.match(name):
                raise LookupError("invalid_control")
            ok, err = set_ctrl(dev, name, req.get("value", 0))
            return {"ok": ok, "error": err, "ctrls": list_ctrls(dev)}

        if path == "/api/defaults":
            dev = dev_for(req.get("key"))
            if not dev:
                raise LookupError("camera_unavailable")
            vals = {c["name"]: c["default"] for c in list_ctrls(dev)
                    if "default" in c and not c["readonly"]}
            errs = apply_values(dev, vals)
            return {"ok": not errs, "error": "; ".join(errs), "ctrls": list_ctrls(dev)}

        if path == "/api/copy":
            src = dev_for(req.get("key"))
            if not src:
                raise LookupError("camera_unavailable")
            vals = current_values(src)
            errs = []
            for cam in discover():
                if cam["dev"] != src:
                    errs += [f"{cam['dev']} {e}" for e in apply_values(cam["dev"], vals)]
            return {"ok": not errs, "error": "; ".join(errs)}

        if path == "/api/save":
            conf = load_conf()
            idx = ad_cam_index()
            errs, synced = [], 0
            for cam in discover(force=True):
                ctrls = list_ctrls(cam["dev"])
                vals = {c["name"]: c["value"] for c in ctrls if not c["readonly"] and "value" in c}
                conf[cam["key"]] = vals
                i = idx.get(os.path.realpath(cam["dev"]))
                if i is not None:
                    e = ad_push(i, vals, ctrls)
                    if e:
                        errs.append(e)
                    else:
                        synced += 1
            save_conf(conf)
            return {"ok": not errs, "error": "; ".join(dict.fromkeys(errs)), "autodarts": synced}

        if path == "/api/apply":
            rep = apply_saved(include_autodarts=True)
            errs = [e for v in rep.values() for e in v]
            return {"ok": not errs, "error": "; ".join(errs), "applied": len(rep)}

        raise LookupError("not_found")


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autodarts Camera Config</title>
<style>@font-face{font-family:"Open Sans";font-style:normal;font-weight:400;font-display:swap;src:url(/fonts/open-sans-latin-400.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:"Open Sans";font-style:normal;font-weight:400;font-display:swap;src:url(/fonts/open-sans-latin-ext-400.woff2) format("woff2");unicode-range:U+0100-02AF,U+0304,U+0308,U+0329,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}
@font-face{font-family:"Open Sans";font-style:normal;font-weight:600;font-display:swap;src:url(/fonts/open-sans-latin-600.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:"Open Sans";font-style:normal;font-weight:600;font-display:swap;src:url(/fonts/open-sans-latin-ext-600.woff2) format("woff2");unicode-range:U+0100-02AF,U+0304,U+0308,U+0329,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}

:root{--blue:#90CDF4;--blueh:#63B3ED;--txt:rgba(255,255,255,.92);--mut:rgba(255,255,255,.64);--line:rgba(255,255,255,.16);--sep:rgba(255,255,255,.08)}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 "Open Sans",sans-serif;color:var(--txt);min-height:100vh;background-color:#1A202C;background-image:radial-gradient(50% 30% at 86% 0%,#313370e3 0%,#40348600 100%),radial-gradient(50% 70% at 70% 22%,#26599ae6 0%,#40348600 100%),radial-gradient(50% 70% at 112% 44%,#2c436cd9 0%,#40348600 100%),radial-gradient(90% 90% at -12% 89%,#0f2f50e0 0%,#40348600 100%),radial-gradient(50% 70% at -2% 53%,#34205fe3 0%,#40348600 100%),radial-gradient(50% 70% at 36% 22%,#403486d4 0%,#40348600 100%),radial-gradient(50% 40% at 66% 59%,#206fb9de 7%,#206fb900 100%),radial-gradient(75% 75% at 50% 50%,#3662b9 1%,#2d285b 100%);background-repeat:no-repeat;background-attachment:fixed}
header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 24px;background:rgba(0,0,0,.24);border-bottom:1px solid var(--sep);backdrop-filter:blur(8px)}
h1{font-size:20px;font-weight:600;margin:0 16px 0 0;line-height:1.2}h1 small{display:block;font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.sp{flex:1}
button,.btn{display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 12px;border:0;border-radius:6px;background:rgba(255,255,255,.08);color:var(--txt);font:600 14px "Open Sans",sans-serif;cursor:pointer;text-decoration:none;transition:background .2s;white-space:nowrap}
button:hover,.btn:hover{background:rgba(255,255,255,.16)}
button.pri{background:var(--blue);color:#1A202C}button.pri:hover{background:var(--blueh)}
.outline{background:transparent;border:1px solid var(--line)}.ghost{background:transparent}
button:disabled{opacity:.4;cursor:not-allowed}
:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(66,153,225,.6)}
main{max-width:1440px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;padding:24px}
.card{background:rgba(26,32,44,.5);border:1px solid var(--sep);border-radius:6px;padding:16px 20px;backdrop-filter:blur(6px);box-shadow:0 1px 3px rgba(0,0,0,.1),0 1px 2px rgba(0,0,0,.06)}
.card h2{display:flex;align-items:center;gap:8px;font-size:18px;font-weight:600;margin:0}
.sub{color:var(--mut);font-size:14px;margin:2px 0 12px}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px}
.row{display:grid;grid-template-columns:1fr 72px 32px;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--sep)}
.row:first-child{border-top:0}
.row label{grid-column:1/-1;display:flex;justify-content:space-between;font-size:14px;margin-bottom:-4px}
.row label b{font-weight:600}.row label span{color:var(--mut);font-size:12px}
.row.dis{opacity:.4}
input[type=number],select{width:100%;height:32px;padding:0 8px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--txt);font:14px "Open Sans",sans-serif;transition:border-color .2s}
input[type=number]:hover,select:hover{border-color:rgba(255,255,255,.24)}
input[type=number]:focus,select:focus{border-color:var(--blueh);box-shadow:0 0 0 1px var(--blueh);outline:none}
select{grid-column:1/3}select option{background:#2D3748}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:4px;margin:0;border-radius:2px;background:linear-gradient(to right,var(--blue) var(--p,0%),rgba(255,255,255,.24) var(--p,0%));cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}
input[type=range]:disabled{cursor:not-allowed}
.sw{grid-column:1/3}
.sw input{-webkit-appearance:none;appearance:none;position:relative;width:34px;height:18px;margin:0;border-radius:9999px;background:rgba(255,255,255,.24);cursor:pointer;transition:background .15s}
.sw input::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#fff;transition:transform .15s}
.sw input:checked{background:var(--blue)}.sw input:checked::after{transform:translateX(16px)}
.rst{width:32px;padding:0;background:transparent;color:var(--mut);font-size:16px}
.badge{display:inline-block;padding:0 4px;border-radius:2px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.02em;background:rgba(226,232,240,.16);color:#E2E8F0}
.badge.a{background:rgba(144,205,244,.16);color:#90CDF4;text-transform:none}.badge.s{background:rgba(154,230,180,.16);color:#9AE6B4}.badge.w{background:rgba(251,211,141,.16);color:#FBD38D}
#toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,8px);padding:12px 16px;border-radius:6px;background:#90CDF4;color:#1A202C;font-size:14px;font-weight:600;box-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -2px rgba(0,0,0,.05);opacity:0;transition:.2s;pointer-events:none;max-width:90vw}
#toast.on{opacity:1;transform:translate(-50%,0)}#toast.ok{background:#9AE6B4}#toast.err{background:#FC8181}
.lang{width:auto;height:32px;padding:0 8px;margin-right:4px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--txt);font:600 14px "Open Sans",sans-serif}.lang option{background:#2D3748}
.empty{grid-column:1/-1;padding:48px;text-align:center;color:var(--mut)}
</style></head><body>
<header>
  <h1><small>Autodarts</small><span data-t="title"></span></h1>
  <span class="sp"></span>
  <select id="lang" class="lang" aria-label="Language" onchange="setLang(this.value)">
    <option value="en">English</option><option value="de">Deutsch</option><option value="hu">Magyar</option>
  </select>
  <button class="ghost" onclick="load()" data-t="refresh"></button>
  <button class="outline" onclick="applySaved()" data-t="reload"></button>
  <a class="btn outline" id="ad" target="_blank" data-t="open_ad"></a>
  <button class="pri" onclick="save()" data-t="save"></button>
</header>
<main id="cams"><div class="empty" data-t="loading"></div></main>
<div id="toast"></div>
<script>
const I18N={
en:{title:"Camera Config",refresh:"Refresh",reload:"Reload saved",open_ad:"Open Autodarts",save:"Save",loading:"Loading…",
 camera:n=>`Camera ${n}`,ad_saved:n=>`Saved – also stored in Autodarts (${n} camera${n==1?"":"s"})`,ad_fail:"Saved here, but could not write to Autodarts",ad_tag:"in Autodarts",saved:"Saved",not_saved:"Not saved",unsaved:"Unsaved changes",usb:"USB port",defaults:"Factory defaults",copy:"Copy to other cameras",
 def:"default",auto:"set by auto mode",reset:"Reset to default",no_cams:"No USB cameras found. Are they connected? (v4l2-ctl --list-devices)",
 no_server:"Cannot reach the server",failed:"Failed",saved_ok:"Saved – these settings survive a reboot",save_fail:"Saving failed",
 applied:n=>`Saved settings loaded (${n} camera${n==1?"":"s"})`,partial:"Partly failed",defaults_ok:"Factory defaults restored",
 copy_q:"Copy this camera's settings to all other cameras?",copied:"Copied to the other cameras",
 camera_unavailable:"Camera not available",invalid_control:"Invalid control",
 L:{brightness:"Brightness",contrast:"Contrast",saturation:"Saturation",hue:"Hue",gamma:"Gamma",gain:"Gain",sharpness:"Sharpness",
 backlight_compensation:"Backlight compensation",white_balance_automatic:"Auto white balance",white_balance_temperature_auto:"Auto white balance",
 white_balance_temperature:"White balance (K)",power_line_frequency:"Power line frequency (anti-flicker)",auto_exposure:"Exposure mode",
 exposure_auto:"Exposure mode",exposure_time_absolute:"Exposure time",exposure_absolute:"Exposure time",exposure_dynamic_framerate:"Dynamic frame rate",
 exposure_auto_priority:"Dynamic frame rate",focus_automatic_continuous:"Continuous autofocus",focus_auto:"Autofocus",focus_absolute:"Focus",
 zoom_absolute:"Zoom",pan_absolute:"Pan",tilt_absolute:"Tilt"}},
de:{title:"Kamera-Konfiguration",refresh:"Aktualisieren",reload:"Gespeicherte laden",open_ad:"Autodarts öffnen",save:"Speichern",loading:"Wird geladen…",
 camera:n=>`Kamera ${n}`,ad_saved:n=>`Gespeichert – auch in Autodarts übernommen (${n} Kamera${n==1?"":"s"})`,ad_fail:"Hier gespeichert, aber Autodarts nicht erreichbar",ad_tag:"in Autodarts",saved:"Gespeichert",not_saved:"Nicht gespeichert",unsaved:"Ungespeicherte Änderungen",usb:"USB-Port",defaults:"Werkseinstellungen",copy:"Auf andere Kameras kopieren",
 def:"Standard",auto:"durch Automatik gesteuert",reset:"Auf Standard zurücksetzen",no_cams:"Keine USB-Kameras gefunden. Sind sie angeschlossen? (v4l2-ctl --list-devices)",
 no_server:"Server nicht erreichbar",failed:"Fehlgeschlagen",saved_ok:"Gespeichert – bleibt auch nach einem Neustart erhalten",save_fail:"Speichern fehlgeschlagen",
 applied:n=>`Gespeicherte Einstellungen geladen (${n} Kamera${n==1?"":"s"})`,partial:"Teilweise fehlgeschlagen",defaults_ok:"Werkseinstellungen wiederhergestellt",
 copy_q:"Einstellungen dieser Kamera auf alle anderen Kameras kopieren?",copied:"Auf die anderen Kameras kopiert",
 camera_unavailable:"Kamera nicht verfügbar",invalid_control:"Ungültiger Regler",
 L:{brightness:"Helligkeit",contrast:"Kontrast",saturation:"Sättigung",hue:"Farbton",gamma:"Gamma",gain:"Verstärkung",sharpness:"Schärfe",
 backlight_compensation:"Gegenlichtkompensation",white_balance_automatic:"Automatischer Weißabgleich",white_balance_temperature_auto:"Automatischer Weißabgleich",
 white_balance_temperature:"Weißabgleich (K)",power_line_frequency:"Netzfrequenz (gegen Flimmern)",auto_exposure:"Belichtungsmodus",
 exposure_auto:"Belichtungsmodus",exposure_time_absolute:"Belichtungszeit",exposure_absolute:"Belichtungszeit",exposure_dynamic_framerate:"Dynamische Bildrate",
 exposure_auto_priority:"Dynamische Bildrate",focus_automatic_continuous:"Kontinuierlicher Autofokus",focus_auto:"Autofokus",focus_absolute:"Fokus",
 zoom_absolute:"Zoom",pan_absolute:"Schwenken",tilt_absolute:"Neigen"}},
hu:{title:"Kamerabeállítások",refresh:"Frissítés",reload:"Mentett visszatöltése",open_ad:"Autodarts megnyitása",save:"Mentés",loading:"Betöltés…",
 camera:n=>`${n}. kamera`,ad_saved:n=>`Elmentve, az Autodarts-ba is beírva (${n} kamera)`,ad_fail:"Itt elmentve, de az Autodarts-ba nem sikerült beírni",ad_tag:"Autodarts-ban",saved:"Mentve",not_saved:"Nincs mentve",unsaved:"Nem mentett változás",usb:"USB port",defaults:"Gyári értékek",copy:"Másolás a többi kamerára",
 def:"alap",auto:"automata módban",reset:"Alapérték",no_cams:"Nem találok USB kamerát. Csatlakoztatva vannak? (v4l2-ctl --list-devices)",
 no_server:"Nem érem el a szervert",failed:"Nem sikerült",saved_ok:"Elmentve, újraindítás után is ezek maradnak",save_fail:"Mentés sikertelen",
 applied:n=>`Mentett értékek betöltve (${n} kamera)`,partial:"Részben sikertelen",defaults_ok:"Gyári értékek visszaállítva",
 copy_q:"Ennek a kamerának a beállításait átmásolod a többire?",copied:"Átmásolva a többi kamerára",
 camera_unavailable:"A kamera nem elérhető",invalid_control:"Érvénytelen vezérlő",
 L:{brightness:"Fényerő",contrast:"Kontraszt",saturation:"Szaturáció",hue:"Színárnyalat",gamma:"Gamma",gain:"Erősítés (gain)",sharpness:"Élesség",
 backlight_compensation:"Ellenfény-kompenzáció",white_balance_automatic:"Automatikus fehéregyensúly",white_balance_temperature_auto:"Automatikus fehéregyensúly",
 white_balance_temperature:"Fehéregyensúly (K)",power_line_frequency:"Hálózati frekvencia (villódzás ellen)",auto_exposure:"Expozíció mód",
 exposure_auto:"Expozíció mód",exposure_time_absolute:"Expozíciós idő",exposure_absolute:"Expozíciós idő",exposure_dynamic_framerate:"Dinamikus képkockaszám",
 exposure_auto_priority:"Dinamikus képkockaszám",focus_automatic_continuous:"Folyamatos autofókusz",focus_auto:"Autofókusz",focus_absolute:"Fókusz",
 zoom_absolute:"Zoom",pan_absolute:"Pásztázás",tilt_absolute:"Döntés"}}};
function pickLang(){let l=null;try{l=localStorage.getItem("camctl_lang")}catch(e){}
  if(!I18N[l])l=(navigator.languages||[navigator.language||"en"]).map(x=>x.slice(0,2).toLowerCase()).find(x=>I18N[x])||"en";return l}
let LANG=pickLang();
function t(k,...a){const v=I18N[LANG][k]??I18N.en[k]??k;return typeof v==="function"?v(...a):v}
function errText(e){return e?(I18N[LANG][e]||e):""}
function setLang(l){LANG=I18N[l]?l:"en";try{localStorage.setItem("camctl_lang",LANG)}catch(e){}applyStatic();render()}
function applyStatic(){document.documentElement.lang=LANG;document.title="Autodarts – "+t("title");$("#lang").value=LANG;
  document.querySelectorAll("[data-t]").forEach(e=>e.textContent=t(e.dataset.t))}
const $=s=>document.querySelector(s);let CAMS=null;const timers={};
document.getElementById("ad").href=location.protocol+"//"+location.hostname+":3180";
function toast(msg,kind){const el=$("#toast");el.textContent=msg;el.className="on "+(kind||"");clearTimeout(el._h);el._h=setTimeout(()=>el.className="",2600)}
async function api(p,b){const r=await fetch(p,b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{});return r.json()}
async function load(){try{CAMS=await api("/api/cams");render()}catch(e){toast(t("no_server"),"err")}}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function render(){
  const m=$("#cams");if(CAMS===null)return;
  if(!CAMS.length){m.innerHTML=`<div class="empty">${esc(t("no_cams"))}</div>`;return}
  m.innerHTML=CAMS.map((c,i)=>`<section class="card" data-key="${esc(c.key)}">
    <h2>${esc(t("camera",c.ad!=null?c.ad+1:i+1))} <span class="badge ${c.saved?"s":""}">${esc(c.saved?t("saved"):t("not_saved"))}</span></h2>
    <div class="sub">${c.ad!=null?`<span class="badge a">${esc(t("ad_tag"))}</span> `:""}${esc(c.label)} · ${esc(c.dev)}${c.port?" · "+esc(t("usb"))+" "+esc(c.port):""}</div>
    <div class="tools"><button class="outline" onclick="defaults(this)">${esc(t("defaults"))}</button>
    <button class="outline" onclick="copyAll(this)">${esc(t("copy"))}</button></div>
    <div class="ctrls">${c.ctrls.filter(x=>!x.readonly).map(ctrlHtml).join("")}</div></section>`).join("");
}
function ctrlHtml(x){
  const lab=I18N[LANG].L[x.name]||I18N.en.L[x.name]||x.name.replace(/_/g," "),dis=x.inactive?"disabled":"",cls="row"+(x.inactive?" dis":"");
  const note=x.inactive?`<span>${esc(t("auto"))}</span>`:`<span>${esc(t("def"))}: ${x.default??"-"}</span>`;
  const head=`<label><b>${esc(lab)}</b>${note}</label>`;
  const rst=`<button class="rst" title="${esc(t("reset"))}" ${dis} onclick="setv(this,'${x.name}',${x.default})">↺</button>`;
  if(x.type==="bool")return `<div class="${cls}" data-n="${x.name}">${head}<div class="sw"><input type="checkbox" aria-label="${esc(lab)}" ${x.value?"checked":""} ${dis} onchange="setv(this,'${x.name}',this.checked?1:0)"></div>${rst}</div>`;
  if(x.menu)return `<div class="${cls}" data-n="${x.name}">${head}<select aria-label="${esc(lab)}" ${dis} onchange="setv(this,'${x.name}',this.value)">${x.menu.map(([v,s])=>`<option value="${v}" ${v==x.value?"selected":""}>${esc(s)}</option>`).join("")}</select>${rst}</div>`;
  return `<div class="${cls}" data-n="${x.name}">${head}<input type="range" aria-label="${esc(lab)}" min="${x.min}" max="${x.max}" step="${x.step||1}" value="${x.value}" style="--p:${pct(x)}%" ${dis} oninput="slide(this,'${x.name}')"><input type="number" aria-label="${esc(lab)}" min="${x.min}" max="${x.max}" step="${x.step||1}" value="${x.value}" ${dis} onchange="setv(this,'${x.name}',this.value)">${rst}</div>`;
}
function pct(x){return x.max>x.min?((x.value-x.min)/(x.max-x.min)*100).toFixed(1):0}
function keyOf(el){return el.closest(".card").dataset.key}
function slide(el,n){const row=el.closest(".row");el.style.setProperty("--p",pct({value:+el.value,min:+el.min,max:+el.max})+"%");row.querySelector("input[type=number]").value=el.value;
  const k=keyOf(el)+n;clearTimeout(timers[k]);timers[k]=setTimeout(()=>setv(el,n,el.value),120)}
async function setv(el,n,v){const card=el.closest(".card"),key=card.dataset.key;
  const r=await api("/api/set",{key,name:n,value:Number(v)});
  if(!r.ok)toast(t("failed")+": "+(errText(r.error)||n),"err");
  if(r.ctrls)refresh(card,r.ctrls);markUnsaved(card)}
function refresh(card,ctrls){
  const cam=CAMS.find(c=>c.key===card.dataset.key);if(cam)cam.ctrls=ctrls;
  ctrls.filter(x=>!x.readonly).forEach(x=>{const row=card.querySelector(`.row[data-n="${x.name}"]`);if(!row)return;
    const active=document.activeElement;if(row.contains(active)&&active.type==="range")return;
    const tmp=document.createElement("div");tmp.innerHTML=ctrlHtml(x);row.replaceWith(tmp.firstElementChild)})}
function markUnsaved(card){const b=card.querySelector(".badge");b.className="badge w";b.textContent=t("unsaved");
  const cam=CAMS.find(c=>c.key===card.dataset.key);if(cam)cam.saved=false}
async function save(){const r=await api("/api/save",{});
  if(r.ok)toast(r.autodarts?t("ad_saved",r.autodarts):t("saved_ok"),"ok");
  else if(r.error&&r.error.startsWith("autodarts"))toast(t("ad_fail"),"err");else toast(t("save_fail"),"err");load()}
async function applySaved(){const r=await api("/api/apply",{});r.ok?toast(t("applied",r.applied),"ok"):toast(t("partial")+": "+errText(r.error),"err");load()}
async function defaults(el){const r=await api("/api/defaults",{key:keyOf(el)});r.ok?toast(t("defaults_ok"),"ok"):toast(errText(r.error),"err");load()}
async function copyAll(el){if(!confirm(t("copy_q")))return;
  const r=await api("/api/copy",{key:keyOf(el)});r.ok?toast(t("copied"),"ok"):toast(t("partial")+": "+errText(r.error),"err");load()}
applyStatic();load();
</script></body></html>"""


def main():
    if "--apply" in sys.argv:
        apply_saved()
        return
    threading.Thread(target=watcher, daemon=True).start()
    log(f"camctl running on http://0.0.0.0:{PORT}  (settings: {CONF})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
