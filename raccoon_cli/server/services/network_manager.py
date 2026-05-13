"""Server-side network management for Wi-Fi, hotspot, and LAN status."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


class NetworkManager:
    """Thin wrapper around nmcli/ip with persisted network UI state."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or (
            Path.home() / ".raccoon" / "network_state.json"
        )

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
        normalized = self._normalize_ssid(ssid)
        try:
            self._run(["nmcli", "connection", "delete", ssid])
        except Exception:
            result = self._run(
                ["nmcli", "-t", "-f", "UUID", "connection", "show"], check=False
            )
            uuids = [
                line.strip() for line in result.stdout.splitlines() if line.strip()
            ]
            for uuid in uuids:
                show_result = self._run(
                    [
                        "nmcli",
                        "-t",
                        "-f",
                        "802-11-wireless.ssid",
                        "connection",
                        "show",
                        uuid,
                    ],
                    check=False,
                )
                if show_result.stdout.strip() == normalized:
                    self._run(
                        ["nmcli", "connection", "delete", "uuid", uuid], check=False
                    )
                    break
        self.remove_saved_network(normalized)

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
            "wifiIpAddress": wifi_details.get("ipv4"),
            "wifiMacAddress": wifi_details.get("mac"),
            "connectedNetwork": self._connected_wifi_network(),
        }

    def get_network_mode(self) -> str:
        return self._load_state().get("network_mode", "client")

    def set_network_mode(self, mode: str) -> None:
        if mode == "access_point":
            if not self.is_access_point_active():
                self.start_access_point(self._default_access_point_config())
            else:
                self._persist_network_mode("access_point")
            return

        if mode == "lan_only":
            self.enable_lan_only_mode()
            return

        if self.is_access_point_active():
            self.stop_access_point()
        else:
            self._ensure_wifi_enabled()
        self._persist_network_mode("client")

    def restore_persisted_network_mode(self) -> None:
        mode = self.get_network_mode()
        if mode == "access_point":
            if not self.is_access_point_active():
                self.start_access_point(self._default_access_point_config())
            else:
                self._persist_network_mode("access_point")
            return

        if mode == "lan_only":
            self.enable_lan_only_mode()
            return

        self._persist_network_mode("client")

    def start_access_point(self, config: dict[str, Any]) -> dict[str, Any]:
        self.stop_access_point()
        wifi_interface = self._get_wifi_interface()
        if wifi_interface is None:
            raise RuntimeError("No Wi-Fi interface found")

        channel = int(config.get("channel") or 0)
        band = config.get("band", "bandAuto")
        if channel == 0:
            channel = self.find_best_channel(band)
            config = {**config, "channel": channel}

        args = [
            "nmcli",
            "device",
            "wifi",
            "hotspot",
            "ifname",
            wifi_interface,
            "con-name",
            "STP-Velox-AP",
            "ssid",
            config["ssid"],
        ]
        password = config.get("password") or ""
        if password:
            args.extend(["password", password])

        band_value = self._band_nmcli_value(band)
        if channel > 0 and band != "bandAuto":
            args.extend(["band", band_value, "channel", str(channel)])
        elif band != "bandAuto":
            args.extend(["band", band_value])

        self._run(args)
        self.save_access_point_config(config)
        self._persist_network_mode("access_point")
        return config

    def stop_access_point(self) -> None:
        for connection_name in self._access_point_connection_names():
            self._run(["nmcli", "connection", "down", connection_name], check=False)
            self._run(["nmcli", "connection", "delete", connection_name], check=False)
        self._reset_wifi_interface()

    def is_access_point_active(self) -> bool:
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
        return {
            "ssid": "STP-Velox-Robot",
            "password": "Robot123!",
            "band": self.find_best_wifi_band(),
            "channel": 0,
            "encryptionType": "wpa3Personal",
            "hidden": False,
            "maxClients": 8,
        }

    def _access_point_connection_names(self) -> list[str]:
        names: list[str] = []
        saved_config = self.get_access_point_config()
        if saved_config and saved_config.get("ssid"):
            names.append(str(saved_config["ssid"]))
        if "STP-Velox-AP" not in names:
            names.append("STP-Velox-AP")
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
        return list(self._load_state().get("saved_networks", []))

    def save_network(self, network: dict[str, Any]) -> None:
        state = self._load_state()
        networks = [
            item
            for item in state.get("saved_networks", [])
            if item.get("ssid") != network.get("ssid")
        ]
        networks.append(network)
        state["saved_networks"] = networks
        self._save_state(state)

    def remove_saved_network(self, ssid: str) -> None:
        normalized = self._normalize_ssid(ssid)
        state = self._load_state()
        state["saved_networks"] = [
            item
            for item in state.get("saved_networks", [])
            if self._normalize_ssid(item.get("ssid", "")) != normalized
        ]
        self._save_state(state)

    def get_saved_network(self, ssid: str) -> dict[str, Any] | None:
        normalized = self._normalize_ssid(ssid)
        for network in self.get_saved_networks():
            if self._normalize_ssid(network.get("ssid", "")) == normalized:
                return network
        return None

    def enable_lan_only_mode(self) -> None:
        if not self.is_ethernet_cable_connected():
            raise RuntimeError(
                "No ethernet cable detected. Please connect a LAN cable before enabling LAN only mode."
            )
        self._run(["nmcli", "radio", "wifi", "off"])
        self._activate_first_ethernet_connection()
        self._persist_network_mode("lan_only")

    def disable_lan_only_mode(self) -> None:
        self._run(["nmcli", "radio", "wifi", "on"])
        self._persist_network_mode("client")

    def is_lan_only_mode_active(self) -> bool:
        result = self._run(["nmcli", "radio", "wifi"], check=False)
        return result.returncode == 0 and "disabled" in result.stdout.lower()

    def is_ethernet_cable_connected(self) -> bool:
        interfaces = self._network_interfaces()
        for device in self._get_ethernet_interfaces():
            details = interfaces.get(device, {})
            if details.get("carrier") == "1":
                return True
            if details.get("ipv4"):
                return True

            state = self._nmcli_device_state(device)
            if state in {"connected", "connecting"}:
                return True
        return False

    def lan_status(self) -> dict[str, Any]:
        device_info = self.get_device_info()
        return {
            "isActive": self.is_lan_only_mode_active(),
            "isCableConnected": device_info["ethernetCableConnected"],
            "ipAddress": device_info.get("ethernetIpAddress")
            or device_info.get("ipAddress"),
            "macAddress": device_info.get("ethernetMacAddress")
            or device_info.get("macAddress"),
        }

    def _activate_first_ethernet_connection(self) -> None:
        result = self._run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], check=False
        )
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "802-3-ethernet":
                self._run(["nmcli", "connection", "up", parts[0]], check=False)
                return
        self._run(["nmcli", "connection", "up", "Wired connection 1"], check=False)

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
        direct = self._run(["nmcli", "connection", "delete", ssid], check=False)
        if direct.returncode == 0:
            return

        list_result = self._run(
            ["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"], check=False
        )
        for line in list_result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            uuid, connection_type = parts[0].strip(), parts[1]
            if not uuid or (
                "wireless" not in connection_type and "wifi" not in connection_type
            ):
                continue
            show_result = self._run(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "802-11-wireless.ssid",
                    "connection",
                    "show",
                    uuid,
                ],
                check=False,
            )
            if show_result.stdout.split(":")[-1].strip() == ssid:
                self._run(["nmcli", "connection", "delete", "uuid", uuid], check=False)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "network_mode": "client",
                "saved_networks": [],
                "access_point_config": None,
            }
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {
                "network_mode": "client",
                "saved_networks": [],
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
        use_sudo = command[0] in {"nmcli", "ip", "iw", "iwlist"}
        full_command = ["sudo", *command] if use_sudo else command
        result = subprocess.run(
            full_command,
            capture_output=True,
            text=True,
            timeout=25,
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
