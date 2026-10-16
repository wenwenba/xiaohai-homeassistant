"""Air conditioner and floor heating."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import (
    MODE_TO_HVAC,
    celsius,
    cloud_mode_token,
    dp_value,
    hvac_names,
    profile,
    scaled,
    apply_speed_mask,
    speed_choices,
)
from .entity import XiaohaiEntity, take_new_entities

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

    def _build(dev_id: str, device: dict) -> XiaohaiClimate | None:
        spec = profile(device)
        if not spec or spec["kind"] != "climate":
            return None
        return XiaohaiClimate(coordinator, dev_id)

    def _add_new() -> None:
        added = take_new_entities(coordinator, known, _build)
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


class XiaohaiClimate(XiaohaiEntity, ClimateEntity):
    """One air conditioner or floor heater."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_climate"

    @property
    def _spec(self) -> dict:
        return profile(self.device) or {}

    @property
    def icon(self) -> str:
        if self._spec.get("mode") or self._spec.get("fan"):
            return "mdi:air-conditioner"
        return "mdi:radiator"

    @property
    def hvac_modes(self) -> list[HVACMode]:
        if self._spec.get("mode"):
            modes = [HVACMode.OFF]
            for name in hvac_names(self._spec.get("mode_values")):
                ha = _HVAC.get(name)
                if ha and ha not in modes:
                    modes.append(ha)
            return modes
        return [HVACMode.OFF, HVACMode.HEAT]

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._spec.get("target"):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._spec.get("fan"):
            features |= ClimateEntityFeature.FAN_MODE
        return features

    def _target_scale(self) -> int | None:
        scale = self._spec.get("target_scale")
        return None if scale is None else int(scale)

    @property
    def min_temp(self) -> float:
        shown = scaled(self._spec.get("target_min"), self._target_scale())
        return super().min_temp if shown is None else shown

    @property
    def max_temp(self) -> float:
        shown = scaled(self._spec.get("target_max"), self._target_scale())
        return super().max_temp if shown is None else shown

    @property
    def target_temperature_step(self) -> float:
        shown = scaled(self._spec.get("target_step"), self._target_scale())
        return super().target_temperature_step if shown is None else shown

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = dp_value(self.device, self._spec.get("power"))
        if power is False:
            return HVACMode.OFF
        if not self._spec.get("mode"):
            return HVACMode.HEAT if power else HVACMode.OFF
        mapped = MODE_TO_HVAC.get(str(dp_value(self.device, self._spec.get("mode")) or ""))
        return _HVAC.get(mapped) if mapped else None

    def _reading_scale(self, which: str) -> int | None:
        scale = self._spec.get(which)
        if scale is None and which == "current_scale":
            scale = self._spec.get("target_scale")
        return None if scale is None else int(scale)

    @property
    def current_temperature(self) -> float | None:
        return celsius(dp_value(self.device, self._spec.get("current")), self._reading_scale("current_scale"))

    @property
    def target_temperature(self) -> float | None:
        return celsius(dp_value(self.device, self._spec.get("target")), self._reading_scale("target_scale"))

    @property
    def fan_mode(self) -> str | None:
        value = dp_value(self.device, self._spec.get("fan"))
        return None if value is None else str(value)

    @property
    def fan_modes(self) -> list[str] | None:
        if not self._spec.get("fan"):
            return None
        values = apply_speed_mask(list(self._spec.get("fan_values") or []), self.device.get("dps"))
        return speed_choices(dp_value(self.device, self._spec.get("fan")), values)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        power = self._spec.get("power")
        if not power:
            return
        if hvac_mode == HVACMode.OFF:
            await self.async_publish({power: False})
            return
        dps: dict = {power: True}
        mode_dp = self._spec.get("mode")
        cloud = cloud_mode_token(self._spec.get("mode_values"), hvac_mode)
        if mode_dp and cloud:
            dps[mode_dp] = cloud
        await self.async_publish(dps)

    async def async_set_temperature(self, **kwargs) -> None:
        target = self._spec.get("target")
        if not target or ATTR_TEMPERATURE not in kwargs:
            return
        degrees = float(kwargs[ATTR_TEMPERATURE])
        scale = self._target_scale()
        if scale:
            value: int = int(round(degrees * (10 ** scale)))
        else:
            raw = dp_value(self.device, target)
            value = (
                int(round(degrees * 10))
                if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 60
                else int(round(degrees))
            )
        await self.async_publish({target: value})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        fan = self._spec.get("fan")
        if fan:
            await self.async_publish({fan: fan_mode})

    async def async_turn_on(self) -> None:
        modes = [mode for mode in self.hvac_modes if mode != HVACMode.OFF]
        if HVACMode.AUTO in modes:
            await self.async_set_hvac_mode(HVACMode.AUTO)
        elif modes:
            await self.async_set_hvac_mode(modes[0])
        else:
            power = self._spec.get("power")
            if power:
                await self.async_publish({power: True})

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
