"""Sensors for BYD Vehicle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pybyd.models._base import BydEnum
from pybyd.models.hvac import AirConditioningMode, HvacWindMode, HvacWindPosition
from pybyd.models.realtime import AirCirculationMode, TirePressureUnit
from pybyd.models.vehicle import Vehicle

from .const import DOMAIN
from .coordinator import BydDataUpdateCoordinator
from .entity import BydVehicleEntity

# ---------------------------------------------------------------------------
# Enum / value display helpers
# ---------------------------------------------------------------------------


def _enum_label(value: BydEnum) -> str | None:
    """Convert a BydEnum member to a human-readable label.

    ``ChargingState.NOT_CHARGING`` → ``"Not charging"``
    ``SeatHeatVentState.LOW`` → ``"Low"``

    Returns ``None`` for sentinel members (UNKNOWN, NO_DATA, UNAVAILABLE)
    so HA shows "unavailable" instead of a meaningless string.
    """
    if value.name in ("UNKNOWN", "NO_DATA", "UNAVAILABLE"):
        return None
    return value.name.replace("_", " ").capitalize()


def _resolve_any(
    obj: Any, attr: str, enum_cls: type[BydEnum] | None = None,
) -> str | None:
    """Resolve an attribute that may be a BydEnum or plain int.

    When *enum_cls* is provided and the value is a plain int (from a
    union-typed field), coerce it to the enum before labelling.
    """
    v = getattr(obj, attr, None)
    if v is None:
        return None
    if isinstance(v, BydEnum):
        return _enum_label(v)
    if enum_cls is not None and isinstance(v, int):
        try:
            return _enum_label(enum_cls(v))
        except (ValueError, KeyError):
            return _enum_label(enum_cls(-1))  # UNKNOWN
    return v


def _warning_indicator(attr: str) -> Callable[[Any], str | None]:
    """Map a warning indicator: 0=OK, non-zero=Warning."""

    def _convert(obj: Any) -> str | None:
        v = getattr(obj, attr, None)
        if v is None or v == -1:
            return None
        return "OK" if v == 0 else "Warning"

    return _convert


def _on_off_indicator(attr: str) -> Callable[[Any], str | None]:
    """Map an on/off indicator using BYD convention: 0=no data, 1=off, 2=on.

    BYD HVAC fields (defrost, wiper heat, rapid heat/cool, etc.) use
    1=off, 2=on — NOT the standard 0=off, 1=on. Confirmed from live
    Sealion 7 debug dumps (2026-04-03).
    """

    def _convert(obj: Any) -> str | None:
        v = getattr(obj, attr, None)
        if v is None or v == -1:
            return None
        if isinstance(v, BydEnum):
            return _enum_label(v)
        if v == 2:
            return "On"
        if v == 1:
            return "Off"
        if v == 0:
            return None  # no data
        return str(v)

    return _convert


# ---------------------------------------------------------------------------
# Simple presentation-level validators (pyBYD state engine handles deeper
# quality guards; these cover HA display edge-cases only).
# ---------------------------------------------------------------------------

FieldValidator = Callable[[Any, Any], Any]


def keep_previous_when_zero(previous: Any, current: Any) -> Any:
    """Return *previous* when *current* is zero or None.

    Prevents transient ``0 %`` SOC values from showing in the HA UI
    when the vehicle sends stale/invalid telemetry.
    """
    if current is None or current == 0:
        return previous
    return current


def _normalize_epoch(value: Any) -> datetime | None:
    """Ensure a pre-parsed BydTimestamp is UTC-aware, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


@dataclass(frozen=True, kw_only=True)
class BydSensorDescription(SensorEntityDescription):
    """Describe a BYD sensor."""

    source: str = "realtime"
    attr_key: str | None = None
    value_fn: Callable[[Any], Any] | None = None
    validator_fn: FieldValidator | None = None


def _round_int_attr(attr: str) -> Callable[[Any], int | None]:
    """Create a converter that rounds a numeric attribute to an integer."""

    def _convert(obj: Any) -> int | None:
        value = getattr(obj, attr, None)
        if value is None:
            return None
        return int(round(float(value)))

    return _convert


