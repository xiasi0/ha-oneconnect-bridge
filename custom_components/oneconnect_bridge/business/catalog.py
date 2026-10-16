"""OneConnect Bridge 内置产品目录."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import yaml
from .models import ProductSpec

CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.yaml"
_REQUIRED_FIELDS = frozenset(
    {
        "sub_prod_id",
        "device_type",
        "manufacturer",
        "model",
        "device_name",
        "protocol_type",
        "setup_type",
    }
)

class CatalogError(ValueError):
    """Raised when the bundled product catalog is invalid."""

@lru_cache(maxsize=8)
def load_catalog(path: Path = CATALOG_PATH) -> dict[str, ProductSpec]:
    """目录为打包静态文件，按路径缓存；返回共享实例，调用方不得修改。"""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(f"unable to read product catalog {path}: {error}") from error

    if not isinstance(value, dict) or value.get("schema") != 1:
        raise CatalogError("product catalog must have schema: 1")
    products = value.get("products")
    if not isinstance(products, dict) or not products:
        raise CatalogError("product catalog has no products")

    result: dict[str, ProductSpec] = {}
    for prod_id, raw in products.items():
        if not isinstance(prod_id, str) or not prod_id:
            raise CatalogError("product catalog contains an invalid product id")
        if not isinstance(raw, dict):
            raise CatalogError(f"product {prod_id} must be a mapping")
        missing = _REQUIRED_FIELDS - raw.keys()
        if missing:
            raise CatalogError(
                f"product {prod_id} is missing fields: {', '.join(sorted(missing))}"
            )
        if not all(isinstance(raw[field], str) and raw[field] for field in (
            "sub_prod_id",
            "device_type",
            "manufacturer",
            "model",
            "device_name",
        )):
            raise CatalogError(f"product {prod_id} has an invalid text field")
        if not isinstance(raw["protocol_type"], int) or not isinstance(
            raw["setup_type"], int
        ):
            raise CatalogError(f"product {prod_id} has an invalid numeric field")
        result[prod_id] = ProductSpec(
            prod_id=prod_id,
            sub_prod_id=raw["sub_prod_id"],
            device_type=raw["device_type"],
            manufacturer=raw["manufacturer"],
            model=raw["model"],
            device_name=raw["device_name"],
            protocol_type=raw["protocol_type"],
            setup_type=raw["setup_type"],
        )
    return result