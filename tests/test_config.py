from pathlib import Path

from policy_agent.collectors.registry import COLLECTORS
from policy_agent.config import load_agent_config


def test_default_config_uses_known_collectors() -> None:
    config = load_agent_config(Path("config/sources.yml"))
    assert len(config.sources) >= 20
    unknown = {source["collector"] for source in config.sources} - set(COLLECTORS)
    assert not unknown
    assert all(source["url"].startswith("https://") for source in config.sources)
