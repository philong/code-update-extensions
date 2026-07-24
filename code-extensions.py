#!/usr/bin/env python3
#
# Copyright (C) 2026 Phi-Long Do
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from functools import lru_cache

try:
    import tty
    import termios
    import select

    HAS_TTY = True
except ImportError:
    HAS_TTY = False

DEFAULT_SERVICE_URL = "https://marketplace.visualstudio.com/_apis/public/gallery"
OPEN_VSX_SERVICE_URL = "https://open-vsx.org/vscode/gallery"


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


@lru_cache(maxsize=4096)
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

    def comparable(parts):
        return tuple((0, x) if isinstance(x, int) else (1, str(x)) for x in parts)

    return (comparable(parsed_ints), is_release, comparable(prerelease_parts))


def parse_code_binary(code_binary):
    if isinstance(code_binary, (list, tuple)):
        tokens = [str(x) for x in code_binary]
    elif isinstance(code_binary, str):
        try:
            tokens = shlex.split(code_binary)
        except Exception:
            tokens = [code_binary]
    elif code_binary:
        tokens = [str(code_binary)]
    else:
        tokens = ["code"]

    if not tokens:
        tokens = ["code"]

    executable = os.path.expanduser(tokens[0])
    resolved_exec = shutil.which(executable) or executable
    return [resolved_exec] + tokens[1:]


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
    binary_cmd = parse_code_binary(code_binary)
    full_cmd = binary_cmd + ["--list-extensions", "--show-versions"]
    try:
        result = run_code_cmd(full_cmd)
        output = result.stdout
    except Exception as e:
        cmd_str = " ".join(full_cmd)
        print(
            f"{Colors.RED}Error running '{cmd_str}': {e}{Colors.ENDC}",
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
    binary_cmd = parse_code_binary(code_binary)
    full_cmd = binary_cmd + ["--version"]
    try:
        result = run_code_cmd(full_cmd)
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


@lru_cache(maxsize=4096)
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
    if not age_str:
        return datetime.timedelta(0)
    age_str = str(age_str).lower().strip()
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
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    cache_dir = os.path.join(base, "code-extensions")
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
    lines = []
    accumulator = []
    in_array = False

    for raw_line in content.splitlines():
        line = strip_comment(raw_line)
        if not line:
            continue

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

            joined = " ".join(accumulator)
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
            sec = line[1:-1].strip()
            if "." in sec:
                parts = [p.strip().strip('"').strip("'") for p in sec.split(".", 1)]
                top_sec, sub_sec = parts[0], parts[1]
                if top_sec not in data or not isinstance(data[top_sec], dict):
                    data[top_sec] = {}
                if sub_sec not in data[top_sec] or not isinstance(
                    data[top_sec][sub_sec], dict
                ):
                    data[top_sec][sub_sec] = {}
                current_section = (top_sec, sub_sec)
            else:
                sec_name = sec.strip('"').strip("'")
                if sec_name not in data or not isinstance(data[sec_name], dict):
                    data[sec_name] = {}
                current_section = sec_name
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

            if isinstance(current_section, tuple):
                data[current_section[0]][current_section[1]][key] = parsed_val
            elif current_section:
                if current_section not in data or not isinstance(
                    data[current_section], dict
                ):
                    data[current_section] = {}
                data[current_section][key] = parsed_val
            else:
                data[key] = parsed_val
    return data


CONFIG_OPTION_TYPES = {
    "include_prerelease": bool,
    "no_code_version_check": bool,
    "yes": bool,
    "code_binary": str,
    "download_dir": str,
    "min_release_age": str,
    "service_url": str,
    "open_vsx": bool,
}


def coerce_config_value(val, expected_type):
    if expected_type is bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, str) and val.strip().lower() in ("true", "false"):
            return val.strip().lower() == "true"
        raise ValueError(f"expected true or false, got {val!r}")
    if isinstance(val, str):
        return val
    raise ValueError(f"expected a string, got {val!r}")


def load_config():
    config_path = os.path.expanduser("~/.config/code-extensions/config.toml")
    config = {"extensions": {}}
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

    ext_sections = {}
    if "extensions" in parsed and isinstance(parsed["extensions"], dict):
        ext_sections.update(parsed["extensions"])
    if "extension" in parsed and isinstance(parsed["extension"], dict):
        ext_sections.update(parsed["extension"])

    for ext_id, ext_data in ext_sections.items():
        if not isinstance(ext_data, dict):
            continue
        ext_id_lower = str(ext_id).strip().lower()
        norm_ext_cfg = {}

        if "ignore" in ext_data:
            val = ext_data["ignore"]
            if isinstance(val, bool):
                norm_ext_cfg["ignore"] = val
            elif isinstance(val, str) and val.strip().lower() in ("true", "false"):
                norm_ext_cfg["ignore"] = val.strip().lower() == "true"

        for age_key in ("min_release_age", "min-release-age"):
            if age_key in ext_data:
                norm_ext_cfg["min_release_age"] = str(ext_data[age_key])

        for skip_key in ("skip_versions", "skip-versions"):
            if skip_key in ext_data:
                val = ext_data[skip_key]
                if isinstance(val, str):
                    norm_ext_cfg["skip_versions"] = [val]
                elif isinstance(val, list):
                    norm_ext_cfg["skip_versions"] = [str(v) for v in val]
                else:
                    norm_ext_cfg["skip_versions"] = [str(val)]

        config["extensions"][ext_id_lower] = norm_ext_cfg

    for key, val in parsed.items():
        if key in ("extensions", "extension"):
            continue
        norm_key = key.replace("-", "_")
        if norm_key not in CONFIG_OPTION_TYPES:
            print(
                f"{Colors.YELLOW}Warning: Unknown option '{key}' in config file '{config_path}'.{Colors.ENDC}",
                file=sys.stderr,
            )
            continue
        try:
            config[norm_key] = coerce_config_value(val, CONFIG_OPTION_TYPES[norm_key])
        except ValueError as e:
            print(
                f"{Colors.YELLOW}Warning: Invalid value for '{key}' in config file '{config_path}': {e}. Ignoring.{Colors.ENDC}",
                file=sys.stderr,
            )

    return config


