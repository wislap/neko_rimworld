import json
from pathlib import Path

import pytest
from plugin.plugins.neko_rimworld.game import protocol

VECTORS = json.loads((Path(__file__).parent / "fixtures/colony-events-v1/vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS["valid"])
def test_native_valid_vectors(vector):
    direction = (protocol.Direction.PLUGIN_TO_MOD if vector["type"] in {"policy_request", "write_request"}
                 else protocol.Direction.MOD_TO_PLUGIN)
    parsed = protocol.decode_envelope(json.dumps(vector).encode(), direction=direction, authenticated=True)
    assert parsed.to_dict() == vector


@pytest.mark.parametrize("vector", VECTORS["invalid"])
def test_native_invalid_vectors(vector):
    direction = (protocol.Direction.MOD_TO_PLUGIN if vector["type"] == "game_event"
                 else protocol.Direction.PLUGIN_TO_MOD)
    with pytest.raises(protocol.ValidationError):
        protocol.decode_envelope(json.dumps(vector).encode(), direction=direction, authenticated=True)
