"""Network management endpoints for Wi-Fi, hotspot, and LAN control."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from raccoon_cli.server.auth import require_auth
from raccoon_cli.server.services.network_manager import NetworkManager

router = APIRouter(
    prefix="/api/v1/network",
    tags=["network"],
    dependencies=[Depends(require_auth)],
)

_network_manager = NetworkManager()


class WifiCredentialsPayload(BaseModel):
    credentialsType: Literal["personal", "enterprise"]
    password: str | None = None
    username: str | None = None
    caCertificatePath: str | None = None


class WifiNetworkPayload(BaseModel):
    ssid: str
    encryptionType: str
    isKnown: bool
    isConnected: bool


class SavedNetworkPayload(BaseModel):
    ssid: str
    encryptionType: str
    credentials: WifiCredentialsPayload
    lastConnected: str
    autoConnect: bool = True


class ConnectRequest(BaseModel):
    ssid: str
    encryptionType: str
    credentials: WifiCredentialsPayload


class NetworkModeRequest(BaseModel):
    mode: Literal["client", "access_point", "lan_only"]


class AccessPointConfigPayload(BaseModel):
    ssid: str
    password: str
    band: Literal["band2_4GHz", "band5GHz", "bandAuto"] = "bandAuto"
    channel: int = 0
    encryptionType: str = "wpa3Personal"
    hidden: bool = False
    maxClients: int = 8


class AccessPointStatusPayload(BaseModel):
    isStarted: bool
    config: AccessPointConfigPayload | None = None


class AccessPointChannelPayload(BaseModel):
    channel: int
    networkCount: int
    ssids: list[str]
    isRecommended: bool


class AccessPointDetectedNetworkPayload(BaseModel):
    ssid: str
    channel: int
    frequencyMHz: int | None = None
    centerFrequencyMHz: int | None = None
    channelWidthMHz: int | None = None
    signalDbm: int | None = None
    qualityPercent: int | None = None
    overlapStartChannel: int
    overlapEndChannel: int
    affectedChannels: list[int]


class AccessPointChannelScanPayload(BaseModel):
    band: Literal["band2_4GHz", "band5GHz", "bandAuto"]
    recommendedChannel: int
    detectedNetworks: int
    channels: list[AccessPointChannelPayload]
    networks: list[AccessPointDetectedNetworkPayload]


class LanStatusPayload(BaseModel):
    isActive: bool
    isCableConnected: bool
    ipAddress: str | None = None
    macAddress: str | None = None


@router.get("/networks", response_model=list[WifiNetworkPayload])
async def get_networks() -> list[dict[str, Any]]:
    return _network_manager.scan_networks()


@router.post("/connect")
async def connect(request: ConnectRequest) -> dict[str, str]:
    try:
        _network_manager.connect(
            request.ssid,
            request.encryptionType,
            request.credentials.model_dump(),
        )
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/forget/{ssid:path}")
async def forget(ssid: str) -> dict[str, str]:
    try:
        _network_manager.forget(ssid)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/device-info")
async def device_info() -> dict[str, Any]:
    return _network_manager.get_device_info()


@router.get("/mode")
async def get_mode() -> dict[str, str]:
    return {"mode": _network_manager.get_network_mode()}


@router.put("/mode")
async def set_mode(request: NetworkModeRequest) -> dict[str, str]:
    try:
        _network_manager.set_network_mode(request.mode)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/access-point/config")
async def get_access_point_config() -> AccessPointConfigPayload | None:
    config = _network_manager.get_access_point_config()
    if config is None:
        return None
    return AccessPointConfigPayload(**config)


@router.post("/access-point/start")
async def start_access_point(
    config: AccessPointConfigPayload,
) -> AccessPointConfigPayload:
    try:
        applied = _network_manager.start_access_point(config.model_dump())
        return AccessPointConfigPayload(**applied)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/access-point/stop")
async def stop_access_point() -> dict[str, str]:
    try:
        _network_manager.stop_access_point()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/access-point/status", response_model=AccessPointStatusPayload)
async def access_point_status() -> dict[str, Any]:
    return {
        "isStarted": _network_manager.is_access_point_active(),
        "config": _network_manager.get_access_point_config(),
    }


@router.get("/access-point/best-band")
async def best_band() -> dict[str, str]:
    return {"band": _network_manager.find_best_wifi_band()}


@router.get("/access-point/best-channel")
async def best_channel(
    band: Literal["band2_4GHz", "band5GHz", "bandAuto"],
) -> dict[str, int]:
    return {"channel": _network_manager.find_best_channel(band)}


@router.get("/access-point/channel-scan", response_model=AccessPointChannelScanPayload)
async def channel_scan(
    band: Literal["band2_4GHz", "band5GHz", "bandAuto"],
) -> dict[str, Any]:
    return _network_manager.scan_access_point_channels(band)


@router.get("/saved", response_model=list[SavedNetworkPayload])
async def get_saved_networks() -> list[dict[str, Any]]:
    return _network_manager.get_saved_networks()


@router.get("/saved/{ssid:path}", response_model=SavedNetworkPayload | None)
async def get_saved_network(ssid: str) -> dict[str, Any] | None:
    return _network_manager.get_saved_network(ssid)


@router.put("/saved")
async def save_network(network: SavedNetworkPayload) -> dict[str, str]:
    _network_manager.save_network(network.model_dump())
    return {"status": "ok"}


@router.delete("/saved/{ssid:path}")
async def remove_saved_network(ssid: str) -> dict[str, str]:
    _network_manager.remove_saved_network(ssid)
    return {"status": "ok"}


@router.post("/lan/enable")
async def enable_lan_only_mode() -> dict[str, str]:
    try:
        _network_manager.enable_lan_only_mode()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/lan/disable")
async def disable_lan_only_mode() -> dict[str, str]:
    try:
        _network_manager.disable_lan_only_mode()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/lan/status", response_model=LanStatusPayload)
async def lan_status() -> dict[str, Any]:
    return _network_manager.lan_status()