def _parse_numeric_string(attr: str) -> Callable[[Any], float | None]:
    """Create a converter that parses a string attribute to float.

    Returns *None* for sentinel strings like ``"--"`` or non-numeric values.
    The BYD API sends several energy-related fields as strings (e.g. ``"29.6"``).
    """

    def _convert(obj: Any) -> float | None:
        value = getattr(obj, attr, None)
        if value is None or value == "--":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    return _convert


def _positive_float_attr(attr: str) -> Callable[[Any], float | None]:
    """Create a converter returning *None* for negative sentinel values.

    The BYD API uses ``-1`` as a "not available" marker for several
    numeric fields (e.g. ``oilEndurance``).
    """

    def _convert(obj: Any) -> float | None:
        value = getattr(obj, attr, None)
        if value is None or value < 0:
            return None
        return float(value)

    return _convert


SENSOR_DESCRIPTIONS: tuple[BydSensorDescription, ...] = (
    # =============================================
    # Realtime: primary sensors (enabled by default)
    # =============================================
    BydSensorDescription(
        key="elec_percent",
        source="realtime",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        validator_fn=keep_previous_when_zero,
    ),
    BydSensorDescription(
        key="endurance_mileage",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value_fn=_round_int_attr("endurance_mileage"),
    ),
    BydSensorDescription(
        key="total_mileage",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=_round_int_attr("total_mileage"),
    ),
    BydSensorDescription(
        key="speed",
        source="realtime",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BydSensorDescription(
        key="temp_in_car",
        source="realtime",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda obj: (
            int(round(obj.temp_in_car)) if obj.temp_in_car is not None else 0
        ),
    ),
    # Tire pressures – unit resolved dynamically from tire_press_unit;
    # kPa is the default because most BYD vehicles report tirePressUnit=3.
    BydSensorDescription(
        key="left_front_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="right_front_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="left_rear_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="right_rear_tire_pressure",
        source="realtime",
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-tire-alert",
    ),
    BydSensorDescription(
        key="battery_power",
        attr_key="gl",
        source="realtime",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # =============================================
    # HVAC: primary sensors (enabled by default)
    # =============================================
    BydSensorDescription(
        key="temp_out_car",
        source="hvac",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_round_int_attr("temp_out_car"),
    ),
    BydSensorDescription(
        key="pm",
        source="hvac",
        native_unit_of_measurement="µg/m³",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # =============================================
    # Charging sensors
    # =============================================
    BydSensorDescription(
        key="charge_state",
        source="realtime",
        icon="mdi:ev-station",
    ),
    BydSensorDescription(
        key="charging_state",
        source="realtime",
        icon="mdi:ev-station",
    ),
    BydSensorDescription(
        key="remaining_hours",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:clock-outline",
    ),
    BydSensorDescription(
        key="remaining_minutes",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:clock-outline",
    ),
    BydSensorDescription(
        key="full_hour",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:clock-outline",
    ),
    BydSensorDescription(
        key="full_minute",
        source="realtime",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:clock-outline",
    ),
    BydSensorDescription(
        key="wait_status",
        source="realtime",
        icon="mdi:timer-sand",
        value_fn=lambda obj: (
            "Not waiting" if obj.wait_status == 0
            else f"Waiting ({obj.wait_status})"
            if obj.wait_status is not None and obj.wait_status > 0
            else None
        ),
    ),
    BydSensorDescription(
        key="booking_charge_state",
        source="realtime",
        icon="mdi:calendar-clock",
        value_fn=lambda obj: {0: "Off", 1: "Scheduled"}.get(
            obj.booking_charge_state, f"Mode {obj.booking_charge_state}"
        ) if obj.booking_charge_state is not None else None,
    ),
    BydSensorDescription(
        key="booking_charging_hour",
        source="realtime",
        icon="mdi:calendar-clock",
    ),
    BydSensorDescription(
        key="booking_charging_minute",
        source="realtime",
        icon="mdi:calendar-clock",
    ),
    BydSensorDescription(
        key="rate",
        source="realtime",
        native_unit_of_measurement="kW",
        icon="mdi:ev-station",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_positive_float_attr("rate"),
    ),
    # =============================================
    # Vehicle state sensors
    # =============================================
    BydSensorDescription(
        key="vehicle_state",
        source="realtime",
        icon="mdi:car-info",
    ),
    BydSensorDescription(
        key="power_gear",
        source="realtime",
        icon="mdi:car-shift-pattern",
    ),
    BydSensorDescription(
        key="epb",
        source="realtime",
        icon="mdi:car-brake-parking",
        value_fn=lambda obj: {0: "Released", 1: "Engaged"}.get(
            obj.epb, None
        ) if obj.epb is not None and obj.epb != -1 else None,
    ),
    # =============================================
    # Battery / range (alternative fields)
    # =============================================
    BydSensorDescription(
        key="power_battery",
        source="realtime",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        validator_fn=keep_previous_when_zero,
    ),
    BydSensorDescription(
        key="ev_endurance",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_round_int_attr("ev_endurance"),
    ),
    BydSensorDescription(
        key="endurance_mileage_v2",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_round_int_attr("endurance_mileage_v2"),
    ),
    BydSensorDescription(
        key="total_mileage_v2",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_round_int_attr("total_mileage_v2"),
    ),
    # =============================================
    # Energy / consumption sensors
    # =============================================
    BydSensorDescription(
        key="total_power",
        source="realtime",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
    ),
    BydSensorDescription(
        key="energy_consumption",
        source="realtime",
        native_unit_of_measurement="kWh/100km",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_parse_numeric_string("energy_consumption"),
    ),
    BydSensorDescription(
        key="nearest_energy_consumption",
        source="realtime",
        native_unit_of_measurement="kWh/100km",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_parse_numeric_string("nearest_energy_consumption"),
    ),
    BydSensorDescription(
        key="recent_50km_energy",
        source="realtime",
        native_unit_of_measurement="kWh/100km",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_parse_numeric_string("recent_50km_energy"),
    ),
    BydSensorDescription(
        key="total_energy",
        source="realtime",
        native_unit_of_measurement="kWh",
        icon="mdi:flash",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_parse_numeric_string("total_energy"),
    ),
    BydSensorDescription(
        key="total_consumption",
        source="realtime",
        native_unit_of_measurement="kWh",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_parse_numeric_string("total_consumption"),
    ),
    BydSensorDescription(
        key="total_consumption_en",
        source="realtime",
        native_unit_of_measurement="kWh",
        icon="mdi:lightning-bolt",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_parse_numeric_string("total_consumption_en"),
    ),
    # =============================================
    # Fuel (hybrid vehicles)
    # =============================================
    BydSensorDescription(
        key="oil_endurance",
        source="realtime",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        value_fn=_round_int_attr("oil_endurance"),
    ),
    BydSensorDescription(
        key="oil_percent",
        source="realtime",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gas-station",
        value_fn=_positive_float_attr("oil_percent"),
    ),
    BydSensorDescription(
        key="total_oil",
        source="realtime",
        native_unit_of_measurement="L",
        icon="mdi:gas-station",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # =============================================
    # Temperature sensors
    # =============================================
    BydSensorDescription(
        key="ect_value",
        source="realtime",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:coolant-temperature",
    ),
    BydSensorDescription(
        key="copilot_setting_temp_new",
        source="hvac",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        icon="mdi:thermometer",
    ),
    # =============================================
    # HVAC / climate sensors
    # =============================================
    BydSensorDescription(
        key="air_conditioning_mode",
        source="hvac",
        icon="mdi:air-conditioner",
        value_fn=lambda obj: _resolve_any(
            obj, "air_conditioning_mode", AirConditioningMode,
        ),
    ),
    BydSensorDescription(
        key="wind_mode",
        source="hvac",
        icon="mdi:fan",
        value_fn=lambda obj: _resolve_any(obj, "wind_mode", HvacWindMode),
    ),
    BydSensorDescription(
        key="wind_position",
        source="hvac",
        icon="mdi:weather-windy",
        value_fn=lambda obj: _resolve_any(obj, "wind_position", HvacWindPosition),
    ),
    BydSensorDescription(
        key="air_run_state",
        source="hvac",
        icon="mdi:air-conditioner",
        value_fn=lambda obj: _resolve_any(obj, "air_run_state", AirCirculationMode),
    ),
    BydSensorDescription(
        key="cycle_choice",
        source="hvac",
        icon="mdi:rotate-3d-variant",
        value_fn=lambda obj: _resolve_any(obj, "cycle_choice", AirCirculationMode),
    ),
    BydSensorDescription(
        key="front_defrost_status",
        source="hvac",
        icon="mdi:car-defrost-front",
        value_fn=_on_off_indicator("front_defrost_status"),
    ),
    BydSensorDescription(
        key="electric_defrost_status",
        source="hvac",
        icon="mdi:car-defrost-rear",
        value_fn=_on_off_indicator("electric_defrost_status"),
    ),
    BydSensorDescription(
        key="rapid_increase_temp_state",
        source="hvac",
        icon="mdi:fire",
        value_fn=_on_off_indicator("rapid_increase_temp_state"),
    ),
    BydSensorDescription(
        key="rapid_decrease_temp_state",
        source="hvac",
        icon="mdi:snowflake",
        value_fn=_on_off_indicator("rapid_decrease_temp_state"),
    ),
    BydSensorDescription(
        key="wiper_heat_status",
        source="hvac",
        icon="mdi:car-windshield",
        value_fn=_on_off_indicator("wiper_heat_status"),
    ),
    BydSensorDescription(
        key="air_temp_level",
        source="hvac",
        icon="mdi:thermometer-lines",
    ),
    BydSensorDescription(
        key="air_condition_temp_range",
        source="hvac",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer-auto",
    ),
    # =============================================
    # Refrigerator (camping/V2L vehicles)
    # =============================================
    BydSensorDescription(
        key="refrigerator_state",
        source="hvac",
        icon="mdi:fridge",
        value_fn=_on_off_indicator("refrigerator_state"),
    ),
    BydSensorDescription(
        key="refrigerator_door_state",
        source="hvac",
        icon="mdi:fridge",
        value_fn=lambda obj: {0: "Closed", 1: "Open"}.get(
            obj.refrigerator_door_state, None
        ) if obj.refrigerator_door_state is not None else None,
    ),
    BydSensorDescription(
        key="refrigerator_temp",
        source="hvac",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fridge-outline",
    ),
    # =============================================
    # Seat heating / ventilation (3rd row)
    # =============================================
    BydSensorDescription(
        key="lr_third_heat_state",
        source="hvac",
        icon="mdi:car-seat-heater",
    ),
    BydSensorDescription(
        key="rr_third_heat_state",
        source="hvac",
        icon="mdi:car-seat-heater",
    ),
    BydSensorDescription(
        key="lr_third_ventilation_state",
        source="hvac",
        icon="mdi:car-seat-cooler",
    ),
    BydSensorDescription(
        key="rr_third_ventilation_state",
        source="hvac",
        icon="mdi:car-seat-cooler",
    ),
    # =============================================
    # Safety / warnings (system indicators)
    # =============================================
    BydSensorDescription(
        key="engine_status",
        source="realtime",
        icon="mdi:engine",
        value_fn=_warning_indicator("engine_status"),
    ),
    BydSensorDescription(
        key="ok_light",
        source="realtime",
        icon="mdi:check-circle",
        value_fn=_on_off_indicator("ok_light"),
    ),
    BydSensorDescription(
        key="power_battery_connection",
        source="realtime",
        icon="mdi:battery-alert",
        value_fn=_warning_indicator("power_battery_connection"),
    ),
    BydSensorDescription(
        key="ins",
        source="realtime",
        icon="mdi:shield-car",
        value_fn=_warning_indicator("ins"),
    ),
    # =============================================
    # Diagnostic (metadata / config / units)
    # =============================================
    BydSensorDescription(
        key="tire_press_unit",
        source="realtime",
        icon="mdi:car-tire-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="upgrade_status",
        source="realtime",
        icon="mdi:cellphone-arrow-down",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="nearest_energy_consumption_unit",
        source="realtime",
        icon="mdi:lightning-bolt",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="endurance_mileage_v2_unit",
        source="realtime",
        icon="mdi:map-marker-distance",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="total_mileage_v2_unit",
        source="realtime",
        icon="mdi:counter",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="repair_mode_switch",
        source="realtime",
        icon="mdi:wrench",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda obj: (
            "On" if str(getattr(obj, "repair_mode_switch", None))
            not in ("None", "0", "-1", "")
            else "Off"
            if getattr(obj, "repair_mode_switch", None) is not None
            else None
        ),
    ),
    BydSensorDescription(
        key="vehicle_time_zone",
        source="realtime",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ==========================================
    # Last updated timestamp
    # ==========================================
    BydSensorDescription(
        key="last_updated",
        source="realtime",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BydSensorDescription(
        key="gps_last_updated",
        source="gps",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:crosshairs-gps",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BYD sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, BydDataUpdateCoordinator] = data["coordinators"]
    gps_coordinators = data.get("gps_coordinators", {})

    entities: list[SensorEntity] = []
    for vin, coordinator in coordinators.items():
        vehicle = coordinator.vehicle
        for description in SENSOR_DESCRIPTIONS:
            if description.key == "gps_last_updated":
                gps_coordinator = gps_coordinators.get(vin)
                if gps_coordinator is not None:
                    entities.append(
                        BydSensor(gps_coordinator, vin, vehicle, description)
                    )
                continue
            entities.append(BydSensor(coordinator, vin, vehicle, description))

    async_add_entities(entities)


_TIRE_PRESSURE_KEYS = {
    "left_front_tire_pressure",
    "right_front_tire_pressure",
    "left_rear_tire_pressure",
    "right_rear_tire_pressure",
}

_TIRE_UNIT_MAP = {
    TirePressureUnit.BAR: UnitOfPressure.BAR,
    TirePressureUnit.PSI: UnitOfPressure.PSI,
    TirePressureUnit.KPA: UnitOfPressure.KPA,
}


class BydSensor(BydVehicleEntity, SensorEntity):
    """Representation of a BYD vehicle sensor.

    All state is read from ``VehicleSnapshot`` sections via the
    base-class ``_get_source_obj()`` helper. No local shadow state.
    """

    _attr_has_entity_name = True
    entity_description: BydSensorDescription

    def __init__(
        self,
        coordinator: BydDataUpdateCoordinator,
        vin: str,
        vehicle: Vehicle,
        description: BydSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_translation_key = description.key
        self._vin = vin
        self._vehicle = vehicle
        self._attr_unique_id = f"{vin}_{description.source}_{description.key}"
        self._last_native_value: Any | None = None

        # Auto-disable sensors that return no data on first fetch.
        if description.entity_registry_enabled_default is not False:
            if self._resolve_validated_value() is None:
                self._attr_entity_registry_enabled_default = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_value(self) -> Any:
        """Extract the current value using the description's extraction logic."""
        key = self.entity_description.key

        # Timestamp sensors use the snapshot section's timestamp attribute.
        if key == "last_updated":
            realtime = self._get_realtime()
            if realtime is None:
                return None
            return _normalize_epoch(getattr(realtime, "timestamp", None))

        if key == "gps_last_updated":
            gps = self._get_gps()
            if gps is None:
                return None
            return _normalize_epoch(getattr(gps, "gps_timestamp", None))

        obj = self._get_source_obj(self.entity_description.source)
        if obj is None:
            return None

        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(obj)

        attr = self.entity_description.attr_key or key
        value = getattr(obj, attr, None)
        # BydEnum: return human-readable name instead of raw integer.
        # UNKNOWN/NO_DATA/UNAVAILABLE → None so HA shows "unavailable".
        if isinstance(value, BydEnum):
            return _enum_label(value)
        return value

    def _resolve_validated_value(self) -> Any:
        """Resolve sensor value and apply optional per-entity validation."""
        value = self._resolve_value()
        validator = self.entity_description.validator_fn
        if validator is not None:
            value = validator(self._last_native_value, value)
        if value is not None:
            self._last_native_value = value
        return value

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True when the coordinator has data for this source."""
        if self.entity_description.key in ("last_updated", "gps_last_updated"):
            return super().available and self._resolve_value() is not None
        return (
            super().available
            and self._get_source_obj(self.entity_description.source) is not None
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit; tire pressures resolve dynamically."""
        desc_unit = self.entity_description.native_unit_of_measurement
        if self.entity_description.key not in _TIRE_PRESSURE_KEYS:
            return desc_unit
        obj = self._get_source_obj(self.entity_description.source)
        if obj is not None:
            api_unit = getattr(obj, "tire_press_unit", None)
            if api_unit is not None:
                return _TIRE_UNIT_MAP.get(api_unit, desc_unit)
        return desc_unit

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self._resolve_validated_value()
