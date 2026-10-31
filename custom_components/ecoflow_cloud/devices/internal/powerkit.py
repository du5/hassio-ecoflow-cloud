"""Power Kits (Power Hub) over the app protocol (private_api).

The 2nd-generation 5 kVA Power Hub is listed by the developer platform, but every
data request for it fails with error 1006, so the public_api ``Power Kits`` device
never sees it. The app's MQTT stream carries the same modules as the public device
(bbcin, bbcout, iclow, bpXXXX, kitscc, lddc, ichigh, ldac) plus the hub's own
``wireless`` and ``gps`` modules, only wrapped differently; see ``powerkit_data``
for the shapes.

The entities are the public ones plus what a 2nd-generation hub reports on top:
system totals (``bmsTotal`` / ``mainBP``), battery health and cumulative energy,
the inverter's effective settings (``appCfg``), the distribution box's extra DC
channels. Every module is its own Home Assistant device, discovered by the config
flow from a ``latestQuotas`` reply; the hub device keeps the totals and its own
modules.

Where a switch reads its state from one module but the command goes to another
(the public implementation left those switches without a readable state), the
switch lives on the module that reports the state and addresses the sibling
module in the command. Commands reuse the payloads EcoFlow documents for the 1st
generation; they have not been confirmed on a 2nd-generation hub.
"""

import logging
from typing import Any, override

from homeassistant.components.number import NumberEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfFrequency, UnitOfPower

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
from custom_components.ecoflow_cloud.devices.public.powerkit import BitMaskEnableEntity
from custom_components.ecoflow_cloud.devices.public.powerkit import PowerKit as PublicPowerKit
from custom_components.ecoflow_cloud.number import AcChargingPowerInAmpereEntity
from custom_components.ecoflow_cloud.sensor import (
    AmpSensorEntity,
    CapacitySensorEntity,
    CumulativeCapacitySensorEntity,
    EnergySensorEntity,
    InWattsSensorEntity,
    LevelSensorEntity,
    MiscSensorEntity,
    OutMilliampSensorEntity,
    OutMilliVoltSensorEntity,
    OutWattsSensorEntity,
    QuotaStatusSensorEntity,
    RemainSensorEntity,
    StateOfHealthSensorEntity,
    TempSensorEntity,
)
from custom_components.ecoflow_cloud.switch import EnabledEntity

_LOGGER = logging.getLogger(__name__)

# moduleType values of the set commands, from EcoFlow's Power Kits documentation
INVERTER_MODULE_TYPE = 15365
DC_IN_MODULE_TYPE = 15362

DC_PANEL_SWITCHES = 12  # relay channels in the panel's dcChRelay bitmask (F1 to F12 on the 2nd generation panel)

_MODULE_LABELS = {
    "iclow": "BMS",
    "kitscc": "MPPT",
    "bbcout": "Hub DC Out",
    "bbcin": "Hub DC In",
    "lddc": "DC Panel",
    "ichigh": "Hub AC Out",
    "ldac": "AC Panel",
}

# Product names as EcoFlow prints them (Chinese manual name, English name), shown as the
# Home Assistant device model. The hub is the 智控五合一充逆一体机 / Power Hub; iclow, ichigh,
# kitscc, bbcin and bbcout are the five functions inside it, bp* the batteries, lddc / ldac
# the two boards of the AC/DC smart distribution panel.
HUB_MODEL = "智控五合一充逆一体机 (Power Hub)"
_MODULE_MODELS = {
    "iclow": "智控五合一充逆一体机 逆变充电 电池侧 (Power Hub inverter charger, battery side)",
    "ichigh": "智控五合一充逆一体机 逆变充电 交流侧 (Power Hub inverter charger, AC side)",
    "kitscc": "智控五合一充逆一体机 MPPT 太阳能充电控制器 (Power Hub MPPT solar charge controller)",
    "bbcin": "智控五合一充逆一体机 DC-DC 电池充电器 (Power Hub DC-DC battery charger)",
    "bbcout": "智控五合一充逆一体机 DC-DC 降压输出 (Power Hub DC-DC step-down converter)",
    "lddc": "交直流智能配电盘 直流回路 (AC/DC Smart Distribution Panel, DC circuits)",
    "ldac": "交直流智能配电盘 交流回路 (AC/DC Smart Distribution Panel, AC circuits)",
}


