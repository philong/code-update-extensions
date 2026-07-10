#!/usr/bin/env python3
import sys
import os
import subprocess
import re
import json
import urllib.request
import urllib.error
import platform
import argparse
import tempfile
import shutil
import datetime
import time
import hashlib

try:
    import tty
    import termios
    import select

    HAS_TTY = True
except ImportError:
    HAS_TTY = False


class Colors:
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


def enable_colors():
    if not sys.stdout.isatty():
        Colors.BLUE = ""
        Colors.CYAN = ""
        Colors.GREEN = ""
        Colors.YELLOW = ""
        Colors.RED = ""
        Colors.ENDC = ""
        Colors.BOLD = ""


def get_local_target_platform():
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux":
        if "arm" in machine or "aarch64" in machine:
            return "linux-arm64" if "64" in machine else "linux-armhf"
        return "linux-x64"
    elif system == "darwin":
        return (
            "darwin-arm64"
            if ("arm" in machine or "aarch64" in machine)
            else "darwin-x64"
        )
    elif system == "windows":
        return (
            "win32-arm64" if ("arm" in machine or "aarch64" in machine) else "win32-x64"
        )
    return "universal"


def parse_version(v_str):
    parts = v_str.split("-")
    main_parts = parts[0].split(".")

    parsed_ints = []
    for p in main_parts:
        try:
            parsed_ints.append(int(p))
        except ValueError:
            digits = re.findall(r"\d+", p)
            if digits:
                parsed_ints.append(int("".join(digits)))
            else:
                parsed_ints.append(p)

    while len(parsed_ints) < 3:
        parsed_ints.append(0)

    is_release = len(parts) == 1

    prerelease_parts = ()
    if not is_release:
        raw_pre = parts[1].split(".")
        pre_parsed = []
        for x in raw_pre:
            try:
                pre_parsed.append(int(x))
            except ValueError:
                pre_parsed.append(x)
        prerelease_parts = tuple(pre_parsed)

    # Semver: numeric identifiers have lower precedence than alphanumeric ones.
    def comparable(parts):
        return tuple(
            (0, x) if isinstance(x, int) else (1, str(x)) for x in parts
        )

    return (comparable(parsed_ints), is_release, comparable(prerelease_parts))


