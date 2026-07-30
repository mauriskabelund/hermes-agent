import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "docker" / "matrix_secret_entrypoint.py"
spec = importlib.util.spec_from_file_location("matrix_secret_entrypoint", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_load_env_treats_shell_metacharacters_as_plain_data(tmp_path):
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / "relay.env"
    env_file.write_text(
        "TOKEN=abc|literal;$HOME\n"
        f"RECOVERY_KEY=$(touch {marker})\n"
        "QUOTED='kept without quotes'\n",
        encoding="utf-8",
    )

    values = module.load_env(env_file)

    assert values["TOKEN"] == "abc|literal;$HOME"
    assert values["RECOVERY_KEY"] == f"$(touch {marker})"
    assert values["QUOTED"] == "kept without quotes"
    assert not marker.exists()


def test_load_env_rejects_invalid_key(tmp_path):
    env_file = tmp_path / "relay.env"
    env_file.write_text("BAD-KEY=value\n", encoding="utf-8")

    try:
        module.load_env(env_file)
    except ValueError as exc:
        assert "invalid" in str(exc)
    else:
        raise AssertionError("invalid key was accepted")
