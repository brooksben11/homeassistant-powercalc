from __future__ import annotations

import logging
import re
from typing import Optional

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, SOURCE_USER
from homeassistant.const import CONF_ENTITY_ID, CONF_PLATFORM
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery, discovery_flow
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.typing import ConfigType

from .aliases import MANUFACTURER_ALIASES, MANUFACTURER_WLED
from .common import SourceEntity, create_source_entity
from .const import (
    CONF_MANUFACTURER,
    CONF_MODE,
    CONF_MODEL,
    DISCOVERY_POWER_PROFILE,
    DISCOVERY_SOURCE_ENTITY,
    DISCOVERY_TYPE,
    DOMAIN,
    CalculationStrategy,
    PowercalcDiscoveryType,
)
from .errors import ModelNotSupported
from .power_profile.factory import get_power_profile
from .power_profile.library import ModelInfo
from .power_profile.power_profile import DEVICE_DOMAINS, PowerProfile

_LOGGER = logging.getLogger(__name__)


async def autodiscover_model(
    hass: HomeAssistant, entity_entry: er.RegistryEntry | None
) -> ModelInfo | None:
    """Try to auto discover manufacturer and model from the known device information"""
    if not entity_entry:
        return None

    if not await has_manufacturer_and_model_information(hass, entity_entry):
        _LOGGER.debug(
            "%s: Cannot autodiscover model, manufacturer or model unknown from device registry",
            entity_entry.entity_id,
        )
        return None

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(entity_entry.device_id)
    model_id = device_entry.model

    manufacturer = device_entry.manufacturer
    if MANUFACTURER_ALIASES.get(manufacturer):
        manufacturer = MANUFACTURER_ALIASES.get(manufacturer)

    # Make sure we don't have a literal / in model_id, so we don't get issues with sublut directory matching down the road
    # See github #658
    model_id = str(model_id).replace("/", "#slash#")

    model_info = ModelInfo(manufacturer, model_id)

    _LOGGER.debug(
        "%s: Auto discovered model (manufacturer=%s, model=%s)",
        entity_entry.entity_id,
        model_info.manufacturer,
        model_info.model,
    )
    return model_info


async def has_manufacturer_and_model_information(
    hass: HomeAssistant, entity_entry: er.RegistryEntry
) -> bool:
    """See if we have enough information in device registry to automatically setup the power sensor"""

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(entity_entry.device_id)
    if device_entry is None:
        return False

    if len(str(device_entry.manufacturer)) == 0 or len(str(device_entry.model)) == 0:
        return False

    return True


