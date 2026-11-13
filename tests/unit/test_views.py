from mnogobase.core.config import DEFAULT_TYPE_LABELS, Settings
from mnogobase.service.views import type_catalog


def test_type_catalog_orders_config_types_first_then_unknown_by_count():
    s = Settings(extract={"entity_types": {"Person": "A human.", "Gadget": "A device."}})
    rows = type_catalog(s, {"Gadget": 2, "Alien": 1, "Zeta": 1, "Beta": 5})
    assert [r["type"] for r in rows] == ["Person", "Gadget", "Beta", "Alien", "Zeta"]
    assert rows[0] == {
        "type": "Person",
        "label": "Человек",
        "description": DEFAULT_TYPE_LABELS["Person"].description,
        "count": 0,
    }
    assert rows[1] == {"type": "Gadget", "label": "Gadget", "description": "A device.", "count": 2}
    assert rows[2] == {"type": "Beta", "label": "Beta", "description": "", "count": 5}
