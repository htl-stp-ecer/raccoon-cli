"""SSH key management for Raccoon client."""

import os
import shlex
from pathlib import Path
from typing import Optional, Tuple

import paramiko


class SSHKeyError(Exception):
    """Raised when a key cannot be loaded, generated or installed on a target."""


class SSHKeyManager:
    """
    Manages the local SSH key pair and installs its public half on a target.

    Handles:
    - Finding an existing key (Ed25519 preferred, RSA fallback)
    - Generating a new Ed25519 key pair when none exists
    - Appending the public key to a target's ``authorized_keys``
    """

    # Prefer Ed25519, fall back to RSA
    KEY_TYPES = [
        ("id_ed25519", paramiko.Ed25519Key),
        ("id_rsa", paramiko.RSAKey),
    ]

    def __init__(self):
        self.ssh_dir = Path.home() / ".ssh"

    def find_existing_key(self) -> Optional[Tuple[Path, paramiko.PKey]]:
        """
        Find a usable SSH private key.

        Returns:
            Tuple of (path, key) for the first key that loads, None otherwise
        """
        for key_name, key_class in self.KEY_TYPES:
            key_path = self.ssh_dir / key_name
            if key_path.exists():
                try:
                    return key_path, key_class.from_private_key_file(str(key_path))
                except Exception:
                    # Exists but cannot be loaded (e.g. passphrase protected)
                    continue
        return None

    def unloadable_key_files(self) -> list[Path]:
        """Key files present on disk. Used when none loads, so they are never overwritten."""
        return [
            self.ssh_dir / name
            for name, _ in self.KEY_TYPES
            if (self.ssh_dir / name).exists()
        ]

    def ensure_key(self) -> Tuple[Path, paramiko.PKey, bool]:
        """
        Return a usable key, generating one if no key file exists at all.

        Returns:
            Tuple of (path, key, generated)

        Raises:
            SSHKeyError: A key file exists but cannot be loaded. It is left
                untouched; overwriting a user's key would lock them out elsewhere.
        """
        existing = self.find_existing_key()
        if existing:
            return (*existing, False)

        blocked = self.unloadable_key_files()
        if blocked:
            names = ", ".join(str(p) for p in blocked)
            raise SSHKeyError(
                f"{names} exists but cannot be loaded (passphrase protected?). "
                "Remove the passphrase or move the file, then try again."
            )

        path, key = self.generate_key()
        return path, key, True

    def generate_key(self, key_path: Optional[Path] = None) -> Tuple[Path, paramiko.PKey]:
        """
        Generate a new SSH key pair (Ed25519 preferred, RSA fallback).

        Args:
            key_path: Path for the private key (default: ~/.ssh/id_ed25519)

        Returns:
            Tuple of (private_key_path, key)
        """
        # Ensure .ssh directory exists with proper permissions
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)

        # Try Ed25519 first (requires paramiko >= 3.0), fall back to RSA
        try:
            key = paramiko.Ed25519Key.generate()
            if key_path is None:
                key_path = self.ssh_dir / "id_ed25519"
        except AttributeError:
            # paramiko < 3.0 doesn't have Ed25519Key.generate()
            key = paramiko.RSAKey.generate(4096)
            if key_path is None:
                key_path = self.ssh_dir / "id_rsa"

        # Save private key
        key.write_private_key_file(str(key_path))
        os.chmod(key_path, 0o600)

        # Save public key
        pub_key_path = Path(str(key_path) + ".pub")
        with open(pub_key_path, "w") as f:
            f.write(f"{key.get_name()} {key.get_base64()} raccoon-generated\n")
        os.chmod(pub_key_path, 0o644)

        return key_path, key

    def get_public_key_string(self, key: paramiko.PKey) -> str:
        """Get the public key in OpenSSH format."""
        return f"{key.get_name()} {key.get_base64()}"

    def deposit_public_key(self, client: paramiko.SSHClient, key: paramiko.PKey) -> None:
        """
        Append the public key to ``~/.ssh/authorized_keys`` on the connected target.

        Equivalent to ``ssh-copy-id``: creates the directory and file with the
        right permissions and adds the key only if it is not already present.

        Args:
            client: An already authenticated SSH client (any method)
            key: The key pair whose public half is installed

        Raises:
            SSHKeyError: The remote command failed
        """
        b64 = shlex.quote(key.get_base64())
        line = shlex.quote(f"{self.get_public_key_string(key)} raccoon-client")
        command = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh "
            "&& touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys "
            f"&& (grep -qF {b64} ~/.ssh/authorized_keys "
            # Leading newline: a file without trailing newline must not swallow the key
            f"|| printf '\\n%s\\n' {line} >> ~/.ssh/authorized_keys)"
        )
        _, stdout, stderr = client.exec_command(command)
        status = stdout.channel.recv_exit_status()
        if status != 0:
            detail = stderr.read().decode(errors="replace").strip()
            raise SSHKeyError(detail or f"remote command exited with status {status}")
