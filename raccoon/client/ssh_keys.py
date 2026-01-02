"""SSH key management for Raccoon client."""

import getpass
import os
from pathlib import Path
from typing import Optional, Tuple

import paramiko


class SSHKeyManager:
    """
    Manages SSH key generation and deployment for Raccoon.

    Handles:
    - Checking for existing SSH keys
    - Generating new key pairs (Ed25519)
    - Uploading public keys to remote hosts
    """

    # Prefer Ed25519, fall back to RSA
    KEY_TYPES = [
        ("id_ed25519", paramiko.Ed25519Key),
        ("id_rsa", paramiko.RSAKey),
    ]

    def __init__(self):
        self.ssh_dir = Path.home() / ".ssh"

    def get_default_key_path(self) -> Path:
        """Get the default SSH key path (Ed25519)."""
        return self.ssh_dir / "id_ed25519"

    def find_existing_key(self) -> Optional[Tuple[Path, paramiko.PKey]]:
        """
        Find an existing SSH private key.

        Returns:
            Tuple of (path, key) if found, None otherwise
        """
        for key_name, key_class in self.KEY_TYPES:
            key_path = self.ssh_dir / key_name
            if key_path.exists():
                try:
                    key = key_class.from_private_key_file(str(key_path))
                    return key_path, key
                except Exception:
                    # Key exists but couldn't be loaded (maybe passphrase protected)
                    # Try loading with SSH agent
                    pass
        return None

    def has_ssh_key(self) -> bool:
        """Check if any SSH key exists."""
        for key_name, _ in self.KEY_TYPES:
            if (self.ssh_dir / key_name).exists():
                return True
        return False

    def generate_key(self, key_path: Optional[Path] = None) -> Tuple[Path, paramiko.PKey]:
        """
        Generate a new Ed25519 SSH key pair.

        Args:
            key_path: Path for the private key (default: ~/.ssh/id_ed25519)

        Returns:
            Tuple of (private_key_path, key)
        """
        if key_path is None:
            key_path = self.get_default_key_path()

        # Ensure .ssh directory exists with proper permissions
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)

        # Generate Ed25519 key
        key = paramiko.Ed25519Key.generate()

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

    def upload_public_key(
        self,
        host: str,
        username: str,
        key: paramiko.PKey,
        password: str,
    ) -> bool:
        """
        Upload a public key to a remote host using password authentication.

        Args:
            host: Remote hostname or IP
            username: SSH username
            key: The key pair (we'll upload the public key)
            password: Password for authentication

        Returns:
            True if successful
        """
        pub_key = self.get_public_key_string(key)

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=host,
                username=username,
                password=password,
                look_for_keys=False,
                allow_agent=False,
            )

            # Create .ssh directory and add key to authorized_keys
            commands = [
                "mkdir -p ~/.ssh",
                "chmod 700 ~/.ssh",
                "touch ~/.ssh/authorized_keys",
                "chmod 600 ~/.ssh/authorized_keys",
                # Add key if not already present
                f'grep -q "{key.get_base64()}" ~/.ssh/authorized_keys || echo "{pub_key} raccoon-client" >> ~/.ssh/authorized_keys',
            ]

            for cmd in commands:
                stdin, stdout, stderr = client.exec_command(cmd)
                stdout.read()  # Wait for completion

            client.close()
            return True

        except paramiko.AuthenticationException:
            return False
        except Exception:
            return False

    def test_key_auth(self, host: str, username: str, key: paramiko.PKey) -> bool:
        """
        Test if key authentication works.

        Args:
            host: Remote hostname or IP
            username: SSH username
            key: The private key to test

        Returns:
            True if authentication succeeded
        """
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=host,
                username=username,
                pkey=key,
                look_for_keys=False,
                allow_agent=False,
            )
            client.close()
            return True
        except paramiko.AuthenticationException:
            return False
        except Exception:
            return False


def setup_ssh_key_interactive(
    host: str,
    username: str,
    console=None,
) -> Optional[paramiko.PKey]:
    """
    Interactive SSH key setup.

    Checks for existing keys, generates if needed, and uploads to remote host.

    Args:
        host: Remote hostname or IP
        username: SSH username
        console: Rich console for output (optional)

    Returns:
        The SSH key if setup successful, None otherwise
    """
    from rich.console import Console
    from rich.prompt import Prompt, Confirm

    console = console or Console()
    manager = SSHKeyManager()

    # Check for existing key
    existing = manager.find_existing_key()

    if existing:
        key_path, key = existing
        console.print(f"[dim]Found existing SSH key: {key_path}[/dim]")

        # Test if it already works
        if manager.test_key_auth(host, username, key):
            console.print("[green]SSH key authentication already working![/green]")
            return key
    else:
        # Generate new key
        console.print("[cyan]No SSH key found. Generating new Ed25519 key...[/cyan]")
        key_path, key = manager.generate_key()
        console.print(f"[green]Generated new SSH key: {key_path}[/green]")

    # Need to upload key
    console.print()
    console.print(f"[cyan]Setting up SSH key authentication with {host}...[/cyan]")
    console.print("[dim]This requires your password once to upload the public key.[/dim]")

    # Get password
    password = Prompt.ask(f"Password for {username}@{host}", password=True)

    if not password:
        console.print("[yellow]Cancelled.[/yellow]")
        return None

    # Upload key
    console.print("[dim]Uploading public key...[/dim]")

    if manager.upload_public_key(host, username, key, password):
        console.print("[green]SSH key uploaded successfully![/green]")

        # Verify it works
        if manager.test_key_auth(host, username, key):
            console.print("[green]SSH key authentication is now working![/green]")
            return key
        else:
            console.print("[yellow]Key uploaded but authentication still failing.[/yellow]")
            console.print("[dim]The Pi may need sshd restarted or have different auth settings.[/dim]")
            return None
    else:
        console.print("[red]Failed to upload SSH key.[/red]")
        console.print("[dim]Check your password and try again.[/dim]")
        return None
