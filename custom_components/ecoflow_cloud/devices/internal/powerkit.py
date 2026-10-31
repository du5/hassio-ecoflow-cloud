"""Power Kits (Power Hub) over the app protocol (private_api).

The 2nd-generation 5 kVA Power Hub is listed by the developer platform, but every
data request for it fails with error 1006, so the public_api ``Power Kits`` device
never sees it. The app's MQTT stream carries the same modules as the public device
(bbcin, bbcout, iclow, bpXXXX, kitscc, lddc, ichigh, ldac) plus the hub's own
``wireless`` module, only wrapped differently; see ``powerkit_data`` for the shapes.

The entities are the public ones. Every module is its own Home Assistant device,
discovered by the config flow from a ``latestQuotas`` reply; the hub device keeps
the system totals the main battery reports and the wireless module.

Commands reuse the payloads of the public device. They are documented by EcoFlow
for the 1st generation only and have not been confirmed on a 2nd-generation hub.
"""

import logging
from typing import override

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import PERCENTAGE

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.api.message import JSONMessage, Message
from custom_components.ecoflow_cloud.device_data import DeviceData
from custom_components.ecoflow_cloud.devices import EcoflowDeviceInfo, const
from custom_components.ecoflow_cloud.devices.data_holder import PreparedData
from custom_components.ecoflow_cloud.devices.internal.powerkit_data import (
    hub_fields_from_message,
    hub_quota_fields,
    module_quota_fields,
    parse_module_message,
    reply_online,
)
from custom_components.ecoflow_cloud.devices.public.powerkit import PowerKit as PublicPowerKit
from custom_components.ecoflow_cloud.sensor import (
    InWattsSensorEntity,
    LevelSensorEntity,
    MiscSensorEntity,
    OutWattsSensorEntity,
    QuotaStatusSensorEntity,
)

_LOGGER = logging.getLogger(__name__)

_MODULE_LABELS = {
    "iclow": "BMS",
    "kitscc": "MPPT",
    "bbcout": "Hub DC Out",
    "bbcin": "Hub DC In",
    "lddc": "DC Panel",
    "ichigh": "Hub AC Out",
    "ldac": "AC Panel",
}


def _module_label(module_type: str) -> str:
    if module_type.startswith("bp"):
        return "Battery"
    return _MODULE_LABELS.get(module_type, module_type)


class PowerKit(PublicPowerKit):
    def __init__(self, device_info: EcoflowDeviceInfo, device_data: DeviceData) -> None:
        super().__init__(device_info, device_data)
        if device_data.parent is not None:
            device_data.display_name = (
                f"{device_data.parent.name} {_module_label(device_data.device_type)} ({device_data.sn})"
            )

    @property
    def is_hub(self) -> bool:
        return self.device_data.parent is None

    def get_quota_message(self) -> Message:
        return JSONMessage(
            {
                "version": "1.1",
                "moduleType": 0,
                "operateType": "latestQuotas",
                "params": {},
            }
        )

    @override
    def _prepare_data_data_topic(self, raw_data: bytes) -> PreparedData:
        # raw_data stays None: in diagnostic mode the data holder merges raw_data["params"] into the
        # device params a second time, which would bring the other modules' double-encoded strings along
        data = self._prepare_data(raw_data)
        if not data:
            return PreparedData(None, None, None)

        if self.is_hub:
            fields = hub_fields_from_message(data, self.device_data.sn)
            # any module traffic proves the hub is online, even when nothing is kept
            return PreparedData(None, {"params": fields or {}}, None)

        module_sn, fields = parse_module_message(data)
        if module_sn != self.device_data.sn:
            return PreparedData(None, None, None)
        return PreparedData(None, {"moduleSn": module_sn, "params": fields}, None)

    @override
    def _prepare_data_get_reply_topic(self, raw_data: bytes) -> PreparedData:
        data = self._prepare_data(raw_data)
        if data.get("operateType") != "latestQuotas" or not isinstance(data.get("data"), dict):
            return PreparedData(None, None, data)

        reply = data["data"]
        online = reply_online(reply)
        if online is False:
            return PreparedData(False, None, data)

        quota_map = reply.get("quotaMap")
        if self.is_hub:
            fields = hub_quota_fields(quota_map, self.device_data.sn)
        else:
            fields = module_quota_fields(quota_map, self.device_data.device_type, self.device_data.sn)
        return PreparedData(online, {"params": fields}, data)

    @override
    def sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        if not self.is_hub:
            return super().sensors(client)
        return [
            LevelSensorEntity(client, self, "totalSoc", const.COMBINED_BATTERY_LEVEL),
            InWattsSensorEntity(client, self, "totalInWatts", const.TOTAL_IN_POWER),
            OutWattsSensorEntity(client, self, "totalOutWatts", const.TOTAL_OUT_POWER),
            MiscSensorEntity(client, self, "totalFullCap", "Total Full Capacity", False).with_unit_of_measurement("Ah"),
            MiscSensorEntity(client, self, "totalChgDsgState", "Total Charge Discharge State", False),
            MiscSensorEntity(client, self, "bmsChgUpline", "Charge Upper Limit", False).with_unit_of_measurement(
                PERCENTAGE
            ),
            MiscSensorEntity(client, self, "scenes", "Scene", False),
            MiscSensorEntity(client, self, "gpsOn", "GPS Enabled", False),
            MiscSensorEntity(client, self, "gpsCon", "GPS Connected", False),
            QuotaStatusSensorEntity(client, self),
        ]
