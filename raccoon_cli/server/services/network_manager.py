"""Server-side network management for Wi-Fi, hotspot, and LAN status."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AP_CONNECTION_NAME = "STP-Velox-AP"

# Hotspot network served by hostapd + dnsmasq (same range NetworkManager's
# "shared" mode uses).
AP_ADDRESS = "10.42.0.1"
AP_PREFIX = 24
AP_DHCP_RANGE = "10.42.0.10,10.42.0.254,255.255.255.0,1h"

# Ethernet Mode: ask the wired network for an address (DHCP) first; if nobody
# answers within this many seconds the robot hosts its own network, with the
# same address and DHCP range as the hotspot.
ETHERNET_DHCP_TIMEOUT = 10


class NetworkManager:
    """Thin wrapper around nmcli/ip. Saved networks live in NetworkManager;
    only the network mode and hotspot config are persisted locally."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or (
            Path.home() / ".raccoon" / "network_state.json"
        )
        self._lan_cable_missing_ticks = 0

    def scan_networks(self) -> list[dict[str, Any]]:
        self._ensure_wifi_enabled()
        self._run(["nmcli", "device", "wifi", "rescan"])
        result = self._run(["nmcli", "-f", "SSID,SECURITY,IN-USE", "dev", "wifi"])

        saved_ssids = {network["ssid"] for network in self.get_saved_networks()}
        networks: dict[str, dict[str, Any]] = {}
        lines = result.stdout.splitlines()[1:]

        for raw_line in lines:
            line = raw_line.rstrip()
            if not line.strip():
                continue

            in_use = line.rstrip().endswith("*")
            working_line = line[:-1].rstrip() if in_use else line
            parts = [
                part for part in __import__("re").split(r"\s\s+", working_line) if part
            ]
            if not parts:
                continue

            security = parts[-1]
            ssid = " ".join(parts[:-1]).strip()
            if not ssid or ssid == "--":
                continue

            network = {
                "ssid": ssid,
                "encryptionType": self._parse_encryption_type(security),
                "isConnected": in_use,
                "isKnown": ssid in saved_ssids,
            }

            existing = networks.get(ssid)
            if existing is None:
                networks[ssid] = network
            elif network["isConnected"] and not existing["isConnected"]:
                networks[ssid] = network
            elif (
                network["isKnown"]
                and not existing["isConnected"]
                and not existing["isKnown"]
            ):
                networks[ssid] = network

        return sorted(
            networks.values(),
            key=lambda n: (
                0 if n["isConnected"] else 1,
                0 if n["isKnown"] else 1,
                n["ssid"].lower(),
            ),
        )

    def connect(
        self,
        ssid: str,
        encryption_type: str,
        credentials: dict[str, Any],
    ) -> None:
        if self._lacks_secrets(encryption_type, credentials):
            # Reconnect a profile NetworkManager already knows (e.g. one added
            # with nmcli or the UI); its secrets are stored in NetworkManager.
            profile = self._find_wifi_profile(ssid)
            if profile is not None:
                self._run(["nmcli", "connection", "up", "uuid", profile["uuid"]])
                return

        self._delete_existing_connection(ssid)
        wifi_interface = self._get_wifi_interface()
        if wifi_interface is None:
            raise RuntimeError("No Wi-Fi interface found")

        if encryption_type == "open":
            self._run(["nmcli", "device", "wifi", "connect", ssid])
            return

        if encryption_type in {"wpa2Personal", "wpa3Personal"}:
            password = credentials.get("password") or ""
            self._run(
                ["nmcli", "device", "wifi", "connect", ssid, "password", password]
            )
            return

        if encryption_type not in {"wpa2Enterprise", "wpa3Enterprise"}:
            raise RuntimeError(f"Unsupported encryption type: {encryption_type}")

        username = credentials.get("username") or ""
        password = credentials.get("password") or ""
        if not username or not password:
            raise RuntimeError("Enterprise credentials require username and password")

        args = [
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "con-name",
            ssid,
            "ifname",
            wifi_interface,
            "ssid",
            ssid,
            "wifi-sec.key-mgmt",
            "wpa-eap",
            "802-1x.eap",
            "peap",
            "802-1x.identity",
            username,
            "802-1x.password",
            password,
        ]
        ca_cert = credentials.get("caCertificatePath")
        if ca_cert:
            args.extend(["802-1x.ca-cert", ca_cert])

        try:
            self._run(args)
            self._run(["nmcli", "connection", "up", ssid])
        except Exception:
            try:
                self._run(["nmcli", "connection", "delete", ssid])
            except Exception:
                pass
            raise

    def forget(self, ssid: str) -> None:
        self._delete_wifi_profiles(ssid)

    def get_device_info(self) -> dict[str, Any]:
        interfaces = self._network_interfaces()
        wifi_interface = self._get_wifi_interface()
        ethernet_interfaces = self._get_ethernet_interfaces()

        wifi_details = interfaces.get(wifi_interface or "", {})
        ethernet_details = next(
            (
                interfaces.get(device, {})
                for device in ethernet_interfaces
                if interfaces.get(device, {}).get("ipv4")
            ),
            interfaces.get(ethernet_interfaces[0], {}) if ethernet_interfaces else {},
        )

        ethernet_connected = self.is_ethernet_cable_connected()
        mode = self.get_network_mode()

        preferred_ip = (
            ethernet_details.get("ipv4")
            if mode == "lan_only" and ethernet_details.get("ipv4")
            else wifi_details.get("ipv4")
            or ethernet_details.get("ipv4")
            or self._get_primary_ip()
        )
        preferred_mac = (
            ethernet_details.get("mac")
            if mode == "lan_only" and ethernet_details.get("mac")
            else wifi_details.get("mac") or ethernet_details.get("mac")
        )

        return {
            "ipAddress": preferred_ip or "127.0.0.1",
            "macAddress": preferred_mac,
            "ethernetCableConnected": ethernet_connected,
            "ethernetIpAddress": ethernet_details.get("ipv4"),
            "ethernetMacAddress": ethernet_details.get("mac"),
            "ethernetConnectionName": self._ethernet_connection_name(),
            **self._ethernet_mode_details(mode, ethernet_interfaces),
            "wifiIpAddress": wifi_details.get("ipv4"),
            "wifiMacAddress": wifi_details.get("mac"),
            "connectedNetwork": self._connected_wifi_network(),
        }

    def _ethernet_mode_details(
        self, mode: str, ethernet_interfaces: list[str]
    ) -> dict[str, Any]:
        """Role and, as host, the connected devices (Ethernet Mode only)."""
        role = self._ethernet_role() if mode == "lan_only" else None
        host = role == "host"
        return {
            "ethernetRole": role,
            "ethernetConnectedDevices": (
                self._count_neighbours(ethernet_interfaces[0])
                if host and ethernet_interfaces
                else None
            ),
        }

    def get_network_mode(self) -> str:
        return self._load_state().get("network_mode", "client")

    def set_network_mode(self, mode: str) -> None:
        if mode == "access_point":
            self._disable_ethernet_ip()
            if not self.is_access_point_active():
                self.start_access_point(self._default_access_point_config())
            else:
                self._persist_network_mode("access_point")
            return

        if mode == "lan_only":
            self.enable_lan_only_mode()
            return

        self._disable_ethernet_ip()
        if self.is_access_point_active():
            self.stop_access_point()
        # Also after a hotspot stop: LAN-only mode switched the radio off.
        self._ensure_wifi_enabled()
        self._persist_network_mode("client")

    def restore_persisted_network_mode(self) -> None:
        mode = self.get_network_mode()
        if mode == "access_point":
            self._disable_ethernet_ip()
            if not self.is_access_point_active():
                self.start_access_point(self._default_access_point_config())
            else:
                self._persist_network_mode("access_point")
            return

        if mode == "lan_only":
            self.enable_lan_only_mode()
            return

        self._disable_ethernet_ip()
        self._persist_network_mode("client")

    def start_access_point(self, config: dict[str, Any]) -> dict[str, Any]:
        self.stop_access_point()
        # LAN-only mode switches the radio off; the hotspot needs it on.
        self._ensure_wifi_enabled()
        wifi_interface = self._get_wifi_interface()
        if wifi_interface is None:
            raise RuntimeError("No Wi-Fi interface found")

        channel = int(config.get("channel") or 0)
        band = config.get("band", "bandAuto")
        if channel == 0:
            channel = self.find_best_channel(band)
            config = {**config, "channel": channel}

        password = config.get("password") or ""
        encryption = config.get("encryptionType")
        if encryption == "open" or not password:
            config = {**config, "encryptionType": "open"}
            self._create_wpa2_or_open_ap(wifi_interface, config, band, None)
        elif encryption == "wpa3Personal":
            try:
                self._create_access_point_profile(wifi_interface, config, band, "sae")
            except Exception:
                # Not every Wi-Fi driver can host WPA3 (SAE); fall back to WPA2.
                self._remove_access_point_profile()
                config = {**config, "encryptionType": "wpa2Personal"}
                self._create_wpa2_or_open_ap(wifi_interface, config, band, "wpa-psk")
        else:
            self._create_wpa2_or_open_ap(wifi_interface, config, band, "wpa-psk")

        self.save_access_point_config(config)
        self._persist_network_mode("access_point")
        return config

    def _remove_access_point_profile(self) -> None:
        self._run(["nmcli", "connection", "down", AP_CONNECTION_NAME], check=False)
        self._run(["nmcli", "connection", "delete", AP_CONNECTION_NAME], check=False)

    # -- hostapd hotspot ----------------------------------------------------
    #
    # NetworkManager (1.52) always hands wpa_supplicant `WPA-PSK WPA-PSK-SHA256`
    # for a WPA2 hotspot, even with PMF disabled. The Pi's Wi-Fi firmware only
    # advertises `WPA-PSK` in its beacon, so the 4-way handshake carries an AKM
    # list that differs from the beacon and strict clients (wpa_supplicant 2.11,
    # i.e. most Linux laptops) drop the connection with reason 17, which
    # NetworkManager reports as "wrong password". hostapd lets us offer exactly
    # `WPA-PSK`, so it is used whenever it is installed.

    @property
    def _ap_dir(self) -> Path:
        return self.state_path.parent

    @property
    def _hostapd_pid_path(self) -> Path:
        return self._ap_dir / "hostapd.pid"

    @property
    def _dnsmasq_pid_path(self) -> Path:
        return self._ap_dir / "dnsmasq-ap.pid"

    def _hostapd_available(self) -> bool:
        return shutil.which("hostapd") is not None and shutil.which("dnsmasq") is not None

    @staticmethod
    def _pid_from(path: Path) -> int | None:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _process_name(pid: int) -> str:
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return ""

    def _hostapd_running(self) -> bool:
        pid = self._pid_from(self._hostapd_pid_path)
        return pid is not None and self._process_name(pid) == "hostapd"

    def _create_wpa2_or_open_ap(
        self,
        wifi_interface: str,
        config: dict[str, Any],
        band: str,
        key_mgmt: str | None,
    ) -> None:
        if self._hostapd_available():
            try:
                self._start_hostapd_ap(wifi_interface, config, band, key_mgmt)
                return
            except Exception as exc:
                print(f"hostapd hotspot failed, falling back to NetworkManager: {exc}")
                self._stop_hostapd_ap()
        self._create_access_point_profile(wifi_interface, config, band, key_mgmt)

    def _hostapd_config(
        self,
        wifi_interface: str,
        config: dict[str, Any],
        band: str,
        key_mgmt: str | None,
    ) -> str:
        ssid = str(config["ssid"])
        password = str(config.get("password") or "")
        if "\n" in ssid or "\r" in ssid or "\n" in password or "\r" in password:
            raise ValueError("SSID and password must not contain line breaks")
        if key_mgmt is not None and not 8 <= len(password) <= 63:
            raise ValueError("WPA2 password must be 8-63 characters")
        channel = int(config.get("channel") or 0)
        if channel <= 0:
            channel = self.find_best_channel(band)
        lines = [
            f"interface={wifi_interface}",
            "driver=nl80211",
            f"ssid={ssid}",
            f"hw_mode={'a' if self._band_nmcli_value(band) == 'a' else 'g'}",
            f"channel={channel}",
            "ieee80211n=1",
            "wmm_enabled=1",
            f"ignore_broadcast_ssid={1 if config.get('hidden') else 0}",
            f"max_num_sta={int(config.get('maxClients') or 8)}",
            "auth_algs=1",
        ]
        if key_mgmt is not None:
            lines += [
                "wpa=2",
                "wpa_key_mgmt=WPA-PSK",
                "rsn_pairwise=CCMP",
                "ieee80211w=0",
                f"wpa_passphrase={password}",
            ]
        return "\n".join(lines) + "\n"

    def _start_hostapd_ap(
        self,
        wifi_interface: str,
        config: dict[str, Any],
        band: str,
        key_mgmt: str | None,
    ) -> None:
        if band == "bandAuto":
            band = self.find_best_wifi_band()
        self._ap_dir.mkdir(parents=True, exist_ok=True)
        conf_path = self._ap_dir / "hostapd.conf"
        # Created 0600 up front: the file holds the Wi-Fi password.
        fd = os.open(conf_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(self._hostapd_config(wifi_interface, config, band, key_mgmt))

        self._run(["nmcli", "device", "set", wifi_interface, "managed", "no"])
        self._run(["ip", "addr", "flush", "dev", wifi_interface])
        self._run(
            ["ip", "addr", "add", f"{AP_ADDRESS}/{AP_PREFIX}", "dev", wifi_interface]
        )
        self._run(["ip", "link", "set", wifi_interface, "up"])
        hostname = socket.gethostname().split(".")[0]
        self._run(["hostapd", "-B", "-P", str(self._hostapd_pid_path), str(conf_path)])
        self._run(
            [
                "dnsmasq",
                f"--interface={wifi_interface}",
                "--bind-interfaces",
                "--except-interface=lo",
                f"--listen-address={AP_ADDRESS}",
                f"--dhcp-range={AP_DHCP_RANGE}",
                f"--dhcp-option=3,{AP_ADDRESS}",
                f"--dhcp-option=6,{AP_ADDRESS}",
                "--no-resolv",
                "--no-hosts",
                # Clients use this dnsmasq as their DNS server but it has no
                # upstream. Answer the robot's own name directly and refuse to
                # forward `.local`, so a lookup of <hostname>.local is instant
                # instead of waiting for an upstream that does not exist.
                f"--host-record={hostname}.local,{AP_ADDRESS}",
                f"--host-record={hostname},{AP_ADDRESS}",
                "--local=/local/",
                "--conf-file=/dev/null",
                f"--pid-file={self._dnsmasq_pid_path}",
            ]
        )

    def refresh_hotspot_hostname(self) -> bool:
        """Restart a running hostapd hotspot so it serves the current hostname.

        dnsmasq gets the robot's name when it starts, so a hostname change
        needs a restart. Returns True if a hotspot was restarted.
        """
        if not self._hostapd_running():
            return False
        config = self.get_access_point_config()
        if not config:
            return False
        self.start_access_point(config)
        return True

    def _stop_hostapd_ap(self) -> None:
        """Stop the hostapd hotspot, if one was started. No-op otherwise."""
        pids = {
            "hostapd": self._pid_from(self._hostapd_pid_path),
            "dnsmasq": self._pid_from(self._dnsmasq_pid_path),
        }
        if not any(pids.values()):
            return
        for name, pid in pids.items():
            if pid is not None and self._process_name(pid) == name:
                self._run(["kill", str(pid)], check=False)
        for path in (self._hostapd_pid_path, self._dnsmasq_pid_path):
            path.unlink(missing_ok=True)
        wifi_interface = self._get_wifi_interface()
        if wifi_interface:
            self._run(["ip", "addr", "flush", "dev", wifi_interface], check=False)
            self._run(
                ["nmcli", "device", "set", wifi_interface, "managed", "yes"],
                check=False,
            )

    def _create_access_point_profile(
        self,
        wifi_interface: str,
        config: dict[str, Any],
        band: str,
        key_mgmt: str | None,
    ) -> None:
        """Create and start the hotspot profile.

        Not `nmcli device wifi hotspot`: its profile leaves protected management
        frames (PMF) at the default, which the Pi's Wi-Fi driver rejects ("key
        setting validation failed"), so the hotspot never came up. PMF is
        disabled for WPA2 and required for WPA3.

        `maxClients` cannot be applied: NetworkManager has no such option.
        """
        if band == "bandAuto":
            band = self.find_best_wifi_band()
        channel = int(config.get("channel") or 0)

        args = [
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            wifi_interface,
            "con-name",
            AP_CONNECTION_NAME,
            "autoconnect",
            "no",
            "ssid",
            config["ssid"],
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.band",
            self._band_nmcli_value(band),
            "ipv4.method",
            "shared",
            "ipv6.method",
            "ignore",
        ]
        if channel > 0:
            args.extend(["802-11-wireless.channel", str(channel)])
        if config.get("hidden"):
            args.extend(["802-11-wireless.hidden", "yes"])
        if key_mgmt is not None:
            args.extend(
                [
                    "wifi-sec.key-mgmt",
                    key_mgmt,
                    "wifi-sec.psk",
                    config["password"],
                    "wifi-sec.proto",
                    "rsn",
                    "wifi-sec.pairwise",
                    "ccmp",
                    "wifi-sec.group",
                    "ccmp",
                    "wifi-sec.pmf",
                    "required" if key_mgmt == "sae" else "disable",
                ]
            )

        try:
            self._run(args)
            self._run(["nmcli", "connection", "up", AP_CONNECTION_NAME])
        except Exception:
            self._remove_access_point_profile()
            raise

    def stop_access_point(self) -> None:
        self._stop_hostapd_ap()
        profiles = {p["name"]: p for p in self._wifi_profiles()}
        for connection_name in self._access_point_connection_names():
            profile = profiles.get(connection_name)
            if (
                connection_name != AP_CONNECTION_NAME
                and profile is not None
                and profile["mode"] != "ap"
            ):
                # A normal Wi-Fi profile that shares the hotspot's SSID.
                continue
            self._run(["nmcli", "connection", "down", connection_name], check=False)
            self._run(["nmcli", "connection", "delete", connection_name], check=False)
        self._reset_wifi_interface()

    def is_access_point_active(self) -> bool:
        if self._hostapd_running():
            return True
        result = self._run(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
            check=False,
        )
        if result.returncode == 0:
            active_names = self._access_point_connection_names()

            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split(":")
                name = parts[0].strip() if parts else ""
                connection_type = parts[1].strip().lower() if len(parts) > 1 else ""
                if name in active_names and (
                    "wifi" in connection_type
                    or "wireless" in connection_type
                    or "802-11" in connection_type
                ):
                    return True

        device_result = self._run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            check=False,
        )
        if device_result.returncode != 0:
            return False

        active_names = self._access_point_connection_names()

        for line in device_result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(":")
            device_type = parts[1].strip().lower() if len(parts) > 1 else ""
            state = parts[2].strip().lower() if len(parts) > 2 else ""
            connection = parts[3].strip() if len(parts) > 3 else ""
            if device_type != "wifi":
                continue
            if state == "connected" and connection in active_names:
                return True

        return False

    def count_access_point_clients(self) -> int:
        """Devices currently connected to the hotspot.

        Counted from the neighbour table of the hotspot interface (`iw` is not
        installed on the robot). A device that just left can still be counted
        for a short while until its entry expires.
        """
        wifi_interface = self._get_wifi_interface()
        if wifi_interface is None:
            return 0
        return self._count_neighbours(wifi_interface)

    def _count_neighbours(self, device: str) -> int:
        result = self._run(["ip", "-j", "neigh", "show", "dev", device], check=False)
        if result.returncode != 0:
            return 0
        try:
            entries = json.loads(result.stdout or "[]")
        except ValueError:
            return 0
        live_states = {"REACHABLE", "DELAY", "PROBE", "STALE"}
        macs = {
            entry["lladdr"].lower()
            for entry in entries
            if entry.get("lladdr") and live_states & set(entry.get("state", []))
        }
        return len(macs)

    def get_access_point_config(self) -> dict[str, Any] | None:
        return self._load_state().get("access_point_config")

    def save_access_point_config(self, config: dict[str, Any]) -> None:
        state = self._load_state()
        state["access_point_config"] = config
        self._save_state(state)

    def _default_access_point_config(self) -> dict[str, Any]:
        saved_config = self.get_access_point_config()
        if saved_config is not None:
            return saved_config
        config = {
            # "Raccoon-Robot-" + 3 random digits, so several robots in one room
            # can be told apart.
            "ssid": f"Raccoon-Robot-{secrets.randbelow(1000):03d}",
            # "raccoon" + 3 random digits. Saved right away so the robot keeps
            # the same password instead of drawing a new one on every start.
            "password": f"raccoon{secrets.randbelow(1000):03d}",
            "band": self.find_best_wifi_band(),
            "channel": 0,
            "encryptionType": "wpa2Personal",
            "hidden": False,
            "maxClients": 8,
        }
        self.save_access_point_config(config)
        return config

    def _access_point_connection_names(self) -> list[str]:
        names: list[str] = []
        saved_config = self.get_access_point_config()
        if saved_config and saved_config.get("ssid"):
            names.append(str(saved_config["ssid"]))
        if AP_CONNECTION_NAME not in names:
            names.append(AP_CONNECTION_NAME)
        return names

    def find_best_wifi_band(self) -> str:
        result = self._run(["iw", "phy", "phy0", "info"], check=False)
        if result.returncode == 0 and "5180" in result.stdout:
            return "band5GHz"
        return "band2_4GHz"

    def find_best_channel(self, band: str) -> int:
        scan = self.scan_access_point_channels(band)
        return int(scan["recommendedChannel"])

    def scan_access_point_channels(self, band: str) -> dict[str, Any]:
        if self.is_access_point_active():
            config = self.get_access_point_config()
            if config is None:
                raise RuntimeError(
                    "Hotspot is active but no access point configuration is available to restore after scanning."
                )

            self.stop_access_point()
            try:
                return self.scan_access_point_channels(band)
            finally:
                self.start_access_point(config)

        recommendation_band = self.find_best_wifi_band() if band == "bandAuto" else band
        channels = self._band_channels(band)
        channel_data = {
            channel: {
                "channel": channel,
                "networkCount": 0,
                "ssids": [],
            }
            for channel in channels
        }

        wifi_interface = self._get_wifi_interface()
        if wifi_interface is None:
            return self._build_channel_scan_response(
                band,
                channel_data,
                recommended_channels=self._recommended_channels(recommendation_band),
            )

        self._ensure_wifi_enabled()
        result = self._run(["iwlist", wifi_interface, "scan"], check=False)
        if result.returncode != 0:
            return self._build_channel_scan_response(
                band,
                channel_data,
                recommended_channels=self._recommended_channels(recommendation_band),
            )

        networks = self._parse_wifi_scan_networks(result.stdout, channels)
        for network in networks:
            for affected_channel in network["affectedChannels"]:
                if affected_channel not in channel_data:
                    continue
                entry = channel_data[affected_channel]
                entry["networkCount"] += 1
                if network["ssid"] not in entry["ssids"]:
                    entry["ssids"].append(network["ssid"])

        return self._build_channel_scan_response(
            band,
            channel_data,
            recommended_channels=self._recommended_channels(recommendation_band),
            networks=networks,
        )

    def get_saved_networks(self) -> list[dict[str, Any]]:
        """Saved Wi-Fi networks, read live from NetworkManager's profiles."""
        networks: dict[str, dict[str, Any]] = {}
        for profile in self._wifi_profiles():
            if profile["mode"] == "ap" or not profile["ssid"]:
                continue
            ssid = profile["ssid"]
            encryption = self._encryption_from_key_mgmt(profile["key_mgmt"])
            if encryption.endswith("Enterprise"):
                credentials: dict[str, Any] = {
                    "credentialsType": "enterprise",
                    "username": "",
                    "password": "",
                }
            else:
                credentials = {"credentialsType": "personal", "password": ""}
            network = {
                "ssid": ssid,
                "encryptionType": encryption,
                "credentials": credentials,
                "lastConnected": datetime.fromtimestamp(
                    profile["timestamp"], tz=timezone.utc
                ).isoformat(),
                "autoConnect": profile["autoconnect"],
            }
            existing = networks.get(ssid)
            if existing is None or network["lastConnected"] > existing["lastConnected"]:
                networks[ssid] = network
        return list(networks.values())

    def save_network(self, network: dict[str, Any]) -> None:
        """No-op: NetworkManager owns saved networks; connecting creates them."""

    def remove_saved_network(self, ssid: str) -> None:
        self._delete_wifi_profiles(ssid)

    def get_saved_network(self, ssid: str) -> dict[str, Any] | None:
        normalized = self._normalize_ssid(ssid)
        for network in self.get_saved_networks():
            if self._normalize_ssid(network.get("ssid", "")) == normalized:
                return network
        return None

    def get_saved_password(self, ssid: str) -> str | None:
        """The stored Wi-Fi password (PSK) of a saved network.

        Read on demand only: the saved-network list never carries passwords.
        None if there is no such network; "" if NetworkManager does not hold
        the secret (enterprise or open networks, or secrets kept by the user).
        """
        profile = self._find_wifi_profile(ssid)
        if profile is None:
            return None
        result = self._run(
            [
                "nmcli",
                "-s",
                "-g",
                "802-11-wireless-security.psk",
                "connection",
                "show",
                "uuid",
                profile["uuid"],
            ],
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def enable_lan_only_mode(self) -> str:
        """Switch to Ethernet Mode. Returns "client" or "host".

        "client": a DHCP server (router, switch, laptop sharing its network)
        answered and gave the robot an address. "host": nobody answered within
        ETHERNET_DHCP_TIMEOUT seconds (a laptop plugged in directly), so the
        robot hosts the network itself: same address and DHCP range as the
        hotspot.
        """
        if not self.is_ethernet_cable_connected():
            raise RuntimeError(
                "No ethernet cable detected. Please connect an ethernet cable before enabling Ethernet Mode."
            )
        # Already there (the UI enables twice: the LAN call and the mode call).
        if self.get_network_mode() == "lan_only" and self._ethernet_has_address(1):
            role = self._ethernet_role()
            if role:
                return role
        # Ethernet first: if it cannot get an address, Wi-Fi stays as it is
        # and the robot does not become unreachable.
        role = self._activate_first_ethernet_connection()
        if role is None:
            self._disable_ethernet_ip()
            raise RuntimeError(
                "Could not bring up the ethernet connection. Wi-Fi was left unchanged."
            )
        # End the hotspot (hostapd, dnsmasq, or the NetworkManager profile)
        # before the radio goes off, so nothing keeps claiming to be hosted.
        if self.is_access_point_active():
            self.stop_access_point()
        self._run(["nmcli", "radio", "wifi", "off"])
        self._persist_network_mode("lan_only")
        return role

    def disable_lan_only_mode(self) -> None:
        self.set_network_mode("client")

    def is_lan_only_mode_active(self) -> bool:
        if self.get_network_mode() != "lan_only":
            return False
        result = self._run(["nmcli", "radio", "wifi"], check=False)
        return result.returncode == 0 and "disabled" in result.stdout.lower()

    def lan_only_failsafe_tick(self, max_missing_ticks: int = 3) -> bool:
        """Turn Wi-Fi back on if LAN-only mode lost its cable.

        Without it the robot would be unreachable. Returns True if it acted.
        """
        if self.get_network_mode() != "lan_only":
            self._lan_cable_missing_ticks = 0
            return False
        if self.is_ethernet_cable_connected():
            self._lan_cable_missing_ticks = 0
            return False
        self._lan_cable_missing_ticks += 1
        if self._lan_cable_missing_ticks < max_missing_ticks:
            return False
        self._lan_cable_missing_ticks = 0
        self.disable_lan_only_mode()
        return True

    def run_network_watchdog(
        self,
        stop: threading.Event,
        retry_restore: bool = False,
        interval: float = 10.0,
        max_restore_attempts: int = 6,
    ) -> None:
        """Background loop: retry a failed boot restore, then guard LAN-only."""
        attempts = 0
        while not stop.wait(interval):
            try:
                if retry_restore:
                    attempts += 1
                    try:
                        self.restore_persisted_network_mode()
                        retry_restore = False
                    except Exception as exc:
                        print(f"Retrying network restore ({attempts}): {exc}")
                        if attempts >= max_restore_attempts:
                            retry_restore = False
                            if self.get_network_mode() == "lan_only":
                                self.disable_lan_only_mode()
                    continue
                self.lan_only_failsafe_tick()
            except Exception as exc:
                print(f"Network watchdog error: {exc}")

    def is_ethernet_cable_connected(
        self, interfaces: dict[str, dict[str, Any]] | None = None
    ) -> bool:
        """True while a cable is plugged into a wired port (link signal).

        Only the carrier counts. An address on the port or NetworkManager
        trying to connect says nothing about a cable: both lingered after a
        mode change and made the robot think a cable had just been plugged in.
        """
        interfaces = interfaces if interfaces is not None else self._network_interfaces()
        for device in self._get_ethernet_interfaces():
            carrier = interfaces.get(device, {}).get("carrier")
            if carrier == "1":
                return True
            if carrier is None:
                # A port that is switched off cannot report a carrier, so a
                # plugged cable would never be noticed. Switch it on; the next
                # check sees the link.
                self._run(["ip", "link", "set", device, "up"], check=False)
        return False

    def lan_status(self) -> dict[str, Any]:
        """Light status for polling (the UI asks every couple of seconds)."""
        interfaces = self._network_interfaces()
        devices = self._get_ethernet_interfaces()
        details = next(
            (interfaces[d] for d in devices if interfaces.get(d, {}).get("ipv4")),
            interfaces.get(devices[0], {}) if devices else {},
        )
        return {
            "isActive": self.is_lan_only_mode_active(),
            "isCableConnected": self.is_ethernet_cable_connected(interfaces),
            "ipAddress": details.get("ipv4") or self._get_primary_ip(),
            "macAddress": details.get("mac"),
        }

    def _ethernet_profile_names(self) -> list[str]:
        result = self._run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], check=False
        )
        names: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.rsplit(":", 1)
            if len(parts) == 2 and parts[1] == "802-3-ethernet":
                names.append(parts[0])
        return names

    def _activate_first_ethernet_connection(self) -> str | None:
        """Bring up the wired connection. Returns "client", "host" or None.

        First DHCP, with a short timeout. Only if nobody answers does the robot
        become the DHCP server itself (NetworkManager "shared", same address and
        range as the hotspot).
        """
        name = self._ensure_ethernet_profile()
        self._run(
            [
                "nmcli", "connection", "modify", name,
                "ipv4.method", "auto",
                "ipv4.addresses", "",
                "ipv4.gateway", "",
                "ipv4.dhcp-timeout", str(ETHERNET_DHCP_TIMEOUT),
                "ipv4.may-fail", "no",
                "ipv6.method", "ignore",
            ],
            check=False,
        )
        # -w: wait a little longer than the DHCP timeout for the result.
        up = self._run(
            ["nmcli", "-w", str(ETHERNET_DHCP_TIMEOUT + 5), "connection", "up", name],
            check=False,
        )
        if up.returncode == 0 and self._ethernet_has_address():
            return "client"

        # No DHCP server on this cable: host the network.
        self._run(
            [
                "nmcli", "connection", "modify", name,
                "ipv4.method", "shared",
                "ipv4.addresses", f"{AP_ADDRESS}/{AP_PREFIX}",
                "ipv4.gateway", "",
                "ipv4.may-fail", "no",
            ],
            check=False,
        )
        self._run(["nmcli", "connection", "up", name], check=False)
        return "host" if self._ethernet_has_address() else None

    def _ensure_ethernet_profile(self) -> str:
        profiles = self._ethernet_profile_names()
        if profiles:
            return profiles[0]
        name = "Wired connection 1"
        interfaces = self._get_ethernet_interfaces()
        self._run(
            [
                "nmcli", "connection", "add", "type", "ethernet",
                "con-name", name,
                *(["ifname", interfaces[0]] if interfaces else []),
            ],
            check=False,
        )
        return name

    def _ethernet_role(self) -> str | None:
        """"host" if the robot serves DHCP on the wired port, "client" if it
        got its address from the network, None if neither (or no profile)."""
        name = self._ethernet_connection_name()
        names = [name] if name else self._ethernet_profile_names()
        for profile in names:
            result = self._run(
                ["nmcli", "-g", "ipv4.method", "connection", "show", profile],
                check=False,
            )
            method = result.stdout.strip()
            if method == "shared":
                return "host"
            if method == "auto":
                return "client"
        return None

    def _disable_ethernet_ip(self) -> None:
        """Keep the wired port without an address outside LAN-only mode.

        The link stays up (the cable is still detected), but nothing is routed
        over it: no DHCP, no fixed address. LAN traffic is for LAN-only mode.
        """
        for name in self._ethernet_profile_names():
            self._run(
                [
                    "nmcli", "connection", "modify", name,
                    "ipv4.method", "disabled",
                    "ipv4.addresses", "",
                    "ipv4.gateway", "",
                    "ipv6.method", "ignore",
                ],
                check=False,
            )
            # Applies the change to a running connection; no-op otherwise.
            self._run(["nmcli", "connection", "up", name], check=False)
        # Without an address the port must stay switched on, or a plugged
        # cable cannot be noticed.
        for device in self._get_ethernet_interfaces():
            self._run(["ip", "link", "set", device, "up"], check=False)

    def _ethernet_has_address(self, attempts: int = 5) -> bool:
        """True once a wired port has an IPv4 address."""
        for attempt in range(attempts):
            interfaces = self._network_interfaces()
            if any(
                interfaces.get(device, {}).get("ipv4")
                for device in self._get_ethernet_interfaces()
            ):
                return True
            if attempt < attempts - 1:
                time.sleep(1)
        return False

    def _ethernet_connection_name(self) -> str | None:
        result = self._run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            check=False,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) == 4 and parts[1] == "ethernet" and parts[2] == "connected":
                return parts[3].strip() or None
        return None

    def _connected_wifi_network(self) -> dict[str, Any] | None:
        result = self._run(
            ["nmcli", "-t", "-f", "SSID,SECURITY,IN-USE", "dev", "wifi"], check=False
        )
        for line in result.stdout.splitlines():
            if "*" not in line:
                continue
            parts = line.split(":")
            ssid = parts[0].strip() if parts else ""
            security = parts[1].strip() if len(parts) > 1 else ""
            if not ssid:
                continue
            return {
                "ssid": ssid,
                "encryptionType": self._parse_encryption_type(security),
                "isConnected": True,
                "isKnown": self.get_saved_network(ssid) is not None,
            }
        return None

    def _network_interfaces(self) -> dict[str, dict[str, Any]]:
        result = self._run(["ip", "-j", "address", "show"], check=False)
        if result.returncode != 0:
            return {}

        data = json.loads(result.stdout or "[]")
        interfaces: dict[str, dict[str, Any]] = {}
        for entry in data:
            name = entry.get("ifname")
            if not name:
                continue
            ipv4 = next(
                (
                    addr.get("local")
                    for addr in entry.get("addr_info", [])
                    if addr.get("family") == "inet"
                ),
                None,
            )
            interfaces[name] = {
                "mac": entry.get("address"),
                "ipv4": ipv4,
                "operstate": entry.get("operstate"),
                "carrier": self._read_carrier(name),
            }
        return interfaces

    def _read_carrier(self, device: str) -> str | None:
        try:
            return Path(f"/sys/class/net/{device}/carrier").read_text().strip()
        except Exception:
            return None

    def _get_primary_ip(self) -> str:
        try:
            with __import__("socket").socket(
                __import__("socket").AF_INET, __import__("socket").SOCK_DGRAM
            ) as sock:
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def _get_wifi_interface(self) -> str | None:
        result = self._run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"], check=False
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi":
                device = parts[0]
                if not device.startswith("p2p-dev-"):
                    return device
        return None

    def _get_ethernet_interfaces(self) -> list[str]:
        result = self._run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"], check=False
        )
        devices: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "ethernet":
                devices.append(parts[0])
        return devices

    def _nmcli_device_state(self, device: str) -> str:
        result = self._run(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"], check=False
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == device:
                return parts[1].strip().lower()
        return ""

    def _ensure_wifi_enabled(self) -> None:
        result = self._run(["nmcli", "radio", "wifi"], check=False)
        if result.returncode == 0 and "disabled" in result.stdout.lower():
            self._run(["nmcli", "radio", "wifi", "on"])

        wifi_interface = self._get_wifi_interface()
        if wifi_interface:
            self._run(
                ["nmcli", "device", "set", wifi_interface, "managed", "yes"],
                check=False,
            )

    def _reset_wifi_interface(self) -> None:
        wifi_interface = self._get_wifi_interface()
        if not wifi_interface:
            return
        self._run(["ip", "link", "set", wifi_interface, "down"], check=False)
        self._run(["ip", "link", "set", wifi_interface, "up"], check=False)
        self._run(
            ["nmcli", "device", "set", wifi_interface, "managed", "yes"], check=False
        )
        self._run(["nmcli", "device", "wifi", "rescan"], check=False)

    def _delete_existing_connection(self, ssid: str) -> None:
        self._delete_wifi_profiles(ssid)

    def _wifi_profiles(self) -> list[dict[str, Any]]:
        """All Wi-Fi connection profiles known to NetworkManager."""
        listing = self._run(
            ["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"], check=False
        )
        profiles: list[dict[str, Any]] = []
        for line in listing.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            uuid, connection_type = parts[0].strip(), parts[1]
            if not uuid or (
                "wireless" not in connection_type and "wifi" not in connection_type
            ):
                continue
            show = self._run(
                [
                    "nmcli",
                    "-g",
                    "connection.id,connection.autoconnect,connection.timestamp,"
                    "802-11-wireless.ssid,802-11-wireless.mode,"
                    "802-11-wireless-security.key-mgmt",
                    "connection",
                    "show",
                    "uuid",
                    uuid,
                ],
                check=False,
            )
            if show.returncode != 0:
                continue
            values = (show.stdout.split("\n") + [""] * 6)[:6]
            name, autoconnect, timestamp, ssid, mode, key_mgmt = (
                v.strip() for v in values
            )
            try:
                timestamp_value = int(timestamp)
            except ValueError:
                timestamp_value = 0
            profiles.append(
                {
                    "uuid": uuid,
                    "name": name,
                    "ssid": ssid or name,
                    "autoconnect": autoconnect.lower() != "no",
                    "timestamp": timestamp_value,
                    "mode": mode,
                    "key_mgmt": key_mgmt,
                }
            )
        return profiles

    def _find_wifi_profile(self, ssid: str) -> dict[str, Any] | None:
        normalized = self._normalize_ssid(ssid)
        for profile in self._wifi_profiles():
            if profile["mode"] == "ap":
                continue
            if profile["ssid"] in {ssid, normalized} or profile["name"] == ssid:
                return profile
        return None

    def _delete_wifi_profiles(self, ssid: str) -> None:
        """Delete every Wi-Fi profile for this SSID from NetworkManager."""
        normalized = self._normalize_ssid(ssid)
        for profile in self._wifi_profiles():
            if profile["mode"] == "ap":
                continue  # never delete the hotspot just because the SSID matches
            if profile["ssid"] in {ssid, normalized} or profile["name"] == ssid:
                self._run(
                    ["nmcli", "connection", "delete", "uuid", profile["uuid"]],
                    check=False,
                )

    @staticmethod
    def _lacks_secrets(encryption_type: str, credentials: dict[str, Any]) -> bool:
        if encryption_type in {"wpa2Personal", "wpa3Personal"}:
            return not credentials.get("password")
        if encryption_type in {"wpa2Enterprise", "wpa3Enterprise"}:
            return not (credentials.get("username") and credentials.get("password"))
        return False

    @staticmethod
    def _encryption_from_key_mgmt(key_mgmt: str) -> str:
        if key_mgmt == "wpa-eap":
            return "wpa2Enterprise"
        if key_mgmt == "sae":
            return "wpa3Personal"
        if key_mgmt in {"", "none"}:
            return "open"
        return "wpa2Personal"

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "network_mode": "client",
                "access_point_config": None,
            }
        try:
            state = json.loads(self.state_path.read_text())
            state.pop("saved_networks", None)  # legacy: NetworkManager owns this now
            return state
        except Exception:
            return {
                "network_mode": "client",
                "access_point_config": None,
            }

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2, sort_keys=True))

    def _persist_network_mode(self, mode: str) -> None:
        state = self._load_state()
        state["network_mode"] = mode
        self._save_state(state)

    def _run(
        self, command: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        use_sudo = command[0] in {"nmcli", "ip", "iw", "iwlist", "hostapd", "dnsmasq", "kill"}
        full_command = ["sudo", *command] if use_sudo else command
        try:
            result = subprocess.run(
                full_command,
                capture_output=True,
                text=True,
                timeout=25,
            )
        except subprocess.TimeoutExpired:
            if check:
                raise RuntimeError(f"{' '.join(command)} timed out after 25s") from None
            return subprocess.CompletedProcess(
                full_command, 124, stdout="", stderr="timed out"
            )
        if check and result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"{' '.join(command)} failed: {stderr}")
        return result

    def _normalize_ssid(self, ssid: str) -> str:
        return ssid.split(":")[0].strip()

    def _parse_encryption_type(self, security: str) -> str:
        if "WPA3" in security:
            return "wpa3Enterprise" if "EAP" in security else "wpa3Personal"
        if "WPA2" in security:
            return "wpa2Enterprise" if "EAP" in security else "wpa2Personal"
        return "open"

    def _band_nmcli_value(self, band: str) -> str:
        return {
            "band2_4GHz": "bg",
            "band5GHz": "a",
            "bandAuto": "a",
        }.get(band, "a")

    def _band_channels(self, band: str) -> list[int]:
        if band == "band2_4GHz":
            return list(range(1, 14))
        if band == "bandAuto":
            return self._band_channels("band2_4GHz") + self._band_channels("band5GHz")
        return [
            36,
            40,
            44,
            48,
            52,
            56,
            60,
            64,
            100,
            104,
            108,
            112,
            116,
            120,
            124,
            128,
            132,
            136,
            140,
            144,
            149,
            153,
            157,
            161,
            165,
        ]

    def _recommended_channels(self, band: str) -> list[int]:
        if band == "band2_4GHz":
            return [1, 6, 11]
        return self._band_channels(band)

    def _parse_wifi_scan_networks(
        self,
        scan_output: str,
        available_channels: list[int],
    ) -> list[dict[str, Any]]:
        cells = re.split(r"(?=^\s*Cell\s+\d+\s+-)", scan_output, flags=re.MULTILINE)
        valid_channels = set(available_channels)
        networks: list[dict[str, Any]] = []

        for cell in cells:
            if "Cell " not in cell:
                continue

            channel = self._extract_scan_channel(cell)
            if channel is None or channel not in valid_channels:
                continue

            ssid = self._extract_scan_ssid(cell) or "<Hidden>"
            frequency_mhz = self._extract_frequency_mhz(
                cell
            ) or self._channel_to_frequency_mhz(channel)
            signal_dbm = self._extract_signal_dbm(cell)
            quality_percent = self._extract_quality_percent(cell)
            secondary_offset = self._extract_secondary_channel_offset(cell)
            width_mhz = self._extract_channel_width_mhz(cell, channel, secondary_offset)
            center_frequency_mhz = self._infer_center_frequency_mhz(
                channel=channel,
                frequency_mhz=frequency_mhz,
                width_mhz=width_mhz,
                secondary_offset=secondary_offset,
            )
            affected_channels = [
                candidate
                for candidate in available_channels
                if self._channels_overlap(
                    center_frequency_mhz=center_frequency_mhz,
                    width_mhz=width_mhz,
                    candidate_channel=candidate,
                )
            ]

            networks.append(
                {
                    "ssid": ssid,
                    "channel": channel,
                    "frequencyMHz": frequency_mhz,
                    "centerFrequencyMHz": center_frequency_mhz,
                    "channelWidthMHz": width_mhz,
                    "signalDbm": signal_dbm,
                    "qualityPercent": quality_percent,
                    "overlapStartChannel": (
                        affected_channels[0] if affected_channels else channel
                    ),
                    "overlapEndChannel": (
                        affected_channels[-1] if affected_channels else channel
                    ),
                    "affectedChannels": affected_channels,
                }
            )

        networks.sort(
            key=lambda item: (
                item["channel"],
                -(item["signalDbm"] if item["signalDbm"] is not None else -120),
                item["ssid"].lower(),
            )
        )
        return networks

    def _extract_scan_channel(self, cell: str) -> int | None:
        channel_match = re.search(r"Channel:(\d+)", cell) or re.search(
            r"\(Channel\s+(\d+)\)", cell
        )
        if not channel_match:
            return None
        return int(channel_match.group(1))

    def _extract_scan_ssid(self, cell: str) -> str:
        ssid_match = re.search(r'ESSID:"(.*)"', cell)
        if not ssid_match:
            return ""
        return ssid_match.group(1).strip()

    def _extract_signal_dbm(self, cell: str) -> int | None:
        signal_match = re.search(r"Signal level=(-?\d+)\s*dBm", cell)
        if signal_match:
            return int(signal_match.group(1))
        return None

    def _extract_frequency_mhz(self, cell: str) -> int | None:
        freq_match = re.search(r"Frequency:([0-9.]+)\s*GHz", cell)
        if freq_match:
            return round(float(freq_match.group(1)) * 1000)

        freq_match = re.search(r"Frequency:([0-9.]+)\s*MHz", cell)
        if freq_match:
            return round(float(freq_match.group(1)))
        return None

    def _extract_quality_percent(self, cell: str) -> int | None:
        quality_match = re.search(r"Quality=(\d+)/(\d+)", cell)
        if not quality_match:
            return None
        numerator = int(quality_match.group(1))
        denominator = int(quality_match.group(2))
        if denominator <= 0:
            return None
        return round((numerator / denominator) * 100)

    def _extract_secondary_channel_offset(self, cell: str) -> str | None:
        offset_match = re.search(
            r"secondary channel offset:\s*(above|below)",
            cell,
            flags=re.IGNORECASE,
        )
        if not offset_match:
            return None
        return offset_match.group(1).lower()

    def _extract_channel_width_mhz(
        self,
        cell: str,
        channel: int,
        secondary_offset: str | None,
    ) -> int:
        width_match = re.search(
            r"channel width:\s*(\d+)\s*MHz",
            cell,
            flags=re.IGNORECASE,
        )
        if width_match:
            return int(width_match.group(1))

        if re.search(r"\b160\s*MHz\b", cell, flags=re.IGNORECASE):
            return 160
        if re.search(r"\b80\s*MHz\b", cell, flags=re.IGNORECASE):
            return 80
        if re.search(r"\b40\s*MHz\b", cell, flags=re.IGNORECASE):
            return 40
        if "HT40" in cell or secondary_offset is not None:
            return 40
        if channel > 14 and (
            "VHT Capabilities" in cell
            or "VHT Operation" in cell
            or "HE Capabilities" in cell
        ):
            return 80
        return 20

    def _infer_center_frequency_mhz(
        self,
        *,
        channel: int,
        frequency_mhz: int | None,
        width_mhz: int,
        secondary_offset: str | None,
    ) -> int:
        base_frequency = frequency_mhz or self._channel_to_frequency_mhz(channel) or 0

        if width_mhz <= 20:
            return base_frequency

        if width_mhz == 40:
            if secondary_offset == "above":
                return base_frequency + 10
            if secondary_offset == "below":
                return base_frequency - 10
            if channel <= 14:
                return base_frequency + 10 if channel <= 7 else base_frequency - 10
            block_center = self._channel_block_center(channel, block_size=2)
            return self._channel_to_frequency_mhz(block_center) or base_frequency

        if width_mhz == 80:
            block_center = self._channel_block_center(channel, block_size=4)
            return self._channel_to_frequency_mhz(block_center) or base_frequency

        if width_mhz >= 160:
            block_center = self._channel_block_center(channel, block_size=8)
            return self._channel_to_frequency_mhz(block_center) or base_frequency

        return base_frequency

    def _channel_block_center(self, channel: int, *, block_size: int) -> int:
        if channel <= 14:
            return channel

        channel_blocks = [
            [36, 40, 44, 48],
            [52, 56, 60, 64],
            [100, 104, 108, 112],
            [116, 120, 124, 128],
            [132, 136, 140, 144],
            [149, 153, 157, 161],
        ]

        if block_size == 2:
            for block in channel_blocks:
                pairs = [block[i : i + 2] for i in range(0, len(block), 2)]
                for pair in pairs:
                    if channel in pair:
                        return round(sum(pair) / len(pair))
            return channel

        for block in channel_blocks:
            if channel in block and block_size == 4:
                return round(sum(block) / len(block))

        if block_size == 8:
            extended_blocks = [
                [36, 40, 44, 48, 52, 56, 60, 64],
                [100, 104, 108, 112, 116, 120, 124, 128],
            ]
            for block in extended_blocks:
                if channel in block:
                    return round(sum(block) / len(block))

        return channel

    def _channel_to_frequency_mhz(self, channel: int) -> int | None:
        if 1 <= channel <= 13:
            return 2407 + channel * 5
        if channel == 14:
            return 2484
        if channel >= 36:
            return 5000 + channel * 5
        return None

    def _channels_overlap(
        self,
        *,
        center_frequency_mhz: int,
        width_mhz: int,
        candidate_channel: int,
    ) -> bool:
        candidate_frequency = self._channel_to_frequency_mhz(candidate_channel)
        if candidate_frequency is None:
            return False
        threshold = (width_mhz / 2) + 10
        return abs(candidate_frequency - center_frequency_mhz) <= threshold

    def _build_channel_scan_response(
        self,
        band: str,
        channel_data: dict[int, dict[str, Any]],
        *,
        recommended_channels: list[int] | None = None,
        networks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        channels = [channel_data[channel] for channel in sorted(channel_data)]
        candidate_channels = recommended_channels or list(channel_data.keys())
        candidate_channels = [
            channel for channel in candidate_channels if channel in channel_data
        ]
        recommended_channel = min(
            candidate_channels,
            key=lambda channel: (
                channel_data[channel]["networkCount"],
                channel,
            ),
        )

        for item in channels:
            item["isRecommended"] = item["channel"] == recommended_channel

        return {
            "band": band,
            "recommendedChannel": recommended_channel,
            "detectedNetworks": len(networks or []),
            "channels": channels,
            "networks": networks or [],
        }
