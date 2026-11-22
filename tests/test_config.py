"""Config loading: defaults, config.json, environment overrides."""
from bot.config import load_config


def test_the_per_run_limit_field_is_off_unless_the_env_turns_it_on():
    assert load_config(env={})["enable_per_run_limit"] is False
    for on in ("1", "true", "YES", " on "):
        assert load_config(env={"ALLOW_MAX_PER_RUN": on})["enable_per_run_limit"] is True
    for off in ("0", "false", "no"):
        assert load_config(env={"ALLOW_MAX_PER_RUN": off})["enable_per_run_limit"] is False
    assert "allow_max_per_run" not in load_config(env={"ALLOW_MAX_PER_RUN": "1"})        # one key, the one the form reads