def _module_label(module_type: str) -> str:
    if module_type.startswith("bp"):
        return "Battery"
    return _MODULE_LABELS.get(module_type, module_type)


# The two battery packs EcoFlow sells for the Power Kits, by the module type they report
_BATTERY_MODELS = {
    "bp5000": "磷酸铁锂电池5度电 (LFP Battery 5kWh)",  # 51.2 V / 100 Ah
    "bp2000": "磷酸铁锂电池2度电 (LFP Battery 2kWh)",  # 51.2 V / 40 Ah
}


def _module_model(module_type: str) -> str:
    if module_type.startswith("bp"):
        return _BATTERY_MODELS.get(module_type, "磷酸铁锂电池 (LFP Battery)")
    return _MODULE_MODELS.get(module_type, module_type)


class TotalCurrentSensorEntity(AmpSensorEntity):
    """``totalAmp`` is a signed 16-bit value in 0.1 A sent unsigned: 65486 is -5.0 A."""

    def _update_value(self, val: Any) -> bool:
        ival = int(val)
        if ival > 32767:
            ival -= 65536
        return super()._update_value(ival / 10)


class PowerKit(PublicPowerKit):
    def __init__(self, device_info: EcoflowDeviceInfo, device_data: DeviceData) -> None:
        super().__init__(device_info, device_data)
        if device_data.parent is not None:
            device_data.display_name = (
                f"{device_data.parent.name} {_module_label(device_data.device_type)} ({self.module_sn})"
            )
            self.model_name = _module_model(device_data.device_type)
        else:
            self.model_name = HUB_MODEL

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

    # ------------------------------------------------------------------ data
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
        if module_sn != self.module_sn:
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
            return PreparedData(online, {"params": hub_quota_fields(quota_map, self.device_data.sn)}, data)

        fields = module_quota_fields(quota_map, self.device_data.device_type, self.module_sn)
        if not fields:
            # the hub is online but does not report this module (disconnected battery, unplugged
            # panel): neither the status nor the stale readings must look fresh
            return PreparedData(None, None, data)
        return PreparedData(online, {"params": fields}, data)

    # -------------------------------------------------------------- commands
    def _sibling_sn(self, client: EcoflowApiClient, module_type: str) -> str:
        """Serial of another module of the same hub, for commands handled by a different module."""
        devices = getattr(client, "devices", None)
        parent = self.device_data.parent
        if not isinstance(devices, dict) or parent is None:
            return self.module_sn
        for device in devices.values():
            sibling = device.device_data
            if sibling.parent is not None and sibling.parent.sn == parent.sn and sibling.device_type == module_type:
                return getattr(device, "module_sn", sibling.sn)
        _LOGGER.warning("No %s module found for %s; sending the command to %s", module_type, parent.sn, self.module_sn)
        return self.module_sn

    def _inverter_command(self, client: EcoflowApiClient, params: dict[str, Any]) -> dict[str, Any]:
        # the inverter settings are read on ichigh but set through iclow
        return {
            "id": 123456789,
            "version": "1.0",
            "moduleSn": self._sibling_sn(client, "iclow"),
            "moduleType": INVERTER_MODULE_TYPE,
            "operateType": "dischgIcParaSet",
            "params": params,
        }

    # -------------------------------------------------------------- entities
    @override
    def sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        if self.is_hub:
            return self.hubSensors(client)

        sensors = list(super().sensors(client))
        module_type = self.device_data.device_type
        if module_type.startswith("bp"):
            sensors += [
                StateOfHealthSensorEntity(client, self, "soh", const.SOH),
                CapacitySensorEntity(client, self, "designCap", "Design Capacity", False),
                EnergySensorEntity(client, self, "accuChgEnergy", const.ACCU_CHARGE_ENERGY),
                EnergySensorEntity(client, self, "accuDsgEnergy", const.ACCU_DISCHARGE_ENERGY),
                CumulativeCapacitySensorEntity(client, self, "accuChgCap", const.ACCU_CHARGE_CAP, False),
                CumulativeCapacitySensorEntity(client, self, "accuDsgCap", const.ACCU_DISCHARGE_CAP, False),
                TempSensorEntity(client, self, "maxCellTemp", "Max Cell Temperature", False),
                TempSensorEntity(client, self, "minCellTemp", "Min Cell Temperature", False),
                MiscSensorEntity(client, self, "chgSetSoc", "Charge Limit", False).with_unit_of_measurement(PERCENTAGE),
                MiscSensorEntity(client, self, "dsgSetSoc", "Discharge Limit", False).with_unit_of_measurement(
                    PERCENTAGE
                ),
                MiscSensorEntity(client, self, "kitNum", "Kit Number", False),
                MiscSensorEntity(client, self, "warnCode", "Warning Code", False),
                MiscSensorEntity(client, self, "bmsFault", "BMS Fault", False),
            ]
        elif module_type == "iclow":
            sensors += [
                MiscSensorEntity(client, self, "acAlwaysOn", "AC Always On", False),
                MiscSensorEntity(
                    client, self, "invNoLoadPwr", "Inverter No Load Power", False
                ).with_unit_of_measurement(UnitOfPower.WATT),
            ]
        elif module_type == "ichigh":
            sensors += [
                MiscSensorEntity(client, self, "outVa", "AC Out Apparent Power").with_unit_of_measurement("VA"),
                InWattsSensorEntity(client, self, "chgWatts", "AC Charge Power"),
                MiscSensorEntity(
                    client, self, "appCfg.acMaxCurrSer", "AC Charge Current Limit", False
                ).with_unit_of_measurement(UnitOfElectricCurrent.AMPERE),
                MiscSensorEntity(
                    client, self, "appCfg.passByMaxCurr", "Bypass Current Limit", False
                ).with_unit_of_measurement(UnitOfElectricCurrent.AMPERE),
                MiscSensorEntity(
                    client, self, "appCfg.acVolSet", "AC Out Voltage Setting", False
                ).with_unit_of_measurement(UnitOfElectricPotential.VOLT),
                MiscSensorEntity(
                    client, self, "appCfg.acFreqSet", "AC Out Frequency Setting", False
                ).with_unit_of_measurement(UnitOfFrequency.HERTZ),
            ]
        elif module_type == "ldac":
            # the 2nd generation panel feeds two AC buses (L1 / L2 for split phase)
            sensors += [
                OutWattsSensorEntity(client, self, "acTotalWatts1", "AC L1 Power"),
                OutWattsSensorEntity(client, self, "acTotalWatts2", "AC L2 Power"),
                OutMilliVoltSensorEntity(client, self, "acInVol2", "AC L2 In Voltage"),
            ]
        elif module_type == "lddc":
            # the 2nd generation panel has 16 DC circuits: dcChWatt carries 12, the high current
            # circuits come in a second set of arrays (F13 to F16 as far as the data shows)
            sensors += [
                OutWattsSensorEntity(client, self, f"dcChWattV2[{i}]", f"DC Out {DC_PANEL_SWITCHES + i + 1}")
                for i in range(4)
            ]
            sensors += [
                OutMilliampSensorEntity(client, self, f"dcChCurV2[{i}]", f"DC Ampere Out {DC_PANEL_SWITCHES + i + 1}")
                for i in range(4)
            ]
            sensors.append(MiscSensorEntity(client, self, "dcChRelayV2", "DC Out 13-16 Ch Relay", False))
        elif module_type == "bbcin":
            sensors += [
                MiscSensorEntity(client, self, "alternatorMode", "Alternator Mode", False),
                MiscSensorEntity(client, self, "thirdWatts", "Third Input Power", False).with_unit_of_measurement(
                    UnitOfPower.WATT
                ),
                MiscSensorEntity(client, self, "setPortType", "Input Port Type", False),
            ]
        elif module_type == "bbcout":
            sensors += [
                MiscSensorEntity(client, self, "dcOutSta", "DC Output Enabled"),
                MiscSensorEntity(client, self, "beepEn", "Beeper", False),
                MiscSensorEntity(client, self, "dcAlwaysOn", "DC Always On", False),
                MiscSensorEntity(client, self, "noLoadPwr", "No Load Power", False).with_unit_of_measurement(
                    UnitOfPower.WATT
                ),
                MiscSensorEntity(client, self, "hubWorkMode", "Hub Work Mode", False),
                MiscSensorEntity(client, self, "dayEnergy", "DC Out Energy Today", False),
            ]
        return sensors

    def hubSensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        return [
            LevelSensorEntity(client, self, "totalSoc", const.COMBINED_BATTERY_LEVEL),
            InWattsSensorEntity(client, self, "totalInWatts", const.TOTAL_IN_POWER),
            OutWattsSensorEntity(client, self, "totalOutWatts", const.TOTAL_OUT_POWER),
            TotalCurrentSensorEntity(client, self, "totalAmp", "Total Current"),
            RemainSensorEntity(client, self, "totalRemainTime", const.REMAINING_TIME),
            MiscSensorEntity(client, self, "totalFullCap", "Total Full Capacity", False).with_unit_of_measurement("Ah"),
            MiscSensorEntity(client, self, "totalChgDsgState", "Total Charge Discharge State", False),
            MiscSensorEntity(client, self, "bmsChgUpline", "Charge Upper Limit", False).with_unit_of_measurement(
                PERCENTAGE
            ),
            MiscSensorEntity(client, self, "bmsDsgDownline", "Discharge Lower Limit", False).with_unit_of_measurement(
                PERCENTAGE
            ),
            MiscSensorEntity(client, self, "oilStartDownline", "Generator Start Level", False).with_unit_of_measurement(
                PERCENTAGE
            ),
            MiscSensorEntity(client, self, "oilStopUpline", "Generator Stop Level", False).with_unit_of_measurement(
                PERCENTAGE
            ),
            MiscSensorEntity(client, self, "scenes", "Scene", False),
            MiscSensorEntity(client, self, "gpsOn", "GPS Enabled", False),
            MiscSensorEntity(client, self, "gpsCon", "GPS Connected", False),
            QuotaStatusSensorEntity(client, self),
        ]

    @override
    def switches(self, client: EcoflowApiClient) -> list[SwitchEntity]:
        module_type = self.device_data.device_type
        if module_type == "iclow" or module_type == "bbcin":
            # their switches read a state only the sibling module reports; see ichigh / bbcout
            return []
        if module_type == "ichigh":
            return [
                *super().switches(client),
                EnabledEntity(
                    client,
                    self,
                    "invSwSta",
                    "AC Output",
                    lambda value: self._inverter_command(
                        client,
                        {
                            "powerOn": 1 if value == 1 else 0,
                            "acCurrMaxSet": 255,
                            "acChgDisa": 255,
                            "acFrequencySet": 255,
                            "acVolSet": 255,
                        },
                    ),
                    enableValue=1,
                ),
                EnabledEntity(
                    client,
                    self,
                    "appCfg.acChgDisable",
                    "AC Charging",
                    lambda value: self._inverter_command(
                        client,
                        {
                            "acCurrMaxSet": 255,
                            "powerOn": 255,
                            "acChgDisa": 0 if value == 0 else 1,
                            "acFrequencySet": 255,
                            "wakeup": 1,
                            "standbyTime": 255,
                            "acRlyCtrlDisable": 255,
                            "acVolSet": 255,
                            "passByMaxCurr": 255,
                        },
                    ),
                    enableValue=0,
                    disableValue=1,
                ),
            ]
        if module_type == "lddc":
            return [
                BitMaskEnableEntity(
                    client,
                    self,
                    f"dcChRelay.{n}",
                    f"DC Switch {n}",
                    self.dcSwitchFunction,
                    bits=DC_PANEL_SWITCHES,
                )
                for n in range(1, DC_PANEL_SWITCHES + 1)
            ]
        if module_type == "bbcout":
            return [
                EnabledEntity(
                    client,
                    self,
                    "dcOutSta",
                    "Main DC Output",
                    lambda value: {
                        "id": 123456789,
                        "version": "1.0",
                        "moduleSn": self._sibling_sn(client, "bbcin"),
                        "moduleType": DC_IN_MODULE_TYPE,
                        "operateType": "dischgParaSet",
                        "params": {"swSta": 1 if value == 1 else 0},
                    },
                    enableValue=1,
                ),
            ]
        return super().switches(client)

    @override
    def numbers(self, client: EcoflowApiClient) -> list[NumberEntity]:
        module_type = self.device_data.device_type
        if module_type == "iclow":
            return []
        if module_type == "ichigh" and self.device_data.parent is not None:
            parent_sn = self.device_data.parent.sn
            return [
                AcChargingPowerInAmpereEntity(
                    client,
                    self,
                    "appCfg.acMaxCurrSer",
                    const.AC_CHARGING_POWER,
                    0,
                    40,  # the app enforces the limit of the region; the hub reported 20 A on a 230 V kit
                    lambda value: {**self._inverter_command(client, {"acCurrMaxSet": int(value)}), "sn": parent_sn},
                ),
            ]
        return super().numbers(client)
