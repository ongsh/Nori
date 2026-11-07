# Install without cloning the repository

Nori is distributed as one `nori.pyz` application file, with a built-in offline user installer. You do not need Git, a checkout, pip, a virtual environment, or administrator privileges. The program necessarily occupies disk space, but it does not create a repository working copy. Python source is packaged inside the application archive; this is not source-code hiding or a standalone Python interpreter.

## Prerequisites

- Linux, or an already configured Windows WSL2 Linux distribution.
- Python 3.11+ available as `python3` inside that environment.
- Native Windows and macOS are unsupported. WSL2 is the intended Windows route; CI validates Linux, not an actual WSL2 machine.
- Do not run installation with sudo. Installing Python or enabling WSL2 is a separate operating-system setup step; this installer does not change the OS.

## Download and install

1. Sign in to GitHub with an account authorized for the private `ongsh/Nori` repository. Open **Actions**, select a successful run of branch `codex/nori-p1-rebuild-20261107`, and download its **nori-linux-wsl2** artifact. Verify the branch and commit you intend to trust. Artifacts expire after 30 days; a new successful run provides a new artifact.
2. Extract the downloaded artifact ZIP. It contains `nori.pyz` and `SHA256SUMS`, not a checkout. On Windows, extract with Explorer and use the extracted directory from WSL (for example under `/mnt/c/Users/.../Downloads/`).
3. In a Linux/WSL terminal in that directory, optionally verify transfer integrity with `sha256sum -c SHA256SUMS`, then install:

   ```sh
   python3 nori.pyz --install
   ```

4. Run from anywhere using `~/.local/bin/nori` to start the browser interface. If `~/.local/bin` is already on PATH, simply use `nori`. Otherwise, `export PATH="$HOME/.local/bin:$PATH"` enables the short command for the current shell. Add that line yourself to your shell configuration if desired; the installer never edits shell startup files.

The checksum detects corruption, not a malicious publisher. Only install an artifact from the trusted repository/run. No public release is required, the repository stays private, and no token is placed into a URL, script, argument, or config file. Browser download uses your existing GitHub access. Users without repository access cannot download the artifact; an authorized owner must provide the package through an approved private channel. Do not make the repository public to install it.

This is a single installation command after download, not a zero-prerequisite Windows double-click installer. No remote script is piped into a shell. The installer itself is offline and does not install or authenticate Codex or Claude.

## Verify safely

```sh
~/.local/bin/nori demo "$(mktemp -d)"
```

This creates only synthetic tasks in a fresh temporary directory. For persistent usage, create separate control and workspace directories using `nori init`, following README. Keep these outside the installation directory and back up the control state together with the workspace.

## Updates and removal

To update, first stop active Nori runs and close controllers, then run the new trusted package with `python3 nori.pyz --install` again. It replaces only the managed program and launcher. It refuses unrelated files named `nori`. It does not migrate project state or automatically upgrade while tasks are running. The launcher uses the Python interpreter that performed installation; reinstall if you remove or relocate that interpreter.

```sh
~/.local/bin/nori --uninstall
```

This removes `~/.local/lib/nori/nori.pyz` and `~/.local/bin/nori` only. It retains all project/control data and empty installation directories. You may delete the original downloaded package after installation.

## Maintainer build

```sh
python3 scripts/build_distribution.py
```

This reproducibly builds `dist/nori.pyz` and `dist/SHA256SUMS` from the package's Python files using only the standard library. CI runs the full test suite, including isolated installation, unrelated-file protection, a synthetic demo, installed subprocess supervision, and uninstall, before publishing a private workflow artifact. No release is created automatically.

Real Codex/Claude model integration has not been successfully validated. The framework's acceptance suite uses executable stand-ins and synthetic tasks; installing the package does not change that limitation.

## One-command online installation after making the repository public

The owner may choose to make this repository public; Nori never changes repository visibility. Public access exposes the repository and its history, not just the installer. If you do not want that, keep the private artifact installation above.

`scripts/install_online.py` downloads the packaged application (not a repository archive), verifies its embedded SHA-256, and invokes the offline installer. It requires an exact 40-character commit SHA. Use the same reviewed commit for the bootstrap and bundle, not `main` (which may not contain this implementation), a branch name, or `latest`.

The delivered command has this form, replacing both `REVIEWED_COMMIT_SHA` occurrences with the supplied verified commit:

```sh
(set -eu; d=$(mktemp -d); trap 'rm -rf "$d"' EXIT; curl --fail --silent --show-error --proto '=https' 'https://raw.githubusercontent.com/ongsh/Nori/REVIEWED_COMMIT_SHA/scripts/install_online.py' -o "$d/install.py"; python3 "$d/install.py" REVIEWED_COMMIT_SHA)
```

Run in Bash on Linux/WSL2 with Python 3.11+, curl, trusted CA certificates, and access to raw.githubusercontent.com. No sudo, clone, login token, release, or Actions-artifact download is required once the repository is publicly accessible. The temporary bootstrap/package are removed afterward; only the installed application remains. A failed download stops the command instead of executing an empty/partial response. The bootstrap rejects a wrong checksum, oversized payload, moving revision, and unexpected redirect before executing the package. The bootstrap itself is trusted via HTTPS plus the fixed reviewed commit, not by the bundled checksum; do not run a command from an untrusted sender.

Before public access exists, unauthenticated installation fails. It does not bypass private-repository restrictions or request tokens; use the private artifact route instead. The owner has since made the repository public. Anonymous public download and installation have been verified in an isolated temporary home. No visibility change or release is performed by this implementation.

Maintainers updating application code must rebuild `distribution/nori.pyz.b64` from `dist/nori.pyz` and update `BUNDLE_SHA256` in the bootstrap. CI verifies that the committed package exactly matches source and that corrupt downloads never execute.


## Browser startup

Run `~/.local/bin/nori` after installation. The default entry now starts a local browser interface instead of requiring a CLI action. If automatic opening is unavailable in WSL, paste the complete printed URL into your Windows browser. Keep the terminal/server running while using the UI. Do not expose or forward the server port publicly. An alternative port can be selected with `nori web --port 8766`.

Installation is per Linux user. If the terminal is running as root, the package is installed under `/root`, not your normal WSL account; the installer warns about this. Prefer your normal WSL account without sudo. Do not copy root credentials or state directories between accounts as a workaround.

Browser workflow acceptance runs in CI using Chromium on Ubuntu 24.04 against temporary synthetic projects. It covers creation, approval, execution, failure blocking dependencies, revision approval, rework, final PASS, and retained failure history. Actual WSL desktop browser opening has not been verified on the user’s machine.
