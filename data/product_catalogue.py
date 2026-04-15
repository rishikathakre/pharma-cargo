"""
Product catalogue loader.

Source of truth: data/raw/product_catalogue.json
This module intentionally does not depend on config.py so the catalogue can be
used as the canonical product stability + financial reference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ProductProfile:
    product_id: str
    name: str
    cold_chain_type: str
    temp_min_c: float
    temp_max_c: float
    excursion_max_hours: float
    excursion_max_temp_c: float
    doses_per_container: int
    value_per_dose_usd: float

    manufacturer: str = ""
    regulatory_ref: str = ""
    therapeutic_class: str = ""


_CATALOGUE_CACHE: Optional[Dict[str, ProductProfile]] = None


def _catalogue_path() -> Path:
    # repo_root/data/raw/product_catalogue.json
    return Path(__file__).resolve().parents[1] / "raw" / "product_catalogue.json"


def load_product_catalogue(force_reload: bool = False) -> Dict[str, ProductProfile]:
    """Load catalogue from JSON into a dict keyed by product_id."""
    global _CATALOGUE_CACHE
    if _CATALOGUE_CACHE is not None and not force_reload:
        return _CATALOGUE_CACHE

    path = _catalogue_path()
    if not path.exists():
        _CATALOGUE_CACHE = {}
        return _CATALOGUE_CACHE

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("product_catalogue.json must be a JSON list")

    out: Dict[str, ProductProfile] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("product_id") or "").strip()
        if not pid:
            continue
        out[pid] = ProductProfile(
            product_id=pid,
            name=str(item.get("name") or pid),
            cold_chain_type=str(item.get("cold_chain_type") or "refrigerated"),
            temp_min_c=float(item.get("temp_min_c")),
            temp_max_c=float(item.get("temp_max_c")),
            excursion_max_hours=float(item.get("excursion_max_hours")),
            excursion_max_temp_c=float(item.get("excursion_max_temp_c")),
            doses_per_container=int(float(item.get("doses_per_container") or 0) or 0),
            value_per_dose_usd=float(item.get("value_per_dose_usd") or 0.0),
            manufacturer=str(item.get("manufacturer") or ""),
            regulatory_ref=str(item.get("regulatory_ref") or ""),
            therapeutic_class=str(item.get("therapeutic_class") or ""),
        )

    _CATALOGUE_CACHE = out
    return out


def get_product_profile(product_id: Optional[str]) -> Optional[ProductProfile]:
    """Return a profile for product_id, or None if not found."""
    if not product_id:
        return None
    return load_product_catalogue().get(product_id)


def get_default_product_id() -> str:
    """
    Choose a reasonable default product_id for simulation when none is set.
    Prefers VACC-STANDARD if present, otherwise first catalogue entry, otherwise 'VACC-STANDARD'.
    """
    cat = load_product_catalogue()
    if "VACC-STANDARD" in cat:
        return "VACC-STANDARD"
    if cat:
        return next(iter(cat.keys()))
    return "VACC-STANDARD"

