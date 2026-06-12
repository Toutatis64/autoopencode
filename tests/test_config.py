import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_autocode_yaml_is_valid():
    path = ROOT / "autocode.yaml"
    assert path.exists()
    with open(path) as f:
        cfg = yaml.safe_load(f)
    assert cfg["project"]["type"] == "python"
    assert "validation" in cfg
    assert "modules" in cfg
    assert isinstance(cfg["modules"], list)
    for m in cfg["modules"]:
        assert "name" in m
        assert "keywords" in m


def test_opencode_json_is_valid():
    path = ROOT / "opencode.json"
    assert path.exists()
    with open(path) as f:
        cfg = __import__("json").load(f)
    assert isinstance(cfg, dict)
