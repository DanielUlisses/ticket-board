from conftest import SCHEMA

from hub_server import PROTOCOL_VERSION


def test_hub_speaks_the_schemas_protocol_version():
    assert PROTOCOL_VERSION == SCHEMA["x-protocol-version"]
