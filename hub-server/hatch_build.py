"""Ship protocol/schema.json in a wheel built from the checkout (see pyproject)."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        schema = Path(self.root).parent / "protocol" / "schema.json"
        if schema.is_file():  # absent when building from the sdist
            build_data["force_include"][str(schema)] = "hub_server/schema.json"
