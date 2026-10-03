"""Safe YAML/JSON loading; one source selects contracts and auditor pipeline."""
import hashlib
import json
from pathlib import Path
import yaml
from .auditors import Pipeline
from .policy import Policy


class UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("configuration aliases are not supported")
        return super().compose_node(parent, index)


def mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError("duplicate config key")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def load(path):
    text = Path(path).read_text()
    if len(text) > 1_000_000:
        raise ValueError("config too large")
    config = yaml.load(text, Loader=UniqueLoader)
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    if set(config) == {"runs"}:
        return Policy(config), Pipeline([])  # Backward-compatible initial policy.
    if set(config) != {"schema_version", "runs", "auditors"} or type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported configuration schema")
    policy = Policy({"runs": config["runs"]})
    pipeline = Pipeline(config["auditors"])
    policy.version = hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return policy, pipeline
