from mnogobase import device


def test_explicit_preference_wins(monkeypatch):
    monkeypatch.setattr(device, "_cuda_available", lambda: True)
    assert device.detect_device("cpu") == "cpu"


def test_auto_prefers_cuda_then_mps(monkeypatch):
    monkeypatch.setattr(device, "_cuda_available", lambda: True)
    monkeypatch.setattr(device, "_mps_available", lambda: True)
    assert device.detect_device("auto") == "cuda"
    monkeypatch.setattr(device, "_cuda_available", lambda: False)
    assert device.detect_device("auto") == "mps"
    monkeypatch.setattr(device, "_mps_available", lambda: False)
    assert device.detect_device("auto") == "cpu"
