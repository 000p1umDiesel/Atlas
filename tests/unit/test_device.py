from mnogobase import device


def test_explicit_preference_wins(monkeypatch):
    monkeypatch.setattr(device, "_mps_available", lambda: True)
    assert device.detect_device("cpu") == "cpu"


def test_auto_prefers_mps_then_cpu(monkeypatch):
    monkeypatch.setattr(device, "_mps_available", lambda: True)
    assert device.detect_device("auto") == "mps"
    monkeypatch.setattr(device, "_mps_available", lambda: False)
    assert device.detect_device("auto") == "cpu"