def resolve_option(args_val, config, key, default):
    if args_val is not None:
        return args_val
    val = config.get(key)
    if val is not None:
        return val
    return default


def get_vsix_download_url(
    ver_obj, pub_name, ext_name, version, platform, service_url=DEFAULT_SERVICE_URL
):
    if ver_obj and isinstance(ver_obj, dict):
        for f in ver_obj.get("files") or []:
            asset_type = f.get("assetType", "")
            if asset_type in (
                "Microsoft.VisualStudio.Services.VSIXPackage",
                "Microsoft.VisualStudio.Code.VSIXPackage",
            ) and f.get("source"):
                url = f["source"]
                if (
                    platform
                    and platform != "universal"
                    and "targetPlatform=" not in url
                ):
                    sep = "&" if "?" in url else "?"
                    url += f"{sep}targetPlatform={platform}"
                return url

    base_url = service_url.rstrip("/")
    url = (
        f"{base_url}/publishers/{pub_name}/vsextensions/{ext_name}/{version}/vspackage"
    )
    if platform and platform != "universal":
        url += f"?targetPlatform={platform}"
    return url


def vsix_filename(pub_name, ext_name, version, platform):
    filename = f"{pub_name}.{ext_name}-{version}"
    if platform and platform != "universal":
        filename += f"-{platform}"
    return filename + ".vsix"


