"""Custom cell models: the YAML file, the wire record and the sync.

The file is the operator's source; the proxy holds at most twelve models with
names of up to seven characters, and refuses a built-in's name. These tests
pin that a bad entry is reported and skipped rather than sinking the file,
and that sync only uploads what differs and never deletes.
"""

from __future__ import annotations

import struct

import pytest

from custom_components.lion_lvrt.const import CellChemistry
from custom_components.lion_lvrt.device import LionDevice
from custom_components.lion_lvrt.models_file import ModelFileError, parse_models
from custom_components.lion_lvrt.protocol import ble as proto
from custom_components.lion_lvrt.protocol.ble import CellModel, ProtocolError
from custom_components.lion_lvrt.transport.base import Transport


def test_parse_good_file() -> None:
    models, problems = parse_models(
        {
            "models": [
                {
                    "name": "VTC6A",
                    "chemistry": "nmc",
                    "capacity_mah": 4000,
                    "manufacturer": "Sony",
                    "charge_current_max_a": 6,
                    "discharge_current_max_a": 30,
                }
            ]
        }
    )
    assert problems == []
    assert len(models) == 1
    m = models[0]
    assert m.name == "VTC6A"
    assert m.chemistry is CellChemistry.NMC
    assert m.capacity_mah == 4000
    assert m.discharge_current_max_a == 30
    assert m.charge_v_max == 0.0


def test_parse_skips_bad_entries_and_keeps_good_ones() -> None:
    models, problems = parse_models(
        {
            "models": [
                {"name": "TOOLONGNAME", "chemistry": "nmc", "capacity_mah": 1},
                {"name": "OK1", "chemistry": "lfp", "capacity_mah": 1500},
                {"name": "BAD", "chemistry": "lipo", "capacity_mah": 1},
                {"name": "NOCAP", "chemistry": "nmc"},
                {"name": "ok1", "chemistry": "nmc", "capacity_mah": 1},
                {"name": "VOLT", "chemistry": "nmc", "capacity_mah": 1,
                 "charge_v_max": 3.0, "discharge_v_min": 3.5},
            ]
        }
    )
    assert [m.name for m in models] == ["OK1"]
    assert len(problems) == 5
    assert any("1-7 characters" in p for p in problems)
    assert any("chemistry must be" in p for p in problems)
    assert any("capacity_mah" in p for p in problems)
    assert any("duplicate" in p for p in problems)
    assert any("below charge_v_max" in p for p in problems)


def test_parse_caps_at_twelve() -> None:
    entries = [
        {"name": f"M{i}", "chemistry": "nmc", "capacity_mah": 1000}
        for i in range(14)
    ]
    models, problems = parse_models({"models": entries})
    assert len(models) == proto.CUSTOM_MODEL_MAX
    assert any("stores 12" in p for p in problems)


def test_parse_rejects_wrong_shape() -> None:
    with pytest.raises(ModelFileError):
        parse_models({"cells": []})
    assert parse_models(None) == ([], [])


def test_model_record_layout() -> None:
    """56 bytes, matching ble_model_cmd_t's _Static_assert in the firmware."""
    m = CellModel(
        name="VTC6A",
        chemistry=CellChemistry.NMC,
        capacity_mah=3000,
        charge_current_max_a=6,
        discharge_current_max_a=30,
        manufacturer="Sony",
    )
    raw = m.encode_save()
    assert len(raw) == 56
    f = struct.unpack(proto.MODEL_CMD_FMT, raw)
    assert f[0] == proto.MODEL_OP_SAVE
    assert f[1] == int(CellChemistry.NMC)
    assert f[3] == 3000
    assert f[5] == 30
    assert f[8].rstrip(b"\0") == b"VTC6A"

    delete = proto.encode_model_delete("VTC6A")
    assert len(delete) == 56
    assert delete[0] == proto.MODEL_OP_DELETE


def test_model_name_rules() -> None:
    with pytest.raises(ProtocolError):
        CellModel(name="VTC 6", chemistry=CellChemistry.NMC, capacity_mah=1).validate()
    with pytest.raises(ProtocolError):
        CellModel(name="", chemistry=CellChemistry.NMC, capacity_mah=1).validate()
    CellModel(name="A-1_b", chemistry=CellChemistry.LFP, capacity_mah=1).validate()


class _CatalogTransport(Transport):
    """Just enough of a transport to exercise the device's model sync."""

    supports_test_engine = True

    def __init__(self, models: list[CellModel]) -> None:
        self.models = list(models)
        self.saved: list[str] = []

    async def async_connect(self) -> None:
        return None

    async def async_disconnect(self) -> None:
        return None

    async def async_poll(self):  # pragma: no cover - not used here
        raise NotImplementedError

    async def async_read_models(self) -> list[CellModel]:
        return list(self.models)

    async def async_save_model(self, model: CellModel) -> None:
        self.saved.append(model.name)
        self.models = [m for m in self.models if m.name != model.name]
        self.models.append(model)


async def test_sync_uploads_only_what_differs_and_never_deletes() -> None:
    builtin = CellModel(name="VTC6", chemistry=CellChemistry.NMC,
                        capacity_mah=3000, custom=False)
    same = CellModel(name="KEEP", chemistry=CellChemistry.NMC, capacity_mah=2000)
    stale = CellModel(name="EDIT", chemistry=CellChemistry.NMC, capacity_mah=1000)
    other = CellModel(name="OTHER", chemistry=CellChemistry.LFP, capacity_mah=500)
    transport = _CatalogTransport([builtin, same, stale, other])
    device = LionDevice(transport)

    wanted = [
        CellModel(name="KEEP", chemistry=CellChemistry.NMC, capacity_mah=2000),
        CellModel(name="EDIT", chemistry=CellChemistry.NMC, capacity_mah=1100),
        CellModel(name="NEW", chemistry=CellChemistry.NCA, capacity_mah=3500),
        CellModel(name="VTC6", chemistry=CellChemistry.NMC, capacity_mah=1),
    ]
    uploaded, problems = await device.async_sync_models(wanted)

    assert sorted(transport.saved) == ["EDIT", "NEW"]
    assert uploaded == 2
    assert any("built-in" in p for p in problems)
    # OTHER was not in the file and is still on the tester.
    assert any(m.name == "OTHER" for m in device.models)
