# VS Code Extension Updater (`code-update-extensions`)

A Python script to check, download, and install VS Code extension updates directly from the VS Code Marketplace. It features a rich, interactive terminal user interface (TUI) for selecting updates and security controls to mitigate supply-chain attacks.

## Features

- **Direct VS Code Marketplace API Integration**: Queries current extension versions directly from the marketplace.
- **Interactive Terminal UI**: An interactive scrolling menu inside your terminal to toggle, select, and review updates before installing.
- **Supply-Chain Security Mitigation**: Hold back updates that are too fresh (default `24h`) to ensure they have not been retracted or flag-analyzed.
- **Auto Platform & Architecture Resolution**: Detects and downloads platform-specific `.vsix` packages (e.g. `linux-x64`, `darwin-arm64`, `win32-x64`).
- **VS Code Version Compatibility Checks**: Automatically queries your installed VS Code version and verifies target extension requirements so you never install incompatible updates.
- **Alternative VS Code Forks Supported**: Works with alternative builds or forks (e.g. VSCodium, VS Code Insiders) by pointing the `--code-binary` argument to your target executable.

---

## Installation

Ensure you have Python 3 installed. You can run the script directly:

```bash
chmod +x code_update_extensions
./code_update_extensions
```

No external Python dependencies are required (uses standard library modules like `urllib`, `subprocess`, and `argparse`).

---

## Usage

```bash
./code_update_extensions [options]
```

### Options

| Option | Short | Description |
| :--- | :--- | :--- |
| `--include-prerelease` | `-p` | Include pre-release versions in the update check. |
| `--no-code-version-check` | `-n` | Disable verification of VS Code host compatibility. |
| `--code-binary <path/cmd>` | `-b` | Path to the VS Code binary or executable, optionally with custom flags (default: `code`). Supports `~` expansion. |
| `--download-dir [path]` | `-d` | Download `.vsix` files to the specified directory. Defaults to system temporary directory if omitted, or current directory (`.`) if specified without a path. |
| `--yes` | `-y` | Run non-interactively; automatically downloads and installs all updates. |
| `--min-release-age <age>` | `-a` | Minimum release age (e.g., `24h`, `3d`, `30m`, `0`) to mitigate supply-chain risks (default: `24h`). Set to `0` to disable. |

Boolean flags set in the configuration file can be overridden back from the command line with `--no-include-prerelease`, `--code-version-check`, and `--no-yes`.

---

## Configuration File

You can set defaults for command line flags and configure per-extension rules (`ignore`, `min-release-age`, `skip-versions`) via a TOML configuration file at `~/.config/code_update_extensions/config.toml`. Options specified via command line arguments will override those in the configuration file. Unknown keys or values with the wrong type produce a warning and are ignored.

### Example Configuration

```toml
# Global defaults for command line flags (hyphenated or snake_case keys are both accepted)
min-release-age = "12h"        # -a, --min-release-age (e.g., 24h, 3d, 30m, 0)
include-prerelease = false     # -p, --include-prerelease
no-code-version-check = false  # -n, --no-code-version-check
code-binary = "code"           # -b, --code-binary (supports flags and ~ expansion, e.g. "codium --user-data-dir ~/.config/VSCodium")
download-dir = "~/Downloads"   # -d, --download-dir
yes = false                    # -y, --yes

# Per-extension configuration tables
[extensions."ms-python.python"]
ignore = true                  # Ignore this extension entirely during update checks

[extensions."vscjava.vscode-gradle"]
min-release-age = "0"          # Override min-release-age (e.g. 0 to disable age buffer)
skip-versions = "3.17.3"       # Specific version(s) to skip (string or array of strings)

[extensions."golang.go"]
min-release-age = "3d"         # Require updates for this extension to be at least 3 days old
skip-versions = ["0.39.0", "0.39.1"]
```

---

## Examples

### 1. Interactive Mode (Default)
Run the script to look for updates and select which ones to install:
```bash
./code_update_extensions
```
* **Controls**:
  - `↑` / `↓` : Navigate list
  - `Space` : Toggle selection (can also select held-back updates marked with `!`)
  - `a` / `A` : Toggle all eligible updates
  - `Enter` : Confirm and install
  - `Esc` / `Ctrl+C` : Cancel

Held-back updates (newer than `--min-release-age`) are marked with `!` and unselected by default, but can be manually checked with `Space` to override the age buffer.

### 2. Auto-Upgrade All Extensions (Non-interactive)
Suitable for cron jobs or start-up scripts:
```bash
./code_update_extensions -y
```

### 3. Check with 3-Day Age Buffer & Pre-Releases
Include pre-releases but require updates to be at least 3 days old:
```bash
./code_update_extensions -p --min-release-age 3d
```

### 4. Update VSCodium Extensions
```bash
./code_update_extensions --code-binary codium
```

---

## License

This project is licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later) - see the [LICENSE](LICENSE) file for details.