def query_marketplace_extensions(ext_ids, service_url=DEFAULT_SERVICE_URL):
    cleanup_stale_cache()
    if not ext_ids:
        return {}

    batch_size = 50
    extension_map = {}

    for i in range(0, len(ext_ids), batch_size):
        batch = ext_ids[i : i + batch_size]
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
            "flags": 17,
        }

        req_data = json.dumps(payload).encode("utf-8")
        cache_key_data = {"service_url": service_url, "payload": payload}
        payload_hash = hashlib.sha256(
            json.dumps(cache_key_data, sort_keys=True).encode("utf-8")
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
            query_endpoint = f"{service_url.rstrip('/')}/extensionquery"
            if "api-version=" not in query_endpoint:
                query_endpoint += "?api-version=7.2-preview.1"

            req = urllib.request.Request(
                query_endpoint,
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
                retry_reason = None
                retry_after = None
                try:
                    with urllib.request.urlopen(req, timeout=30) as response:
                        resp_data = json.loads(response.read().decode("utf-8"))
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
                    elif e.code == 429:
                        retry_reason = "rate limited (HTTP 429)"
                        ra = e.headers.get("Retry-After")
                        if ra and ra.strip().isdigit():
                            retry_after = float(ra.strip())
                except (urllib.error.URLError, TimeoutError) as e:
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
                    delay = retry_after if retry_after is not None else backoff
                    print(
                        f"{Colors.YELLOW}Marketplace API {retry_reason}. Retrying in {delay}s... (attempt {attempt + 1}/{max_retries}){Colors.ENDC}",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    backoff *= 2.0
                else:
                    print(
                        f"{Colors.RED}Failed to query marketplace API: {err}{Colors.ENDC}",
                        file=sys.stderr,
                    )
                    break

        if not resp_data:
            continue

        results = resp_data.get("results", [])
        if not results:
            continue

        extensions = results[0].get("extensions", [])
        for ext in extensions:
            pub_name = ext.get("publisher", {}).get("publisherName", "")
            ext_name = ext.get("extensionName", "")
            full_id = f"{pub_name}.{ext_name}".lower()
            extension_map[full_id] = ext

    return extension_map


def download_vsix(url, filepath):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    show_progress = sys.stdout.isatty()

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    )

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
        chunk_size = 32768

        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)

            if not show_progress:
                continue
            if total_size and total_size > 0:
                percent = (bytes_read * 100) // total_size
                bar_len = 30
                filled_len = int(round(bar_len * bytes_read / float(total_size)))
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

        if show_progress and (total_size or bytes_read > 0):
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
        elif ch == "\x03":
            return "ctrl+c"
        else:
            return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def truncate(text, width):
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def prompt_yes_no(question, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        reply = input(f"{Colors.YELLOW}{question}{suffix}{Colors.ENDC}").strip().lower()
        if not reply:
            return default
        return reply.startswith("y")
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def handle_install(args, config):
    code_binary = parse_code_binary(
        resolve_option(args.code_binary, config, "code_binary", "code")
    )
    open_vsx = resolve_option(args.open_vsx, config, "open_vsx", False)
    service_url = (
        OPEN_VSX_SERVICE_URL
        if open_vsx
        else resolve_option(
            args.service_url, config, "service_url", DEFAULT_SERVICE_URL
        ).rstrip("/")
    )

    include_prerelease = resolve_option(
        args.include_prerelease, config, "include_prerelease", False
    )
    no_code_version_check = resolve_option(
        args.no_code_version_check, config, "no_code_version_check", False
    )
    yes = resolve_option(args.yes, config, "yes", False)
    min_release_age_str = resolve_option(
        args.min_release_age, config, "min_release_age", "24h"
    )

    try:
        min_release_age = parse_age_threshold(min_release_age_str)
    except ValueError as e:
        print(f"{Colors.RED}Error: {e}{Colors.ENDC}", file=sys.stderr)
        sys.exit(1)

    vscode_version = None if no_code_version_check else get_vscode_version(code_binary)
    target_platform = get_local_target_platform()
    extensions_config = config.get("extensions", {})

    parsed_targets = []
    for spec in args.extensions:
        spec = spec.strip()
        if "@" in spec:
            ext_id, req_ver = spec.rsplit("@", 1)
        else:
            ext_id, req_ver = spec, None
        ext_id_lower = ext_id.lower()
        if "." not in ext_id_lower:
            print(
                f"{Colors.RED}Error: Invalid extension ID '{spec}'. Expected format 'publisher.name' or 'publisher.name@version'.{Colors.ENDC}",
                file=sys.stderr,
            )
            continue
        parsed_targets.append((ext_id_lower, req_ver))

    if not parsed_targets:
        print("No valid extensions specified for installation.")
        return

    ext_ids = [t[0] for t in parsed_targets]
    print(f"{Colors.BLUE}Querying extension gallery for installation...{Colors.ENDC}")
    marketplace_data = query_marketplace_extensions(ext_ids, service_url=service_url)

    download_dir = resolve_option(args.download_dir, config, "download_dir", None)
    if download_dir is not None:
        download_dir = os.path.expanduser(download_dir)
    download_dir_resolved = (
        download_dir if download_dir is not None else tempfile.gettempdir()
    )
    download_dir_is_temp = download_dir is None

    for ext_id, req_ver in parsed_targets:
        ext_obj = marketplace_data.get(ext_id)
        if not ext_obj:
            print(
                f"{Colors.RED}✗ Extension '{ext_id}' not found on extension gallery.{Colors.ENDC}"
            )
            continue

        pub_name = ext_obj.get("publisher", {}).get("publisherName", "")
        ext_name = ext_obj.get("extensionName", "")
        full_id = f"{pub_name}.{ext_name}".lower()

        ext_cfg = extensions_config.get(full_id, {})
        eff_min_age = min_release_age
        if args.min_release_age is None and "min_release_age" in ext_cfg:
            try:
                eff_min_age = parse_age_threshold(ext_cfg["min_release_age"])
            except ValueError:
                pass

        versions = ext_obj.get("versions", [])
        compatible_versions = []
        for ver_obj in versions:
            v_str = ver_obj.get("version")
            if not v_str:
                continue
            if req_ver and v_str != req_ver:
                continue
            if not req_ver and not include_prerelease and is_prerelease(ver_obj):
                continue
            if vscode_version:
                engine_constraint = get_engine_constraint(ver_obj)
                if engine_constraint and not is_engine_compatible(
                    vscode_version, engine_constraint
                ):
                    continue
            ver_platform = ver_obj.get("targetPlatform")
            if ver_platform is None or ver_platform.lower() in (
                "universal",
                target_platform.lower(),
            ):
                compatible_versions.append(ver_obj)

        if not compatible_versions:
            if req_ver:
                print(
                    f"{Colors.RED}✗ Version '{req_ver}' for '{full_id}' not found or incompatible with host platform/VS Code.{Colors.ENDC}"
                )
            else:
                print(
                    f"{Colors.RED}✗ No compatible version of '{full_id}' found.{Colors.ENDC}"
                )
            continue

        compatible_versions.sort(
            key=lambda x: parse_version(x["version"]), reverse=True
        )
        latest_ver_obj = compatible_versions[0]

        eligible_ver_obj = None
        for ver_obj in compatible_versions:
            if eff_min_age and eff_min_age > datetime.timedelta(0):
                last_updated = ver_obj.get("lastUpdated")
                if last_updated:
                    try:
                        cleaned_ts = (
                            last_updated[:-1] + "+00:00"
                            if last_updated.endswith("Z")
                            else last_updated
                        )
                        release_dt = datetime.datetime.fromisoformat(cleaned_ts)
                        now = datetime.datetime.now(datetime.timezone.utc)
                        if now - release_dt < eff_min_age:
                            continue
                    except Exception:
                        pass
            eligible_ver_obj = ver_obj
            break

        selected_ver_obj = None

        if req_ver:
            target_ver_obj = compatible_versions[0]
            last_updated = target_ver_obj.get("lastUpdated")
            is_too_fresh = False
            if eff_min_age and eff_min_age > datetime.timedelta(0) and last_updated:
                try:
                    cleaned_ts = (
                        last_updated[:-1] + "+00:00"
                        if last_updated.endswith("Z")
                        else last_updated
                    )
                    release_dt = datetime.datetime.fromisoformat(cleaned_ts)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if now - release_dt < eff_min_age:
                        is_too_fresh = True
                except Exception:
                    pass

            if is_too_fresh:
                print(
                    f"{Colors.YELLOW}Warning: Requested version '{req_ver}' of '{full_id}' was released less than {eff_min_age} ago.{Colors.ENDC}"
                )
                if not yes:
                    if not sys.stdin.isatty() or not prompt_yes_no(
                        f"Do you want to install '{full_id}@{req_ver}' despite minimum release age policy?"
                    ):
                        print(f"Skipping installation of '{full_id}@{req_ver}'.")
                        continue
                else:
                    print(
                        f"Installing '{full_id}@{req_ver}' due to explicit version parameter."
                    )
            selected_ver_obj = target_ver_obj
        else:
            if eligible_ver_obj:
                selected_ver_obj = eligible_ver_obj
                if eligible_ver_obj != latest_ver_obj:
                    print(
                        f"{Colors.YELLOW}Notice: Latest version '{latest_ver_obj['version']}' of '{full_id}' is held back by minimum release age policy ({eff_min_age_str if 'eff_min_age_str' in locals() else min_release_age_str}). Installing latest eligible version '{eligible_ver_obj['version']}'.{Colors.ENDC}"
                    )
            else:
                latest_ver_str = latest_ver_obj["version"]
                print(
                    f"{Colors.YELLOW}Warning: Latest version '{latest_ver_str}' of '{full_id}' is held back by minimum release age policy, and no older compatible release was found.{Colors.ENDC}"
                )
                if not yes and sys.stdin.isatty():
                    if prompt_yes_no(
                        f"Install held-back version '{latest_ver_str}' anyway?"
                    ):
                        selected_ver_obj = latest_ver_obj
                    else:
                        print(f"Skipping '{full_id}'.")
                        continue
                else:
                    print(
                        f"{Colors.RED}Skipping '{full_id}' (held back by release age requirement). Use --min-release-age 0 to override.{Colors.ENDC}"
                    )
                    continue

        target_version = selected_ver_obj["version"]
        target_plat = selected_ver_obj.get("targetPlatform") or "universal"
        url = get_vsix_download_url(
            selected_ver_obj,
            pub_name,
            ext_name,
            target_version,
            target_plat,
            service_url,
        )
        filename = vsix_filename(pub_name, ext_name, target_version, target_plat)
        filepath = os.path.join(download_dir_resolved, filename)

        print(
            f"Downloading {Colors.CYAN}{full_id}{Colors.ENDC} v{Colors.GREEN}{target_version}{Colors.ENDC} ({target_plat})..."
        )
        try:
            download_vsix(url, filepath)
        except Exception as e:
            print(f"{Colors.RED}✗ Download failed: {e}{Colors.ENDC}", file=sys.stderr)
            continue

        print(
            f"Installing {Colors.CYAN}{full_id}{Colors.ENDC} v{Colors.GREEN}{target_version}{Colors.ENDC}..."
        )
        try:
            run_code_cmd(code_binary + ["--install-extension", filepath], retries=0)
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

        if download_dir_is_temp and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass


def check_updates(
    installed_exts,
    target_platform,
    vscode_version=None,
    exclude_prerelease=True,
    min_release_age=None,
    extensions_config=None,
    cli_min_release_age_override=False,
    service_url=DEFAULT_SERVICE_URL,
):
    ext_ids = list(installed_exts.keys())
    if extensions_config:
        ext_ids = [
            eid
            for eid in ext_ids
            if not extensions_config.get(eid.lower(), {}).get("ignore", False)
        ]

    marketplace_data = query_marketplace_extensions(ext_ids, service_url=service_url)
    updates = []

    for full_id, ext in marketplace_data.items():
        pub_name = ext.get("publisher", {}).get("publisherName", "")
        ext_name = ext.get("extensionName", "")
        installed_ver = installed_exts.get(full_id)
        if not installed_ver:
            continue

        parsed_installed = parse_version(installed_ver)
        ext_cfg = extensions_config.get(full_id, {}) if extensions_config else {}
        skipped_versions = ext_cfg.get("skip_versions", [])

        compatible_versions = []
        for ver_obj in ext.get("versions", []):
            version_str = ver_obj.get("version")
            if not version_str:
                continue
            if parse_version(version_str) <= parsed_installed:
                break
            if skipped_versions and version_str in skipped_versions:
                continue
            if exclude_prerelease and is_prerelease(ver_obj):
                continue
            if vscode_version:
                engine_constraint = get_engine_constraint(ver_obj)
                if engine_constraint and not is_engine_compatible(
                    vscode_version, engine_constraint
                ):
                    continue
            ver_platform = ver_obj.get("targetPlatform")
            if ver_platform is None or ver_platform.lower() in (
                "universal",
                target_platform.lower(),
            ):
                compatible_versions.append(ver_obj)

        if not compatible_versions:
            continue

        compatible_versions.sort(
            key=lambda x: parse_version(x["version"]), reverse=True
        )
        latest_ver_obj = compatible_versions[0]
        latest_version = latest_ver_obj["version"]

        eff_min_age = min_release_age
        if not cli_min_release_age_override and "min_release_age" in ext_cfg:
            try:
                eff_min_age = parse_age_threshold(ext_cfg["min_release_age"])
            except ValueError:
                pass

        if parse_version(latest_version) > parse_version(installed_ver):
            eligible_ver_obj = None
            for ver_obj in compatible_versions:
                if eff_min_age and eff_min_age > datetime.timedelta(0):
                    last_updated = ver_obj.get("lastUpdated")
                    if last_updated:
                        try:
                            cleaned_ts = (
                                last_updated[:-1] + "+00:00"
                                if last_updated.endswith("Z")
                                else last_updated
                            )
                            release_dt = datetime.datetime.fromisoformat(cleaned_ts)
                            now = datetime.datetime.now(datetime.timezone.utc)
                            if now - release_dt < eff_min_age:
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

            latest_download_url = get_vsix_download_url(
                latest_ver_obj,
                pub_name,
                ext_name,
                latest_version,
                latest_ver_obj.get("targetPlatform"),
                service_url,
            )
            eligible_download_url = (
                get_vsix_download_url(
                    eligible_ver_obj,
                    pub_name,
                    ext_name,
                    eligible_version,
                    eligible_platform,
                    service_url,
                )
                if eligible_ver_obj
                else None
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
                    "latest_download_url": latest_download_url,
                    "eligible": eligible_version,
                    "eligible_release_date": eligible_release_date,
                    "eligible_platform": eligible_platform,
                    "eligible_download_url": eligible_download_url,
                }
            )

    updates.sort(key=lambda u: u["id"])
    return updates


def print_updates_table(updates):
    print(
        f"{Colors.BOLD}{'Extension ID':<45} {'Installed':<12} {'Eligible':<12} {'Latest':<12} {'Release Date':<15} {'Platform':<12}{Colors.ENDC}"
    )
    print("-" * 115)
    for update in updates:
        eligible_str = (
            f"{Colors.GREEN}{update['eligible']:<12}{Colors.ENDC}"
            if update["eligible"]
            else f"{Colors.YELLOW}{'held back':<12}{Colors.ENDC}"
        )
        print(
            f"{Colors.CYAN}{update['id']:<45}{Colors.ENDC} "
            f"{Colors.YELLOW}{update['installed']:<12}{Colors.ENDC} "
            f"{eligible_str} "
            f"{Colors.BLUE}{update['latest']:<12}{Colors.ENDC} "
            f"{update['latest_release_date']:<15} "
            f"{update['eligible_platform'] or update['latest_platform']:<12}"
        )


def select_updates(updates):
    if not HAS_TTY or not sys.stdin.isatty() or not sys.stdout.isatty():
        return updates

    n = len(updates)
    selected = [bool(u["eligible"]) for u in updates]
    cursor_idx = 0
    top = 0

    W_VER, W_DATE, W_PLAT = 12, 12, 12
    OVERHEAD = 6 + 1 + (W_VER + 1) * 3 + (W_DATE + 1) + W_PLAT
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    def visual_len(s):
        return len(ansi_escape.sub("", s))

    first_frame = True
    prev_lines = 0

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            id_w = max(12, cols - OVERHEAD)
            row_width = OVERHEAD + id_w
            lines_per_row = max(1, -(-row_width // cols))
            win = max(1, min(n, (rows - 5) // lines_per_row))

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

            status = (
                f"[{top + 1}-{top + win}/{n}]  (scroll with ↑/↓)"
                if win < n
                else f"[{n} update{'s' if n != 1 else ''}]"
            )
            out.append(f"{Colors.BOLD}{status}{Colors.ENDC}")

            if not first_frame:
                if prev_lines > 1:
                    sys.stdout.write(f"\r\033[{prev_lines - 1}A")
                else:
                    sys.stdout.write("\r")
                sys.stdout.write("\033[J")
            else:
                first_frame = False

            total_lines = sum(max(1, -(-visual_len(line) // cols)) for line in out)
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
                selected = (
                    [False] * n
                    if any(selected)
                    else [bool(u["eligible"]) for u in updates]
                )
            elif key == "enter":
                break

        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()

        chosen = []
        for i in range(n):
            if selected[i]:
                update = updates[i]
                if not update["eligible"]:
                    update["eligible"] = update["latest"]
                    update["eligible_platform"] = update["latest_platform"]
                    update["eligible_release_date"] = update["latest_release_date"]
                    update["eligible_download_url"] = update.get("latest_download_url")
                chosen.append(update)
        return chosen

    except KeyboardInterrupt:
        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()
        print("Selection cancelled.")
        sys.exit(0)


def handle_update(args, config):
    code_binary = parse_code_binary(
        resolve_option(args.code_binary, config, "code_binary", "code")
    )
    open_vsx = resolve_option(args.open_vsx, config, "open_vsx", False)
    service_url = (
        OPEN_VSX_SERVICE_URL
        if open_vsx
        else resolve_option(
            args.service_url, config, "service_url", DEFAULT_SERVICE_URL
        ).rstrip("/")
    )

    include_prerelease = resolve_option(
        args.include_prerelease, config, "include_prerelease", False
    )
    no_code_version_check = resolve_option(
        args.no_code_version_check, config, "no_code_version_check", False
    )
    yes = resolve_option(args.yes, config, "yes", False)
    min_release_age_str = resolve_option(
        args.min_release_age, config, "min_release_age", "24h"
    )

    try:
        min_release_age = parse_age_threshold(min_release_age_str)
    except ValueError as e:
        print(f"{Colors.RED}Error: {e}{Colors.ENDC}", file=sys.stderr)
        sys.exit(1)

    vscode_version = None if no_code_version_check else get_vscode_version(code_binary)
    target_platform = get_local_target_platform()

    print(f"{Colors.BLUE}Fetching installed VS Code extensions...{Colors.ENDC}")
    installed_exts = get_installed_extensions(code_binary)
    if not installed_exts:
        print("No extensions found.")
        return

    print(f"Found {len(installed_exts)} extensions installed.")
    print(
        f"{Colors.BLUE}Checking updates (including pre-releases: {include_prerelease})...{Colors.ENDC}"
    )

    cli_min_release_age_override = args.min_release_age is not None
    extensions_config = config.get("extensions", {})
    updates = check_updates(
        installed_exts,
        target_platform,
        vscode_version=vscode_version,
        exclude_prerelease=not include_prerelease,
        min_release_age=min_release_age,
        extensions_config=extensions_config,
        cli_min_release_age_override=cli_min_release_age_override,
        service_url=service_url,
    )

    print()
    if not updates:
        print(f"{Colors.GREEN}All extensions are up to date!{Colors.ENDC}")
        return

    download_dir = resolve_option(args.download_dir, config, "download_dir", None)
    if download_dir is not None:
        download_dir = os.path.expanduser(download_dir)
    download_dir_resolved = (
        download_dir if download_dir is not None else tempfile.gettempdir()
    )
    download_dir_is_temp = download_dir is None

    if yes:
        print(f"{Colors.GREEN}{Colors.BOLD}Updates available:{Colors.ENDC}")
        print_updates_table(updates)
        print()
        eligible_updates = [u for u in updates if u["eligible"]]
        if not eligible_updates:
            print(
                f"{Colors.YELLOW}All available updates are held back by minimum release age policy; nothing to install.{Colors.ENDC}"
            )
            return
        selected_updates = eligible_updates
    elif HAS_TTY and sys.stdin.isatty() and sys.stdout.isatty():
        selected_updates = select_updates(updates)
        if not selected_updates:
            print("No updates selected for installation.")
            return
    else:
        print(f"{Colors.GREEN}{Colors.BOLD}Updates available:{Colors.ENDC}")
        print_updates_table(updates)
        selected_updates = [u for u in updates if u["eligible"]]

    for update in selected_updates:
        pub_name = update["publisher"]
        ext_name = update["name"]
        version = update["eligible"]
        platform = update["eligible_platform"]
        url = update.get("eligible_download_url") or get_vsix_download_url(
            {}, pub_name, ext_name, version, platform, service_url
        )
        filepath = os.path.join(
            download_dir_resolved, vsix_filename(pub_name, ext_name, version, platform)
        )

        print(
            f"Downloading {Colors.CYAN}{update['id']}{Colors.ENDC} v{Colors.GREEN}{version}{Colors.ENDC} ({platform})..."
        )
        try:
            download_vsix(url, filepath)
        except Exception as e:
            print(f"{Colors.RED}✗ Download failed: {e}{Colors.ENDC}", file=sys.stderr)
            continue

        print(
            f"Installing {Colors.CYAN}{update['id']}{Colors.ENDC} v{Colors.GREEN}{version}{Colors.ENDC}..."
        )
        try:
            run_code_cmd(code_binary + ["--install-extension", filepath], retries=0)
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

        if download_dir_is_temp and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass


def select_removals(installed_exts):
    if not HAS_TTY or not sys.stdin.isatty() or not sys.stdout.isatty():
        return []

    ext_list = sorted(installed_exts.items(), key=lambda x: x[0])
    n = len(ext_list)
    if n == 0:
        return []

    selected = [False] * n
    cursor_idx = 0
    top = 0

    W_VER = 15
    OVERHEAD = 6 + 1 + W_VER + 1  # 6 prefix + 1 gap + 15 version + 1 safety buffer
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    def visual_len(s):
        return len(ansi_escape.sub("", s))

    first_frame = True
    prev_lines = 0

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            id_w = max(12, cols - OVERHEAD)
            row_width = OVERHEAD + id_w
            lines_per_row = max(1, -(-row_width // cols))
            win = max(1, min(n, (rows - 5) // lines_per_row))

            if cursor_idx < top:
                top = cursor_idx
            elif cursor_idx >= top + win:
                top = cursor_idx - win + 1
            top = max(0, min(top, max(0, n - win)))

            out = []
            out.append(
                f"{Colors.RED}{Colors.BOLD}Space=toggle  a=toggle all  ↑/↓=move  Enter=uninstall  Esc/Ctrl+C=cancel{Colors.ENDC}"
            )
            out.append(
                f"{Colors.BOLD}{'':6}{'Extension ID':<{id_w}} {'Version':<{W_VER}}{Colors.ENDC}"
            )
            out.append("-" * min(cols, row_width))

            for i in range(top, top + win):
                ext_id, ver = ext_list[i]
                prefix = ">" if i == cursor_idx else " "
                mark = f"{Colors.RED}x{Colors.ENDC}" if selected[i] else " "
                out.append(
                    f"{prefix} [{mark}] {Colors.CYAN}{truncate(ext_id, id_w):<{id_w}}{Colors.ENDC} "
                    f"{Colors.YELLOW}{truncate(ver, W_VER):<{W_VER}}{Colors.ENDC}"
                )

            status = (
                f"[{top + 1}-{top + win}/{n}]  (scroll with ↑/↓)"
                if win < n
                else f"[{n} extension{'s' if n != 1 else ''}]"
            )
            out.append(f"{Colors.BOLD}{status}{Colors.ENDC}")

            if not first_frame:
                if prev_lines > 1:
                    sys.stdout.write(f"\r\033[{prev_lines - 1}A")
                else:
                    sys.stdout.write("\r")
                sys.stdout.write("\033[J")
            else:
                first_frame = False

            total_lines = sum(max(1, -(-visual_len(line) // cols)) for line in out)
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
                selected = [False] * n if any(selected) else [True] * n
            elif key == "enter":
                break

        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()

        return [ext_list[i][0] for i in range(n) if selected[i]]

    except KeyboardInterrupt:
        sys.stdout.write("\n\033[?25h")
        sys.stdout.flush()
        print("Removal selection cancelled.")
        sys.exit(0)


def handle_remove(args, config):
    code_binary = parse_code_binary(
        resolve_option(args.code_binary, config, "code_binary", "code")
    )
    yes = resolve_option(args.yes, config, "yes", False)

    installed_exts = get_installed_extensions(code_binary)
    if not installed_exts:
        print("No extensions found installed.")
        return

    targets = []
    if args.extensions:
        for spec in args.extensions:
            spec_lower = spec.strip().lower()
            if spec_lower not in installed_exts:
                print(
                    f"{Colors.YELLOW}Warning: Extension '{spec}' is not currently installed.{Colors.ENDC}",
                    file=sys.stderr,
                )
            else:
                targets.append(spec_lower)
        if not targets:
            print("No matching installed extensions to remove.")
            return
    else:
        if HAS_TTY and sys.stdin.isatty() and sys.stdout.isatty():
            targets = select_removals(installed_exts)
            if not targets:
                print("No extensions selected for removal.")
                return
        else:
            print(
                f"{Colors.RED}Error: Standard input is non-interactive. Please specify extension ID(s) to remove.{Colors.ENDC}",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"\n{Colors.RED}{Colors.BOLD}Extensions to remove:{Colors.ENDC}")
    for t in targets:
        print(f"  - {t} (v{installed_exts.get(t, 'unknown')})")

    if not yes:
        if not prompt_yes_no(
            f"Are you sure you want to remove {len(targets)} extension(s)?",
            default=False,
        ):
            print("Removal cancelled.")
            return

    for ext_id in targets:
        print(f"Removing {Colors.CYAN}{ext_id}{Colors.ENDC}...")
        try:
            run_code_cmd(code_binary + ["--uninstall-extension", ext_id], retries=0)
            print(f"  {Colors.GREEN}✓{Colors.ENDC} Removed successfully.")
        except subprocess.CalledProcessError as e:
            print(
                f"  {Colors.RED}✗ Removal failed: {e.stderr.strip() or e}{Colors.ENDC}",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"  {Colors.RED}✗ Removal failed: {e}{Colors.ENDC}", file=sys.stderr)


def handle_list(args, config):
    code_binary = parse_code_binary(
        resolve_option(args.code_binary, config, "code_binary", "code")
    )
    installed_exts = get_installed_extensions(code_binary)

    if not installed_exts:
        print("No extensions found installed.")
        return

    ext_items = sorted(installed_exts.items(), key=lambda x: x[0])

    if args.query:
        q = args.query.strip().lower()
        ext_items = [item for item in ext_items if q in item[0]]

    if args.outdated:
        open_vsx = resolve_option(args.open_vsx, config, "open_vsx", False)
        service_url = (
            OPEN_VSX_SERVICE_URL
            if open_vsx
            else resolve_option(
                args.service_url, config, "service_url", DEFAULT_SERVICE_URL
            ).rstrip("/")
        )
        vscode_version = get_vscode_version(code_binary)
        target_platform = get_local_target_platform()
        min_release_age_str = resolve_option(None, config, "min_release_age", "24h")
        min_release_age = parse_age_threshold(min_release_age_str)

        filtered_dict = dict(ext_items)
        updates = check_updates(
            filtered_dict,
            target_platform,
            vscode_version=vscode_version,
            exclude_prerelease=True,
            min_release_age=min_release_age,
            extensions_config=config.get("extensions", {}),
            service_url=service_url,
        )
        update_ids = {u["id"]: u for u in updates}
        ext_items = [item for item in ext_items if item[0] in update_ids]

        if args.quiet:
            for ext_id, _ in ext_items:
                print(ext_id)
            return

        if not ext_items:
            print(f"{Colors.GREEN}All extensions are up to date!{Colors.ENDC}")
            return

        print(
            f"{Colors.BOLD}{'Extension ID':<45} {'Installed':<15} {'Latest':<15}{Colors.ENDC}"
        )
        print("-" * 77)
        for ext_id, installed_ver in ext_items:
            up_info = update_ids[ext_id]
            latest_str = up_info["latest"]
            print(
                f"{Colors.CYAN}{ext_id:<45}{Colors.ENDC} {Colors.YELLOW}{installed_ver:<15}{Colors.ENDC} {Colors.GREEN}{latest_str:<15}{Colors.ENDC}"
            )
        return

    if args.quiet:
        for ext_id, _ in ext_items:
            print(ext_id)
        return

    print(f"{Colors.BOLD}{'Extension ID':<45} {'Version':<15}{Colors.ENDC}")
    print("-" * 62)
    for ext_id, ver in ext_items:
        print(
            f"{Colors.CYAN}{ext_id:<45}{Colors.ENDC} {Colors.YELLOW}{ver:<15}{Colors.ENDC}"
        )
    print(f"\nTotal: {len(ext_items)} extension(s)")


def main():
    enable_colors()
    config = load_config()

    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "-b",
        "--code-binary",
        default=None,
        help="Path to VS Code binary/executable or its fork (default: code)",
    )
    parent_parser.add_argument(
        "-s",
        "--service-url",
        default=None,
        help="VS Code Extension Gallery service API URL",
    )
    parent_parser.add_argument(
        "--open-vsx",
        action="store_true",
        default=None,
        help="Use Open VSX Registry (https://open-vsx.org/vscode/gallery)",
    )

    parser = argparse.ArgumentParser(
        prog="code-extensions",
        description="VS Code Extension Manager: Install, update, list, and remove extensions with security controls.",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Subcommand to execute"
    )

    # Install sub-parser
    parser_install = subparsers.add_parser(
        "install",
        parents=[parent_parser],
        help="Install VS Code extension(s) by ID (e.g. publisher.name or publisher.name@version)",
    )
    parser_install.add_argument(
        "extensions",
        nargs="+",
        help="Extension ID(s) to install (e.g. ms-python.python or ms-python.python@2024.1.0)",
    )
    parser_install.add_argument(
        "-p",
        "--include-prerelease",
        action="store_true",
        default=None,
        help="Allow pre-release versions",
    )
    parser_install.add_argument(
        "-n",
        "--no-code-version-check",
        action="store_true",
        default=None,
        help="Disable VS Code version compatibility check",
    )
    parser_install.add_argument(
        "-d",
        "--download-dir",
        default=None,
        help="Download directory for .vsix files",
    )
    parser_install.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=None,
        help="Non-interactive mode (automatically accept held-back or eligible versions)",
    )
    parser_install.add_argument(
        "-a",
        "--min-release-age",
        default=None,
        help="Minimum release age threshold (e.g. 24h, 3d, 0)",
    )

    # Update sub-parser
    parser_update = subparsers.add_parser(
        "update",
        parents=[parent_parser],
        help="Check, download, and install updates for installed extensions",
    )
    parser_update.add_argument(
        "-p",
        "--include-prerelease",
        action="store_true",
        default=None,
        help="Include pre-release versions in update check",
    )
    parser_update.add_argument(
        "-n",
        "--no-code-version-check",
        action="store_true",
        default=None,
        help="Disable VS Code version compatibility check",
    )
    parser_update.add_argument(
        "-d",
        "--download-dir",
        default=None,
        help="Download directory for .vsix files",
    )
    parser_update.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=None,
        help="Automatically download and install all updates without prompting",
    )
    parser_update.add_argument(
        "-a",
        "--min-release-age",
        default=None,
        help="Minimum release age threshold (e.g. 24h, 3d, 0)",
    )

    # Remove sub-parser
    parser_remove = subparsers.add_parser(
        "remove",
        parents=[parent_parser],
        help="Remove installed extension(s)",
    )
    parser_remove.add_argument(
        "extensions",
        nargs="*",
        default=[],
        help="Extension ID(s) to remove (if omitted, launches interactive removal TUI)",
    )
    parser_remove.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=None,
        help="Skip confirmation prompt",
    )

    # List sub-parser
    parser_list = subparsers.add_parser(
        "list",
        parents=[parent_parser],
        help="List installed extension(s)",
    )
    parser_list.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Optional search query to filter extensions by ID",
    )
    parser_list.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Output raw extension IDs only (one per line, ideal for scripting)",
    )
    parser_list.add_argument(
        "-u",
        "--outdated",
        action="store_true",
        default=False,
        help="List only extensions that have updates available",
    )

    args = parser.parse_args()

    if args.command == "install":
        handle_install(args, config)
    elif args.command == "update":
        handle_update(args, config)
    elif args.command == "remove":
        handle_remove(args, config)
    elif args.command == "list":
        handle_list(args, config)


if __name__ == "__main__":
    main()