def run_code_cmd(args, retries=3, delay=1.0):
    for attempt in range(retries + 1):
        try:
            return subprocess.run(args, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            if attempt < retries:
                cmd_str = " ".join(args)
                print(
                    f"{Colors.YELLOW}Warning: Command '{cmd_str}' failed with exit code {e.returncode}. Retrying in {delay}s... (attempt {attempt + 1}/{retries}){Colors.ENDC}",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise e


def get_installed_extensions(code_binary="code"):
    try:
        result = run_code_cmd([code_binary, "--list-extensions", "--show-versions"])
        output = result.stdout
    except Exception as e:
        print(
            f"{Colors.RED}Error running '{code_binary} --list-extensions --show-versions': {e}{Colors.ENDC}",
            file=sys.stderr,
        )
        sys.exit(1)

    extensions = {}
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or "@" not in line:
            continue
        ext_id, version = line.rsplit("@", 1)
        extensions[ext_id.lower()] = version

    return extensions


def is_prerelease(version_obj):
    properties = version_obj.get("properties", [])
    for p in properties:
        if (
            p.get("key") == "Microsoft.VisualStudio.Code.PreRelease"
            and p.get("value") == "true"
        ):
            return True
    return False


def get_vscode_version(code_binary="code"):
    try:
        result = run_code_cmd([code_binary, "--version"])
        lines = result.stdout.strip().splitlines()
        if lines:
            return lines[0].strip()
    except Exception:
        pass
    return None


def semver_parts(v_str):
    cleaned = re.sub(r"^[^0-9]+", "", v_str)
    main_part = cleaned.split("-")[0]
    parts = main_part.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return major, minor, patch
    except ValueError:
        return 0, 0, 0


def is_engine_compatible(vscode_version_str, engine_constraint):
    if not vscode_version_str or not engine_constraint:
        return True
    constraint_str = engine_constraint.strip()
    if constraint_str == "*" or constraint_str == "":
        return True
    if "||" in constraint_str:
        return any(
            is_engine_compatible(vscode_version_str, g)
            for g in constraint_str.split("||")
        )
    parts = [p.strip() for p in re.split(r"\s+", constraint_str) if p.strip()]
    if len(parts) > 1:
        return all(is_engine_compatible(vscode_version_str, p) for p in parts)
    single_constraint = parts[0]
    match = re.match(r"^([>=<~^]+)?(.*)$", single_constraint)
    if not match:
        return True
    op, version_str = match.groups()
    if not op:
        op = ">="
    parsed_vscode = parse_version(vscode_version_str)
    parsed_constraint = parse_version(version_str)
    if op in ("=", "=="):
        return parsed_vscode[0] == parsed_constraint[0]
    if op == ">=":
        return parsed_vscode >= parsed_constraint
    elif op == ">":
        return parsed_vscode > parsed_constraint
    elif op == "<=":
        return parsed_vscode <= parsed_constraint
    elif op == "<":
        return parsed_vscode < parsed_constraint
    elif op == "~":
        if parsed_vscode < parsed_constraint:
            return False
        major, minor, patch = semver_parts(version_str)
        next_minor_ver = f"{major}.{minor + 1}.0"
        return parsed_vscode < parse_version(next_minor_ver)
    elif op == "^":
        if parsed_vscode < parsed_constraint:
            return False
        major, minor, patch = semver_parts(version_str)
        if major > 0:
            return parsed_vscode < parse_version(f"{major + 1}.0.0")
        elif minor > 0:
            return parsed_vscode < parse_version(f"0.{minor + 1}.0")
        else:
            return parsed_vscode < parse_version(f"0.0.{patch + 1}")
    return True


def get_engine_constraint(version_obj):
    properties = version_obj.get("properties", [])
    for p in properties:
        if p.get("key") == "Microsoft.VisualStudio.Code.Engine":
            return p.get("value")
    return None


def parse_age_threshold(age_str):
    age_str = age_str.lower().strip()
    if age_str in ("0", "0h", "0d", "0m"):
        return datetime.timedelta(0)

    match = re.match(r"^(\d+)([hdm])$", age_str)
    if not match:
        raise ValueError(
            f"Invalid age format: '{age_str}'. Expected format like '24h', '1d', '30m'."
        )
    value, unit = match.groups()
    value = int(value)
    if unit == "h":
        return datetime.timedelta(hours=value)
    elif unit == "d":
        return datetime.timedelta(days=value)
    elif unit == "m":
        return datetime.timedelta(minutes=value)
    return datetime.timedelta(0)


def get_cache_dir():
    # Per-user cache dir: /tmp is world-writable with predictable filenames,
    # which would let another local user poison cached marketplace responses.
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    cache_dir = os.path.join(base, "code_update_extensions")
    try:
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    except Exception:
        return tempfile.gettempdir()
    return cache_dir


def cleanup_stale_cache():
    try:
        cache_dir = get_cache_dir()
        now = time.time()
        for filename in os.listdir(cache_dir):
            if filename.startswith("vscode_ext_cache_") and filename.endswith(".json"):
                filepath = os.path.join(cache_dir, filename)
                try:
                    if now - os.path.getmtime(filepath) > 3600:
                        os.remove(filepath)
                except Exception:
                    pass
    except Exception:
        pass


def check_updates(
    installed_exts,
    target_platform,
    vscode_version=None,
    exclude_prerelease=True,
    min_release_age=None,
    skip_versions=None,
    ignore_extensions=None,
):
    cleanup_stale_cache()
    ext_ids = list(installed_exts.keys())
    if ignore_extensions:
        ignored_set = {x.lower() for x in ignore_extensions}
        ext_ids = [eid for eid in ext_ids if eid not in ignored_set]
    batch_size = 50
    updates = []

    if sys.stdout.isatty():
        sys.stdout.write(f"\rChecking updates: [{' ' * 20}] 0% (0/{len(ext_ids)})")
        sys.stdout.flush()

    # Query in batches
    for i in range(0, len(ext_ids), batch_size):
        batch = ext_ids[i : i + batch_size]

        # Build payload
        criteria = [{"filterType": 8, "value": "Microsoft.VisualStudio.Code"}]
        for ext_id in batch:
            criteria.append({"filterType": 7, "value": ext_id})

        payload = {
            "filters": [
                {
                    "criteria": criteria,
                    "pageNumber": 1,
                    "pageSize": len(batch),
                    "sortBy": 0,
                    "sortOrder": 0,
                }
            ],
            "assetTypes": [],
            "flags": 411,
        }

        req_data = json.dumps(payload).encode("utf-8")

        # Check cache
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cache_file = os.path.join(
            get_cache_dir(), f"vscode_ext_cache_{payload_hash}.json"
        )

        resp_data = None
        if os.path.exists(cache_file):
            try:
                if time.time() - os.path.getmtime(cache_file) < 3600:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        resp_data = json.load(f)
            except Exception:
                pass

        if resp_data is None:
            req = urllib.request.Request(
                "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery?api-version=7.2-preview.1",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json; api-version=7.2-preview.1",
                },
                method="POST",
            )

            max_retries = 3
            backoff = 2.0
            for attempt in range(max_retries + 1):
                if attempt > 0 and sys.stdout.isatty():
                    percent = (i * 100) // len(ext_ids)
                    bar_len = 20
                    filled_len = int(round(bar_len * i / float(len(ext_ids))))
                    bar = "=" * filled_len + " " * (bar_len - filled_len)
                    sys.stdout.write(
                        f"\r\033[KChecking updates: [{bar}] {percent}% ({i}/{len(ext_ids)}) (retry {attempt})"
                    )
                    sys.stdout.flush()

                # retry_reason is set to a short description only for transient failures.
                retry_reason = None
                try:
                    with urllib.request.urlopen(req, timeout=30) as response:
                        resp_data = json.loads(response.read().decode("utf-8"))
                    # Write to cache
                    try:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(resp_data, f)
                    except Exception:
                        pass
                    break
                except urllib.error.HTTPError as e:
                    err = e
                    if 500 <= e.code < 600:
                        retry_reason = f"returned HTTP status {e.code}"
                except (urllib.error.URLError, TimeoutError) as e:
                    # Read timeouts surface as a bare TimeoutError; connect timeouts come
                    # wrapped in URLError with a "timed out" reason.
                    err = e
                    reason = str(getattr(e, "reason", e)).lower()
                    if (
                        isinstance(e, TimeoutError)
                        or "timed out" in reason
                        or "timeout" in reason
                    ):
                        retry_reason = "request timed out"
                except Exception as e:
                    err = e

                if retry_reason and attempt < max_retries:
                    if sys.stdout.isatty():
                        sys.stdout.write("\r\033[K")
                        sys.stdout.flush()
                    print(
                        f"{Colors.YELLOW}Marketplace API {retry_reason}. Retrying in {backoff}s... (attempt {attempt + 1}/{max_retries}){Colors.ENDC}",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    backoff *= 2.0
                else:
                    if sys.stdout.isatty():
                        sys.stdout.write("\r\033[K")
                        sys.stdout.flush()
                    print(
                        f"{Colors.RED}Failed to query marketplace API: {err}{Colors.ENDC}",
                        file=sys.stderr,
                    )
                    break

        if sys.stdout.isatty():
            current_count = min(i + batch_size, len(ext_ids))
            percent = (current_count * 100) // len(ext_ids)
            bar_len = 20
            filled_len = int(round(bar_len * current_count / float(len(ext_ids))))
            bar = "=" * filled_len + " " * (bar_len - filled_len)
            sys.stdout.write(
                f"\r\033[KChecking updates: [{bar}] {percent}% ({current_count}/{len(ext_ids)})"
            )
            sys.stdout.flush()

        if resp_data is None:
            continue

        results = resp_data.get("results", [])
        if not results:
            continue

        extensions = results[0].get("extensions", [])
        for ext in extensions:
            pub_name = ext.get("publisher", {}).get("publisherName", "")
            ext_name = ext.get("extensionName", "")
            full_id = f"{pub_name}.{ext_name}".lower()

            installed_ver = installed_exts.get(full_id)
            if not installed_ver:
                continue

            # Filter compatible versions
            compatible_versions = []
            for ver_obj in ext.get("versions", []):
                version_str = ver_obj.get("version")
                # Exclude skipped versions from config
                if skip_versions and full_id in skip_versions:
                    if version_str in skip_versions[full_id]:
                        continue
                # Exclude pre-releases if requested
                if exclude_prerelease and is_prerelease(ver_obj):
                    continue
                # Exclude incompatible VS Code versions if vscode_version is set
                if vscode_version:
                    engine_constraint = get_engine_constraint(ver_obj)
                    if engine_constraint and not is_engine_compatible(
                        vscode_version, engine_constraint
                    ):
                        continue
                ver_platform = ver_obj.get("targetPlatform")
                # Compatible if platform matches, or if it is universal (None / "universal")
                if ver_platform is None or ver_platform.lower() in (
                    "universal",
                    target_platform.lower(),
                ):
                    compatible_versions.append(ver_obj)

            if not compatible_versions:
                continue

            # Find the latest compatible version
            try:
                compatible_versions.sort(
                    key=lambda x: parse_version(x["version"]), reverse=True
                )
                latest_ver_obj = compatible_versions[0]
                latest_version = latest_ver_obj["version"]

                # Check if it's newer than installed
                if parse_version(latest_version) > parse_version(installed_ver):
                    # Find the latest eligible version
                    eligible_ver_obj = None
                    for ver_obj in compatible_versions:
                        if min_release_age and min_release_age > datetime.timedelta(0):
                            last_updated = ver_obj.get("lastUpdated")
                            if last_updated:
                                try:
                                    cleaned_ts = last_updated
                                    if cleaned_ts.endswith("Z"):
                                        cleaned_ts = cleaned_ts[:-1] + "+00:00"
                                    release_dt = datetime.datetime.fromisoformat(
                                        cleaned_ts
                                    )
                                    now = datetime.datetime.now(datetime.timezone.utc)
                                    if now - release_dt < min_release_age:
                                        continue
                                except Exception:
                                    pass
                        eligible_ver_obj = ver_obj
                        break

                    last_updated = latest_ver_obj.get("lastUpdated", "")
                    latest_release_date = (
                        last_updated[:10] if len(last_updated) >= 10 else last_updated
                    )

                    eligible_version = None
                    eligible_release_date = ""
                    eligible_platform = "universal"

                    if eligible_ver_obj:
                        el_ver = eligible_ver_obj["version"]
                        if parse_version(el_ver) > parse_version(installed_ver):
                            eligible_version = el_ver
                            el_updated = eligible_ver_obj.get("lastUpdated", "")
                            eligible_release_date = (
                                el_updated[:10] if len(el_updated) >= 10 else el_updated
                            )
                            eligible_platform = (
                                eligible_ver_obj.get("targetPlatform") or "universal"
                            )

                    updates.append(
                        {
                            "id": full_id,
                            "publisher": pub_name,
                            "name": ext_name,
                            "installed": installed_ver,
                            "latest": latest_version,
                            "latest_release_date": latest_release_date,
                            "latest_platform": latest_ver_obj.get("targetPlatform")
                            or "universal",
                            "eligible": eligible_version,
                            "eligible_release_date": eligible_release_date,
                            "eligible_platform": eligible_platform,
                        }
                    )
            except Exception as ex:
                print(
                    f"{Colors.RED}Warning: Error parsing versions for {full_id}: {ex}{Colors.ENDC}",
                    file=sys.stderr,
                )

    return updates


def vsix_filename(update):
    """Build the on-disk VSIX filename for an update (shared by download/install/cleanup)."""
    pub_name = update["publisher"]
    ext_name = update["name"]
    version = update["eligible"]
    platform = update["eligible_platform"]
    filename = f"{pub_name}.{ext_name}-{version}"
    if platform and platform != "universal":
        filename += f"-{platform}"
    return filename + ".vsix"


def download_updates(updates, download_dir):
    os.makedirs(download_dir, exist_ok=True)
    for update in updates:
        if not update["eligible"]:
            continue
        pub_name = update["publisher"]
        ext_name = update["name"]
        version = update["eligible"]
        platform = update["eligible_platform"]

        url = f"https://marketplace.visualstudio.com/_apis/public/gallery/publishers/{pub_name}/vsextensions/{ext_name}/{version}/vspackage"
        if platform and platform != "universal":
            url += f"?targetPlatform={platform}"

        filepath = os.path.join(download_dir, vsix_filename(update))

        print(
            f"Downloading {Colors.CYAN}{update['id']}{Colors.ENDC} v{Colors.GREEN}{version}{Colors.ENDC} ({platform})..."
        )

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                content_encoding = response.headers.get("Content-Encoding", "").lower()
                total_size = response.headers.get("Content-Length")
                if total_size:
                    try:
                        total_size = int(total_size)
                    except ValueError:
                        total_size = None

                chunks = []
                bytes_read = 0
                chunk_size = 32768  # 32 KB chunks

                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    bytes_read += len(chunk)

                    if total_size and total_size > 0:
                        percent = (bytes_read * 100) // total_size
                        bar_len = 30
                        filled_len = int(
                            round(bar_len * bytes_read / float(total_size))
                        )
                        bar = "=" * filled_len + " " * (bar_len - filled_len)

                        read_mb = bytes_read / (1024 * 1024)
                        total_mb = total_size / (1024 * 1024)

                        sys.stdout.write(
                            f"\r  [{bar}] {percent}% ({read_mb:.2f}MB / {total_mb:.2f}MB)"
                        )
                        sys.stdout.flush()
                    else:
                        read_mb = bytes_read / (1024 * 1024)
                        sys.stdout.write(f"\r  Downloaded: {read_mb:.2f}MB")
                        sys.stdout.flush()

                if total_size or bytes_read > 0:
                    sys.stdout.write("\n")
                    sys.stdout.flush()

                body = b"".join(chunks)

                if content_encoding == "gzip":
                    import gzip

                    body = gzip.decompress(body)
                elif content_encoding == "deflate":
                    import zlib

                    body = zlib.decompress(body)

                with open(filepath, "wb") as f:
                    f.write(body)
            print(f"  {Colors.GREEN}✓{Colors.ENDC} Saved to {filepath}")
        except Exception as e:
            print(
                f"  {Colors.RED}✗ Failed to download: {e}{Colors.ENDC}", file=sys.stderr
            )


def get_key():
    if not HAS_TTY:
        return None
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        rlist, _, _ = select.select([fd], [], [], None)
        if not rlist:
            return None
        b = os.read(fd, 1)
        if not b:
            return None
        ch = b.decode("utf-8", errors="ignore")
        if ch == "\x1b":
            rlist, _, _ = select.select([fd], [], [], 0.05)
            if rlist:
                b2 = os.read(fd, 1)
                ch2 = b2.decode("utf-8", errors="ignore") if b2 else ""
                if ch2 == "[":
                    rlist, _, _ = select.select([fd], [], [], 0.05)
                    if rlist:
                        b3 = os.read(fd, 1)
                        ch3 = b3.decode("utf-8", errors="ignore") if b3 else ""
                        if ch3 == "A":
                            return "up"
                        elif ch3 == "B":
                            return "down"
                        elif ch3 == "C":
                            return "right"
                        elif ch3 == "D":
                            return "left"
            return "esc"
        elif ch in ("\r", "\n"):
            return "enter"
        elif ch == " ":
            return "space"
        elif ch == "\x03":  # Ctrl+C
            return "ctrl+c"
        else:
            return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def cleanup_temp_files(updates, download_dir):
    print(f"\n{Colors.BLUE}Cleaning up temporary files...{Colors.ENDC}")
    for update in updates:
        if not update["eligible"]:
            continue
        filepath = os.path.join(download_dir, vsix_filename(update))
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as e:
                print(
                    f"  {Colors.YELLOW}Warning: Could not remove temporary file {filepath}: {e}{Colors.ENDC}",
                    file=sys.stderr,
                )


def truncate(text, width):
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def select_updates(updates):
    if not HAS_TTY or not sys.stdin.isatty() or not sys.stdout.isatty():
        return updates

    n = len(updates)
    selected = [bool(u["eligible"]) for u in updates]  # pre-select eligible updates
    cursor_idx = 0
    top = 0  # index of first visible row (scroll window)

    # Fixed column widths; the Extension ID column flexes to the terminal width.
    W_VER, W_DATE, W_PLAT = 12, 12, 12
    # Per-row width excluding the id column: "P C[xxx] <id> v v v d p" with single-space gaps.
    OVERHEAD = 6 + 1 + (W_VER + 1) * 3 + (W_DATE + 1) + W_PLAT

    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    def visual_len(s):
        return len(ansi_escape.sub("", s))

    first_frame = True
    prev_lines = 0

    # Hide the cursor and repaint each frame in-place, so wrapped lines or lists
    # taller than the terminal can never corrupt the display.
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            id_w = max(12, cols - OVERHEAD)
            row_width = OVERHEAD + id_w
            lines_per_row = max(1, -(-row_width // cols))  # ceil, accounts for wrapping
            # Reserve rows for instructions + column header + separator + status.
            win = max(1, min(n, (rows - 5) // lines_per_row))

            # Keep cursor inside the scroll window.
            if cursor_idx < top:
                top = cursor_idx
            elif cursor_idx >= top + win:
                top = cursor_idx - win + 1
            top = max(0, min(top, max(0, n - win)))

            out = []
            out.append(
                f"{Colors.GREEN}{Colors.BOLD}Space=toggle  a=toggle all  ↑/↓=move  Enter=install  Esc/Ctrl+C=cancel{Colors.ENDC}"
            )
            out.append(
                f"{Colors.BOLD}{'':6}{'Extension ID':<{id_w}} {'Installed':<{W_VER}} "
                f"{'Eligible':<{W_VER}} {'Latest':<{W_VER}} {'Release':<{W_DATE}} {'Platform':<{W_PLAT}}{Colors.ENDC}"
            )
            out.append("-" * min(cols, row_width))

            for i in range(top, top + win):
                update = updates[i]
                prefix = ">" if i == cursor_idx else " "
                if update["eligible"]:
                    mark = f"{Colors.GREEN}x{Colors.ENDC}" if selected[i] else " "
                    eligible_str = f"{Colors.GREEN}{truncate(update['eligible'], W_VER):<{W_VER}}{Colors.ENDC}"
                else:
                    mark = f"{Colors.YELLOW}!{Colors.ENDC}" if selected[i] else " "
                    eligible_str = f"{Colors.YELLOW}{'held back':<{W_VER}}{Colors.ENDC}"
                out.append(
                    f"{prefix} [{mark}] {Colors.CYAN}{truncate(update['id'], id_w):<{id_w}}{Colors.ENDC} "
                    f"{Colors.YELLOW}{truncate(update['installed'], W_VER):<{W_VER}}{Colors.ENDC} "
                    f"{eligible_str} "
                    f"{Colors.BLUE}{truncate(update['latest'], W_VER):<{W_VER}}{Colors.ENDC} "
                    f"{truncate(update['latest_release_date'], W_DATE):<{W_DATE}} "
                    f"{truncate(update['eligible_platform'] or update['latest_platform'], W_PLAT):<{W_PLAT}}"
                )

            if win < n:
                status = f"[{top + 1}-{top + win}/{n}]  (scroll with ↑/↓)"
            else:
                status = f"[{n} update{'s' if n != 1 else ''}]"
            out.append(f"{Colors.BOLD}{status}{Colors.ENDC}")

            # Repaint logic: move cursor to the top of the previously rendered area
            # and clear from the cursor to the bottom of the screen.
            if not first_frame:
                if prev_lines > 1:
                    sys.stdout.write(f"\r\033[{prev_lines - 1}A")
                else:
                    sys.stdout.write("\r")
                sys.stdout.write("\033[J")
            else:
                first_frame = False

            # Calculate total terminal lines this frame will print
            total_lines = 0
            for line in out:
                vlen = visual_len(line)
                total_lines += max(1, -(-vlen // cols))
            prev_lines = total_lines

            sys.stdout.write("\n".join(out))
            sys.stdout.flush()

            key = get_key()
            if key in ("ctrl+c", "esc"):
                raise KeyboardInterrupt
            elif key == "up":
                cursor_idx = (cursor_idx - 1) % n
            elif key == "down":
                cursor_idx = (cursor_idx + 1) % n
            elif key == "space":
                selected[cursor_idx] = not selected[cursor_idx]
            elif key in ("a", "A"):
                if any(selected):
                    # Untoggle all: uncheck everything, including overrides for held back.
                    selected = [False] * n
                else:
                    # Toggle all: check eligible updates, leaving held back ones alone.
                    selected = [bool(u["eligible"]) for u in updates]
            elif key == "enter":
                break

        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()

        chosen = []
        for i in range(n):
            if selected[i]:
                update = updates[i]
                if not update["eligible"]:
                    # User explicitly chose to install the held back latest version.
                    update["eligible"] = update["latest"]
                    update["eligible_platform"] = update["latest_platform"]
                chosen.append(update)
        return chosen

    except KeyboardInterrupt:
        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()
        print("Update selection cancelled.")
        sys.exit(0)
    except Exception:
        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()
        raise


def install_updates(updates, download_dir, code_binary="code"):
    for update in updates:
        if not update["eligible"]:
            continue
        version = update["eligible"]
        filepath = os.path.join(download_dir, vsix_filename(update))

        if not os.path.exists(filepath):
            print(
                f"{Colors.RED}VSIX file not found for installation: {filepath}{Colors.ENDC}",
                file=sys.stderr,
            )
            continue

        print(
            f"Installing {Colors.CYAN}{update['id']}{Colors.ENDC} v{Colors.GREEN}{version}{Colors.ENDC}..."
        )
        try:
            result = run_code_cmd([code_binary, "--install-extension", filepath])
            print(f"  {Colors.GREEN}✓{Colors.ENDC} Installed successfully.")
        except subprocess.CalledProcessError as e:
            print(
                f"  {Colors.RED}✗ Installation failed: {e.stderr.strip() or e}{Colors.ENDC}",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"  {Colors.RED}✗ Installation failed: {e}{Colors.ENDC}",
                file=sys.stderr,
            )


def strip_comment(line):
    in_quote = None
    for i, char in enumerate(line):
        if char in ('"', "'"):
            if in_quote == char:
                in_quote = None
            elif in_quote is None:
                in_quote = char
        elif char == "#" and in_quote is None:
            return line[:i].strip()
    return line.strip()


def parse_toml_fallback(content):
    data = {}
    current_section = None

    # Pre-process content to handle multi-line arrays
    lines = []
    accumulator = []
    in_array = False

    for raw_line in content.splitlines():
        line = strip_comment(raw_line)
        if not line:
            continue

        # Section header
        if line.startswith("[") and line.endswith("]") and not in_array:
            if accumulator:
                lines.append(" ".join(accumulator))
                accumulator = []
            lines.append(line)
            continue

        if "=" in line or in_array:
            if "=" in line and not in_array:
                if accumulator:
                    lines.append(" ".join(accumulator))
                    accumulator = []
                accumulator.append(line)
            else:
                accumulator.append(line)

            # Check if brackets are balanced in accumulator
            joined = " ".join(accumulator)
            # Count brackets outside quotes
            open_brackets = 0
            in_quote = None
            for char in joined:
                if char in ('"', "'"):
                    if in_quote == char:
                        in_quote = None
                    elif in_quote is None:
                        in_quote = char
                elif in_quote is None:
                    if char == "[":
                        open_brackets += 1
                    elif char == "]":
                        open_brackets -= 1

            if open_brackets <= 0:
                lines.append(joined)
                accumulator = []
                in_array = False
            else:
                in_array = True
        else:
            if accumulator:
                accumulator.append(line)
            else:
                lines.append(line)

    if accumulator:
        lines.append(" ".join(accumulator))

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            if current_section not in data:
                data[current_section] = {}
            continue

        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip().strip('"').strip("'")
            val = val.strip()

            if val.startswith("[") and val.endswith("]"):
                items = []
                in_quote = None
                current_item = []
                for char in val[1:-1]:
                    if char in ('"', "'"):
                        if in_quote == char:
                            in_quote = None
                        elif in_quote is None:
                            in_quote = char
                    elif char == "," and in_quote is None:
                        items.append(
                            "".join(current_item).strip().strip('"').strip("'")
                        )
                        current_item = []
                    else:
                        current_item.append(char)
                if current_item:
                    items.append("".join(current_item).strip().strip('"').strip("'"))
                parsed_val = [x for x in items if x]
            elif val.lower() == "true":
                parsed_val = True
            elif val.lower() == "false":
                parsed_val = False
            else:
                parsed_val = val.strip('"').strip("'")

            if current_section:
                if current_section not in data:
                    data[current_section] = {}
                data[current_section][key] = parsed_val
            else:
                data[key] = parsed_val
    return data


def load_config():
    config_path = os.path.expanduser("~/.config/code_update_extensions/config.toml")
    config = {"skip_versions": {}, "ignore": []}
    if not os.path.exists(config_path):
        return config

    try:
        try:
            import tomllib

            with open(config_path, "rb") as f:
                parsed = tomllib.load(f)
        except ImportError:
            try:
                import tomli as tomllib

                with open(config_path, "rb") as f:
                    parsed = tomllib.load(f)
            except ImportError:
                try:
                    import toml

                    with open(config_path, "r", encoding="utf-8") as f:
                        parsed = toml.load(f)
                except ImportError:
                    with open(config_path, "r", encoding="utf-8") as f:
                        parsed = parse_toml_fallback(f.read())
    except Exception as e:
        print(
            f"{Colors.YELLOW}Warning: Failed to parse config file '{config_path}': {e}{Colors.ENDC}",
            file=sys.stderr,
        )
        return config

    # Parse [skip_versions]
    skip_versions = parsed.get("skip_versions", {})
    if isinstance(skip_versions, dict):
        for ext_id, val in skip_versions.items():
            ext_id_lower = ext_id.lower()
            if isinstance(val, str):
                config["skip_versions"][ext_id_lower] = [val]
            elif isinstance(val, list):
                config["skip_versions"][ext_id_lower] = [str(v) for v in val]
            else:
                config["skip_versions"][ext_id_lower] = [str(val)]

    # Parse ignore
    ignore = parsed.get("ignore", parsed.get("ignore_extensions", []))
    if not isinstance(ignore, list):
        if isinstance(ignore, str):
            ignore = [ignore]
        else:
            ignore = []
    config["ignore"] = [str(eid).strip().lower() for eid in ignore]

    # Copy other keys
    for k in [
        "min_release_age",
        "min-release-age",
        "include_prerelease",
        "include-prerelease",
        "no_code_version_check",
        "no-code-version-check",
        "code_binary",
        "code-binary",
        "download_dir",
        "download-dir",
        "yes",
    ]:
        val = parsed.get(k)
        if val is not None:
            config[k] = val

    return config


def get_config_val(config, key, default=None):
    # Try underscore
    val = config.get(key.replace("-", "_"))
    if val is not None:
        return val
    # Try hyphen
    val = config.get(key.replace("_", "-"))
    if val is not None:
        return val
    return default


def main():
    parser = argparse.ArgumentParser(
        description="Check, download, and install VS Code extension updates."
    )
    parser.add_argument(
        "-p",
        "--include-prerelease",
        action="store_true",
        default=None,
        help="Include pre-release versions in update check",
    )
    parser.add_argument(
        "-n",
        "--no-code-version-check",
        action="store_true",
        default=None,
        help="Disable VS Code version compatibility check",
    )
    parser.add_argument(
        "-b",
        "--code-binary",
        default=None,
        help="Path to VS Code binary/executable or its fork (default: code)",
    )
    parser.add_argument(
        "-d",
        "--download-dir",
        nargs="?",
        const=".",
        default=None,
        help="Download updates to the specified directory. In interactive mode, this defaults to the system temporary directory if not specified. If specified without a path, defaults to the current directory.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=None,
        help="Automatically download and install all updates without prompting (useful for non-interactive environments)",
    )
    parser.add_argument(
        "-a",
        "--min-release-age",
        default=None,
        help="Minimum age of a release to be considered for update (e.g. 24h, 1d) to mitigate supply chain attacks (default: 24h)",
    )
    args = parser.parse_args()

    enable_colors()

    # Load config file
    config = load_config()

    # Resolve parameters (command line overrides config overrides default)
    include_prerelease = (
        args.include_prerelease
        if args.include_prerelease is not None
        else get_config_val(config, "include_prerelease", False)
    )
    no_code_version_check = (
        args.no_code_version_check
        if args.no_code_version_check is not None
        else get_config_val(config, "no_code_version_check", False)
    )
    code_binary_val = (
        args.code_binary
        if args.code_binary is not None
        else get_config_val(config, "code_binary", "code")
    )
    yes = args.yes if args.yes is not None else get_config_val(config, "yes", False)
    min_release_age_str = (
        args.min_release_age
        if args.min_release_age is not None
        else get_config_val(config, "min_release_age", "24h")
    )

    download_dir_is_temp = (
        args.download_dir is None and get_config_val(config, "download_dir") is None
    )
    download_dir = (
        args.download_dir
        if args.download_dir is not None
        else get_config_val(config, "download_dir", None)
    )

    try:
        min_release_age = parse_age_threshold(min_release_age_str)
    except ValueError as e:
        print(f"{Colors.RED}Error: {e}{Colors.ENDC}", file=sys.stderr)
        sys.exit(1)

    code_binary_path = os.path.expanduser(code_binary_val)
    code_binary = shutil.which(code_binary_path) or code_binary_path

    # Determine VS Code version to use for compatibility checks
    vscode_version = None
    if not no_code_version_check:
        vscode_version = get_vscode_version(code_binary)

    target_platform = get_local_target_platform()

    print(
        f"{Colors.BLUE}Local Target Platform:{Colors.ENDC} {Colors.BOLD}{target_platform}{Colors.ENDC}"
    )
    if min_release_age > datetime.timedelta(0):
        print(
            f"{Colors.BLUE}Min Release Age:{Colors.ENDC} {Colors.BOLD}{min_release_age_str}{Colors.ENDC}"
        )
    if no_code_version_check:
        print(
            f"{Colors.BLUE}VS Code Version Check:{Colors.ENDC} {Colors.BOLD}Disabled{Colors.ENDC}"
        )
    elif vscode_version:
        print(
            f"{Colors.BLUE}VS Code Version:{Colors.ENDC} {Colors.BOLD}{vscode_version}{Colors.ENDC}"
        )
    else:
        print(
            f"{Colors.YELLOW}Warning: Could not auto-detect VS Code version. Compatibility checks are skipped.{Colors.ENDC}"
        )

    print(f"{Colors.BLUE}Fetching installed VS Code extensions...{Colors.ENDC}")

    installed_exts = get_installed_extensions(code_binary)
    if not installed_exts:
        print("No extensions found.")
        return

    print(f"Found {len(installed_exts)} extensions installed.")
    print(
        f"{Colors.BLUE}Checking updates on VS Code Marketplace (including pre-releases: {include_prerelease})...{Colors.ENDC}"
    )

    skip_versions = config.get("skip_versions", {})
    ignore_extensions = config.get("ignore", [])
    updates = check_updates(
        installed_exts,
        target_platform,
        vscode_version=vscode_version,
        exclude_prerelease=not include_prerelease,
        min_release_age=min_release_age,
        skip_versions=skip_versions,
        ignore_extensions=ignore_extensions,
    )

    print()
    if updates:
        if yes:
            download_dir_resolved = (
                download_dir if download_dir is not None else tempfile.gettempdir()
            )
            print(
                f"{Colors.BLUE}Automatically downloading updates to:{Colors.ENDC} {Colors.BOLD}{download_dir_resolved}{Colors.ENDC}"
            )
            download_updates(updates, download_dir_resolved)
            print()
            print(f"{Colors.BLUE}Installing updates...{Colors.ENDC}")
            install_updates(updates, download_dir_resolved, code_binary=code_binary)
            if download_dir_is_temp:
                cleanup_temp_files(updates, download_dir_resolved)
        elif HAS_TTY and sys.stdin.isatty() and sys.stdout.isatty():
            selected_updates = select_updates(updates)
            if selected_updates:
                print()
                download_dir_resolved = (
                    download_dir if download_dir is not None else tempfile.gettempdir()
                )
                print(
                    f"{Colors.BLUE}Downloading updates to:{Colors.ENDC} {Colors.BOLD}{download_dir_resolved}{Colors.ENDC}"
                )
                download_updates(selected_updates, download_dir_resolved)
                print()
                print(f"{Colors.BLUE}Installing updates...{Colors.ENDC}")
                install_updates(
                    selected_updates, download_dir_resolved, code_binary=code_binary
                )
                if download_dir_is_temp:
                    cleanup_temp_files(selected_updates, download_dir_resolved)
            else:
                print("No updates selected for installation.")
        else:
            print(f"{Colors.GREEN}{Colors.BOLD}Updates available:{Colors.ENDC}")
            print(
                f"{Colors.BOLD}{'Extension ID':<45} {'Installed':<12} {'Eligible':<12} {'Latest':<12} {'Release Date':<15} {'Platform':<12}{Colors.ENDC}"
            )
            print("-" * 115)
            for update in updates:
                if update["eligible"]:
                    eligible_str = (
                        f"{Colors.GREEN}{update['eligible']:<12}{Colors.ENDC}"
                    )
                else:
                    eligible_str = f"{Colors.YELLOW}{'held back':<12}{Colors.ENDC}"

                print(
                    f"{Colors.CYAN}{update['id']:<45}{Colors.ENDC} "
                    f"{Colors.YELLOW}{update['installed']:<12}{Colors.ENDC} "
                    f"{eligible_str} "
                    f"{Colors.BLUE}{update['latest']:<12}{Colors.ENDC} "
                    f"{update['latest_release_date']:<15} "
                    f"{update['eligible_platform'] or update['latest_platform']:<12}"
                )

            if download_dir is not None:
                print()
                print(
                    f"{Colors.BLUE}Downloading updates to:{Colors.ENDC} {Colors.BOLD}{download_dir}{Colors.ENDC}"
                )
                download_updates(updates, download_dir)
    else:
        print(f"{Colors.GREEN}All extensions are up to date!{Colors.ENDC}")


if __name__ == "__main__":
    main()
