"""Hard-coded adapters, one for each supported Huawei service."""

from .registry import get_service_adapter, registered_services

__all__ = ["get_service_adapter", "registered_services"]