class DiscoveryManager:
    """
    This class is responsible for scanning the HA instance for entities and their manufacturer / model info
    It checks if any of these devices is supported in the powercalc library
    When entities are found it will dispatch a discovery flow, so the user can add them to their HA instance
    """

    def __init__(self, hass: HomeAssistant, ha_config: ConfigType):
        self.hass = hass
        self.ha_config = ha_config
        self.manually_configured_entities: list[str] | None = None

    async def start_discovery(self) -> None:
        """Start the discovery procedure"""

        _LOGGER.debug("Start auto discovering entities")
        entity_registry = er.async_get(self.hass)
        for entity_entry in list(entity_registry.entities.values()):
            if not self.should_process_entity(entity_entry):
                continue

            model_info = await autodiscover_model(self.hass, entity_entry)
            if not model_info or not model_info.manufacturer or not model_info.model:
                continue

            source_entity = await create_source_entity(
                entity_entry.entity_id, self.hass
            )

            if (
                model_info.manufacturer == MANUFACTURER_WLED
                and entity_entry.domain == LIGHT_DOMAIN
                and not re.search(
                    "master|segment",
                    str(entity_entry.original_name),
                    flags=re.IGNORECASE,
                )
            ):
                self._init_entity_discovery(
                    source_entity,
                    power_profile=None,
                    extra_discovery_data={
                        CONF_MODE: CalculationStrategy.WLED,
                        CONF_MANUFACTURER: model_info.manufacturer,
                        CONF_MODEL: model_info.model,
                    },
                )
                continue

            try:
                power_profile = await get_power_profile(
                    self.hass, {}, model_info=model_info
                )
            except ModelNotSupported:
                _LOGGER.debug(
                    "%s: Model not found in library, skipping discovery",
                    entity_entry.entity_id,
                )
                continue

            if not power_profile.is_entity_domain_supported(source_entity):
                continue

            self._init_entity_discovery(source_entity, power_profile, {})

        _LOGGER.debug("Done auto discovering entities")

    def should_process_entity(self, entity_entry: er.RegistryEntry) -> bool:
        """Do some validations on the registry entry to see if it qualifies for discovery"""
        if entity_entry.disabled:
            return False

        if entity_entry.domain not in DEVICE_DOMAINS.values():
            return False

        if entity_entry.entity_category in [
            EntityCategory.CONFIG,
            EntityCategory.DIAGNOSTIC,
        ]:
            return False

        has_user_config = self._is_user_configured(entity_entry.entity_id)
        if has_user_config:
            _LOGGER.debug(
                "%s: Entity is manually configured, skipping auto configuration",
                entity_entry.entity_id,
            )
            return False

        return True

    @callback
    def _init_entity_discovery(
        self,
        source_entity: SourceEntity,
        power_profile: PowerProfile | None,
        extra_discovery_data: Optional[dict],
    ) -> None:
        """Dispatch the discovery flow for a given entity"""
        existing_entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.unique_id
            in [source_entity.unique_id, f"pc_{source_entity.unique_id}"]
        ]
        if existing_entries:
            _LOGGER.debug(
                f"{source_entity.entity_id}: Already setup with discovery, skipping new discovery"
            )
            return

        discovery_data = {
            CONF_ENTITY_ID: source_entity.entity_id,
            DISCOVERY_SOURCE_ENTITY: source_entity,
        }

        if power_profile:
            discovery_data[DISCOVERY_POWER_PROFILE] = power_profile
            discovery_data[CONF_MANUFACTURER] = power_profile.manufacturer
            discovery_data[CONF_MODEL] = power_profile.model

        if extra_discovery_data:
            discovery_data.update(extra_discovery_data)

        discovery_flow.async_create_flow(
            self.hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data=discovery_data,
        )

        # Code below if for legacy discovery routine, will be removed somewhere in the future
        if power_profile and not power_profile.is_additional_configuration_required:
            discovery_info = {
                CONF_ENTITY_ID: source_entity.entity_id,
                DISCOVERY_SOURCE_ENTITY: source_entity,
                DISCOVERY_POWER_PROFILE: power_profile,
                DISCOVERY_TYPE: PowercalcDiscoveryType.LIBRARY,
            }
            self.hass.async_create_task(
                discovery.async_load_platform(
                    self.hass, SENSOR_DOMAIN, DOMAIN, discovery_info, self.ha_config
                )
            )

    def _is_user_configured(self, entity_id: str) -> bool:
        """
        Check if user have setup powercalc sensors for a given entity_id.
        Either with the YAML or GUI method.
        """
        if not self.manually_configured_entities:
            self.manually_configured_entities = (
                self._load_manually_configured_entities()
            )

        return entity_id in self.manually_configured_entities

    def _load_manually_configured_entities(self) -> list[str]:
        """Looks at the YAML and GUI config entries for all the configured entity_id's"""
        entities = []

        # Find entity ids in yaml config
        if SENSOR_DOMAIN in self.ha_config:
            sensor_config = self.ha_config.get(SENSOR_DOMAIN)
            platform_entries = [
                item
                for item in sensor_config
                if isinstance(item, dict) and item.get(CONF_PLATFORM) == DOMAIN
            ]
            for entry in platform_entries:
                entities.extend(self._find_entity_ids_in_yaml_config(entry))

        # Add entities from existing config entries
        entities.extend(
            [
                entry.data.get(CONF_ENTITY_ID)
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.source == SOURCE_USER
            ]
        )

        return entities

    def _find_entity_ids_in_yaml_config(self, search_dict: dict):
        """
        Takes a dict with nested lists and dicts,
        and searches all dicts for a key of the field
        provided.
        """
        found_entity_ids = []

        for key, value in search_dict.items():
            if key == "entity_id":
                found_entity_ids.append(value)

            elif isinstance(value, dict):
                results = self._find_entity_ids_in_yaml_config(value)
                for result in results:
                    found_entity_ids.append(result)

            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        results = self._find_entity_ids_in_yaml_config(item)
                        for result in results:
                            found_entity_ids.append(result)

        return found_entity_ids
