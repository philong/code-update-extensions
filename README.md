# VS Code Extension Manager (`code-extensions`)

A Python script to install, update, list, and remove VS Code extensions directly from the VS Code Marketplace or Open VSX Registry. It features an interactive terminal user interface (TUI) for selecting updates or extensions to remove, along with security controls to mitigate supply-chain attacks.

## Features

- **Subcommands**: Supports `install`, `update`, `list`, and `remove` commands.
- **Direct VS Code Marketplace API Integration**: Queries extension metadata and package assets directly from the marketplace gallery API.
- **Supply-Chain Security Mitigation**: Holds back releases newer than a specified age (default `24h`) across both `install` and `update` commands to ensure packages have not been compromised or flag-analyzed.
- **Interactive Terminal UI**: Scrollable interactive menus in your terminal to toggle, select, and review updates or removals.
- **Auto Platform & Architecture Resolution**: Detects and downloads platform-specific `.vsix` packages (e.g. `linux-x64`, `darwin-arm64`, `win32-x64`).
- **VS Code Version Compatibility Checks**: Verifies host VS Code version engine requirements so you never install incompatible extensions.
- **Alternative VS Code Extension Registries & Open VSX**: Query extensions from Open VSX or custom self-hosted VS Code Extension Galleries.
- **Alternative VS Code Forks Supported**: Works with alternative builds or forks (e.g. VSCodium, VS Code Insiders) via `--code-binary`.

---

## Installation

Ensure you have Python 3 installed. Make the script executable and run:

```bash
chmod +x code_extensions.py
./code-extensions --help
```

No external Python dependencies are required (uses standard library modules like `urllib`, `subprocess`, and `argparse`).

---

## Usage

```bash
code-extensions <command> [options]
```

### Commands

* `install`: Install extension(s) by ID (e.g. `publisher.name` or `publisher.name@version`).
* `update`: Check, download, and install updates for installed extensions.
* `list`: List installed extensions (with optional search query, quiet mode, or outdated filter).
* `remove`: Remove installed extension(s) by ID or interactively select extensions to remove.

### 1. `install` Command

```bash
code-extensions install <extension-id...> [options]
```

* Installs specified extension(s). Supports explicit version parameter (e.g., `ms-python.python@2024.1.0`).
* Respects `min-release-age` policy. If the latest release is too fresh, automatically selects the latest age-eligible version, or warns before proceeding.

**Options**:
* `-p`, `--include-prerelease`: Allow pre-release versions.
* `-n`, `--no-code-version-check`: Disable VS Code host compatibility check.
* `-d`, `--download-dir <path>`: Directory for downloading `.vsix` files.
* `-y`, `--yes`: Non-interactive mode.
* `-a`, `--min-release-age <age>`: Minimum release age threshold (e.g. `24h`, `3d`, `0`).

### 2. `update` Command

```bash
code-extensions update [options]
```

* Scans installed extensions, queries marketplace for updates, and launches interactive TUI selection (or auto-installs if `-y`).

**Options**:
* `-p`, `--include-prerelease`: Include pre-release versions in update check.
* `-n`, `--no-code-version-check`: Disable VS Code host compatibility check.
* `-d`, `--download-dir <path>`: Download directory for `.vsix` files.
* `-y`, `--yes`: Non-interactive auto-update.
* `-a`, `--min-release-age <age>`: Minimum release age threshold (e.g. `24h`, `3d`, `0`).

### 3. `list` Command

```bash
code-extensions list [query] [options]
```

* Lists installed extensions, optionally filtering by a search query string.

**Options**:
* `-q`, `--quiet`: Output raw extension IDs only (one per line, ideal for scripting).
* `-u`, `--outdated`: List only extensions that have updates available.

### 4. `remove` Command

```bash
code-extensions remove [extension-id...] [options]
```

* Removes specified extension(s).
* If no extension IDs are passed, launches an interactive TUI listing all installed extensions to select which ones to remove.

**Options**:
* `-y`, `--yes`: Skip confirmation prompt.

---

## Configuration File

Set defaults and per-extension rules in `~/.config/code_extensions/config.toml`:

```toml
min-release-age = "12h"        # Default release age buffer (e.g., 24h, 3d, 0)
include-prerelease = false
no-code-version-check = false
code-binary = "code"           # Path to executable or fork (e.g., "codium")
download-dir = "~/Downloads"
yes = false
open-vsx = false

[extensions."ms-python.python"]
ignore = true                  # Ignore during update checks

[extensions."golang.go"]
min-release-age = "3d"         # Require updates/installs for this extension to be at least 3 days old
skip-versions = ["0.39.0"]
```

---

## Examples

```bash
# Install extensions
./code-extensions install ms-python.python golang.go

# Install specific version respecting 12h release age
./code-extensions install ms-python.python@2024.1.0 -a 12h

# List installed extensions matching "python"
./code-extensions list python

# Export list of installed extension IDs to a file
./code-extensions list -q > extensions.txt

# List extensions that have updates available
./code-extensions list --outdated

# Interactive update check
./code-extensions update

# Auto-upgrade all extensions non-interactively
./code-extensions update -y

# Remove extension directly
./code-extensions remove ms-python.python

# Interactive removal menu
./code-extensions remove
```

---

## License

This project is licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later) - see the [LICENSE](LICENSE) file for details.
