"""OneConnect Bridge 业务层配置与设备模型."""
from .catalog import CATALOG_PATH, CatalogError, load_catalog
from .child_adapters import SUPPORTED_PRODUCT_REFS
from .child_binding import (
    BindResult,
    ChildView,
    bind_entities,
    children_view,
    remove_children,
    update_child,
)
from .mapping_config import (
    MappingConfigError,
    load_mapping_if_present,
)
from .models import (
    BindingField,
    ChildPlan,
    GatewayPlan,
    ProductSpec,
    ServiceRef,
)

__all__ = [
    "BindResult",
    "BindingField",
    "CATALOG_PATH",
    "CatalogError",
    "ChildPlan",
    "ChildView",
    "GatewayPlan",
    "MappingConfigError",
    "ProductSpec",
    "ServiceRef",
    "SUPPORTED_PRODUCT_REFS",
    "bind_entities",
    "children_view",
    "load_catalog",
    "load_mapping_if_present",
    "remove_children",
    "update_child",
]