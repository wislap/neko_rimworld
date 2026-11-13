import json
from pathlib import Path

import pytest
from plugin.plugins.neko_rimworld.game.protocol import ValidationError, decode_envelope

VECTORS = json.loads((Path(__file__).parent / "fixtures/colony-write-v1/vectors.json").read_text())


@pytest.mark.parametrize("vector", VECTORS["valid"])
def test_valid(vector):
    assert decode_envelope(json.dumps(vector).encode()).type == vector["type"]


@pytest.mark.parametrize("vector", VECTORS["invalid"])
def test_invalid(vector):
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(vector).encode())
