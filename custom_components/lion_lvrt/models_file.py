"""Custom cell models from a YAML file, uploaded to the tester.

The proxy holds up to twelve user-defined models in its own flash, beside the
built-in table, so a model added here is available to the front panel and
every host - not only to Home Assistant. This file is the operator's source
for them:

    <config>/lion_lvrt_models.yaml

    models:
      - name: VTC6A            # 1-7 characters: A-Z a-z 0-9 - _
        chemistry: nmc         # lco lto lfp nmc nca none
        capacity_mah: 3000     # required
        manufacturer: Sony     # optional
        charge_current_max_a: 6      # optional, 0 = chemistry default
        discharge_current_max_a: 30  # optional
        charge_v_max: 4.2            # optional, 0 = chemistry default
        discharge_v_min: 2.5         # optional

It is read at start-up and by the ``lion_lvrt.sync_models`` service. Syncing
only adds and updates: a model on the tester that is not in the file is left
alone, because a slot may be using it or another host may have added it.
Delete one explicitly with ``lion_lvrt.delete_model``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .const import CellChemistry
from .protocol.ble import CUSTOM_MODEL_MAX, CellModel, ProtocolError

_LOGGER = logging.getLogger(__name__)

MODELS_FILENAME = "lion_lvrt_models.yaml"

_NUMERIC = (
    "capacity_mah",
    "charge_current_max_a",
    "discharge_current_max_a",
    "charge_v_max",
    "discharge_v_min",
)


class ModelFileError(ValueError):
    """The models file could not be used; the message says why."""


def parse_models(data: Any) -> tuple[list[CellModel], list[str]]:
    """Turn the parsed YAML into models, collecting a message per bad entry.

    A bad entry is skipped rather than failing the whole file, so one typo
    does not stop the other eleven from uploading.
    """
    if data is None:
        return [], []
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ModelFileError("expected a top-level 'models:' list")

    models: list[CellModel] = []
    problems: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(data["models"], start=1):
        where = f"entry {i}"
        if not isinstance(raw, dict):
            problems.append(f"{where}: not a mapping")
            continue
        name = str(raw.get("name", "")).strip()
        where = f"entry {i} ({name or 'no name'})"
        chem_raw = str(raw.get("chemistry", "")).strip().upper()
        try:
            chemistry = CellChemistry[chem_raw]
        except KeyError:
            problems.append(
                f"{where}: chemistry must be one of "
                f"{', '.join(c.name.lower() for c in CellChemistry)}"
            )
            continue
        try:
            figures = {k: float(raw.get(k, 0.0) or 0.0) for k in _NUMERIC}
        except (TypeError, ValueError):
            problems.append(f"{where}: a numeric field is not a number")
            continue
        model = CellModel(
            name=name,
            chemistry=chemistry,
            manufacturer=str(raw.get("manufacturer", "") or "").strip(),
            **figures,
        )
        try:
            model.validate()
        except ProtocolError as err:
            problems.append(f"{where}: {err}")
            continue
        if name.upper() in seen:
            problems.append(f"{where}: duplicate name, later entry ignored")
            continue
        seen.add(name.upper())
        models.append(model)

    if len(models) > CUSTOM_MODEL_MAX:
        problems.append(
            f"{len(models)} models listed but the tester stores {CUSTOM_MODEL_MAX}; "
            f"only the first {CUSTOM_MODEL_MAX} are used"
        )
        models = models[:CUSTOM_MODEL_MAX]
    return models, problems


def load_models_file(config_dir: str) -> tuple[list[CellModel], list[str]]:
    """Read and parse the models file. A missing file is simply no models.

    Blocking (file I/O and YAML parsing) - call it in the executor.
    """
    path = Path(config_dir) / MODELS_FILENAME
    if not path.is_file():
        return [], []
    import yaml  # noqa: PLC0415  (bundled with Home Assistant)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ModelFileError(f"{path.name} could not be read: {err}") from err
    return parse_models(data)
