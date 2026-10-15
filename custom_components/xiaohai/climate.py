"""Air conditioner and floor heating."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import HVAC_TO_MODE, MODE_TO_HVAC, celsius, dp_value, profile, speed_choices
from .entity import XiaohaiEntity

_HVAC = {
    "cool": HVACMode.COOL,
    "heat": HVACMode.HEAT,
    "fan_only": HVACMode.FAN_ONLY,
    "auto": HVACMode.AUTO,
    "dry": HVACMode.DRY,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a climate entity for each air conditioner and floor heater."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _add_new() -> None:
        added = []
        for dev_id, device in coordinator.data.items():
            if dev_id in known:
                continue
            spec = profile(device)
            if not spec or spec["kind"] != "climate":
                continue
            known.add(dev_id)
            added.append(XiaohaiClimate(coordinator, dev_id))
        if added:
            async_add_entities(added)

    coordinator.async_add_listener(_add_new)
    _add_new()


class XiaohaiClimate(XiaohaiEntity, ClimateEntity):
    """One air conditioner or floor heater."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_min_temp = 16
    _attr_max_temp = 32

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_climate"

    @property
    def _spec(self) -> dict:
        return profile(self.device) or {}

    @property
    def icon(self) -> str:
        return "mdi:radiator" if self.device.get("category") == "wk" else "mdi:air-conditioner"

    @property
    def hvac_modes(self) -> list[HVACMode]:
        if self._spec.get("mode"):
            return [
                HVACMode.OFF,
                HVACMode.COOL,
                HVACMode.HEAT,
                HVACMode.DRY,
                HVACMode.FAN_ONLY,
                HVACMode.AUTO,
            ]
        return [HVACMode.OFF, HVACMode.HEAT]

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._spec.get("target"):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._spec.get("fan"):
            features |= ClimateEntityFeature.FAN_MODE
        return features

    @property
    def min_temp(self) -> float:
        return 5 if self.device.get("category") == "wk" else 16

    @property
    def max_temp(self) -> float:
        return 35 if self.device.get("category") == "wk" else 32

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = dp_value(self.device, self._spec.get("power"))
        if power is False:
            return HVACMode.OFF
        if not self._spec.get("mode"):
            return HVACMode.HEAT if power else HVACMode.OFF
        mapped = MODE_TO_HVAC.get(str(dp_value(self.device, self._spec.get("mode")) or ""))
        return _HVAC.get(mapped, HVACMode.AUTO)

    @property
    def current_temperature(self) -> float | None:
        return celsius(dp_value(self.device, self._spec.get("current")))

    @property
    def target_temperature(self) -> float | None:
        return celsius(dp_value(self.device, self._spec.get("target")))

    @property
    def fan_mode(self) -> str | None:
        value = dp_value(self.device, self._spec.get("fan"))
        return None if value is None else str(value)

    @property
    def fan_modes(self) -> list[str] | None:
        if not self._spec.get("fan"):
            return None
        return speed_choices(dp_value(self.device, self._spec.get("fan")))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        power = self._spec.get("power")
        if not power:
            return
        if hvac_mode == HVACMode.OFF:
            await self.async_publish({power: False})
            return
        dps: dict = {power: True}
        mode_dp = self._spec.get("mode")
        cloud = HVAC_TO_MODE.get(hvac_mode)
        if mode_dp and cloud:
            dps[mode_dp] = cloud
        await self.async_publish(dps)

    async def async_set_temperature(self, **kwargs) -> None:
        target = self._spec.get("target")
        if not target or ATTR_TEMPERATURE not in kwargs:
            return
        degrees = float(kwargs[ATTR_TEMPERATURE])
        raw = dp_value(self.device, target)
        value: int | float = int(round(degrees * 10)) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 60 else int(round(degrees))
        await self.async_publish({target: value})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        fan = self._spec.get("fan")
        if fan:
            await self.async_publish({fan: fan_mode})

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT if not self._spec.get("mode") else HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
