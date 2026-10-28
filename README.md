# Lion LVRT — Home Assistant integration

Home Assistant control and monitoring for the eight-channel Lion IRVT battery
tester (TIDA-010086 + ESP32 proxy).

Written for someone installing and operating this, not for someone reading the
firmware. The wire-level contracts live in the hardware repository's
[`Docs/`](https://github.com/ArakniD/Lion-IRVT/tree/master/Hardware/source/Docs).

This repository is also checked out as the `lion-lvrt-integration` submodule
of [Lion-IRVT](https://github.com/ArakniD/Lion-IRVT), beside the firmware it
mirrors. It installs and runs on its own; only the header cross-checks in the
test suite need the firmware alongside (see [Testing](#testing)).

---

## What you get

Per slot (eight of them), as their own Home Assistant devices:

| Kind | Entities |
|---|---|
| **Mode** | one `select` — idle / charge / discharge / test |
| **Buttons** | start test, stop, pause, resume, clear fault |
| **Measurements** | voltage, current (the ADS131M08 path the loop regulates on), cell temperature, charge moved, energy moved (the BTS's own counters), progress, elapsed |
| **Status** | state, fault, running, charging, paused, needs-operator, reverse polarity, calibrating, calibration valid, group follower, slot disabled |
| **Cell profile** | chemistry and cell model (dropdowns from the tester's own catalogue), capacity, charge and discharge C-rate - read from the tester and kept by it across a restart |
| **Limits** | voltage and current limits, cell temperature max |
| **Unit counters** | the BTS's own per-direction mAh / mWh / seconds |
| **Cell code** | the barcode scanned onto this slot, with the last cycle's figures as attributes |

Plus, for the unit: input voltage, unit state, uptime, online, WiFi, host
watchdog armed and its timeout, charge-allowed and discharge-allowed, an
abort-all button, and the four input-bus thresholds.

Services: `configure_slot`, `set_serial`, `set_mode`, `resume_slot`,
`calibration_command`, `get_result`, `sync_models`, `delete_model`,
`assign_code`, `clear_code`, `get_cycles`.

**Front-panel runs show up too.** The tester can also be driven from its own
rotary encoder and display. A charge or discharge started there reports the
slot state `direct` - a bare run the proxy supervises, with no test sequence -
and the Mode select shows it as charge or discharge. Stopping it from Home
Assistant works the same as stopping any other run.

---

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ArakniD&repository=ha-lion-irvt&category=integration)

The button opens HACS on your Home Assistant with this repository filled in.
Or, by hand:

1. **HACS → ⋮ → Custom repositories**.
2. Repository `https://github.com/ArakniD/ha-lion-irvt`, type **Integration**,
   **Add**.
3. Find **Lion LVRT Battery Tester** in HACS, **Download**, and restart Home
   Assistant.

HACS offers the tagged releases and the `main` branch. Take a release unless
you are testing a change: `main` moves with the firmware and can be ahead of
the tester you have.

Needs Home Assistant **2024.4** or later. The brand icon in
`custom_components/lion_lvrt/brand/` is shown from **2026.3**; older releases
show the generic integration icon and are otherwise unaffected.

### By hand

Copy `custom_components/lion_lvrt/` into your Home Assistant
`config/custom_components/` directory and restart.

The tester advertises as `BTS-Tester`, so Home Assistant discovers it
automatically if a Bluetooth adapter or proxy is in range — **Settings →
Devices & Services** will show it waiting to be configured.

### Bluetooth proxies

Nothing special is required. An ESPHome or Shelly Bluetooth proxy presents the
tester like any other device and `bleak-retry-connector` picks the adapter with
the best signal. The proxy must be a **connectable** one — a passive scanner
sees the advertisement but cannot be written to.

---

## Choosing a transport

This is the one decision worth understanding before you set up, because it
decides which controls appear.

The system has **two separate control surfaces**:

* **The test engine**, on the ESP32. `start` runs a whole characterisation —
  check rest, charge, rest, discharge, rest, optional recharge — and picks the
  direction itself.
* **The mode register**, on the C2000. Writing `eChX_Mode` commands the
  converter directly: run/stop and charge/discharge, nothing more.

"Test this cell" is the first. "Charge this slot" is the second. They are
reached differently:

| Transport | Test engine | Charge / discharge | Needs |
|---|:---:|:---:|---|
| **Bluetooth** | yes | only on firmware ≥ proto 4 | an adapter or proxy in range |
| **CAN** | no | yes | SocketCAN on the HA host, wired to the unit |
| **HTTP** | yes | yes | the tester joined to your WiFi |

Bluetooth is the default and works on a bench unit with no network. If your
proxy firmware predates `BLE_PROTO_VERSION` 4 it has no register
characteristic, so charge and discharge are **not offered in the mode
dropdown** — the integration reports what it can actually do rather than
listing an action that would fail. Two ways to get them back:

1. Add a CAN interface or the HTTP address during setup (both fields are
   optional and either one is enough), or
2. Flash the firmware in this repository, which adds characteristic `000d`.

A CAN-only setup is the mirror image: full converter control and configuration,
but no characterisation tests, because the test engine lives on the ESP32 and
CAN does not reach it.

---

## Safety behaviour you should know about

**The poll is a keepalive.** The unit pauses every running slot if no host
command arrives within its watchdog timeout (default 30 s). Home Assistant's
polling feeds that watchdog. If you widen the scan interval past the timeout
you will get a warning in the log — and eventually slots that pause on their
own.

**Some pauses need you, not an automation.** A slot paused because the
watchdog fired, or because the unit reset and restored the run from F-RAM,
comes back with `needs_operator` on. The resume *button* refuses it. That is
deliberate: both mean the unit lost contact with the host mid-run, and the cell
in the holder may not be the cell the counters belong to. To override, call the
service with the flag:

```yaml
action: lion_lvrt.resume_slot
data:
  slot: 0
  force: true
```

**Overrides only tighten.** Charge voltage, cutoff and temperature limits
set on the unit's front panel can only be more conservative than the cell's
own; the proxy clamps anything past them. Changing a slot's chemistry or model
- from Home Assistant or the panel - drops those overrides, because a voltage
chosen for one cell is wrong for another.

**The unit can refuse a command without reporting anything.** If the DC input
bus is outside its window, the firmware rejects a charge or discharge outright
and leaves the slot stopped with no error set anywhere. The integration checks
the unit state first and raises a message saying which state blocked it, rather
than letting the slot silently do nothing.

**Do not disable the host watchdog** (setting its timeout to 0) on anything
unattended. It is the only mechanism that stops the converters when the host
goes away mid-run. The integration allows it, because the firmware does and a
bench operator sometimes needs it, but it logs a warning every time.

---

## Things the hardware does that look like bugs

Worth knowing before you file one:

* **The over-current trip flag is never set.** Every hardware trip source is
  masked in this firmware build, so `binary_sensor.*_over_current_trip` stays
  off even during a software over-current. Use the **fault** sensor instead.
* **Voltage and current come from the 12-bit internal ADC**, not the 16-bit
  converter, despite what the register names suggest. Where a register
  transport is configured the 16-bit sense values are available too.
* **Result timestamps are not wall-clock.** `completed_uptime_s` is seconds of
  proxy uptime — the device has no real-time clock. Stamp your own on retrieval.
* **A charge total and a discharge total coexist.** Each direction's
  accumulator is zeroed only when that direction starts, never on stop or
  trip, so a slot that charged then discharged shows both.
* **Slots can be strapped into groups.** A follower has no control loop of its
  own and mirrors its leader; commanding it is refused. `group_follower` tells
  you which ones those are.

---

## Cycle events and cell barcodes

Every charge or discharge that finishes fires a
`lion_lvrt_cycle_completed` event carrying what the slot moved — and, if a
cell barcode has been scanned onto that slot, which cell it moved it for.
That is what makes the tester's output traceable to a physical battery.

### Scanning from a phone

Two scans: one of the label on the slot, one of the barcode on the cell.

```yaml
action: lion_lvrt.assign_code
data:
  slot_code: e465b810d93a-3     # scanned from the slot label
  code: CELL-00417              # scanned from the cell
```

The slot label carries the tester's Bluetooth address and the front-panel
slot number, so one call is unambiguous on a bench with several testers.
These forms all work, in any case, with or without separators:

```
e465b810d93a-3      E4:65:B8:10:D9:3A S3      BTS/e465b810d93a/SLOT3
```

A dashboard with a slot picker rather than a slot barcode can send `slot`
(zero-based) and `address` instead; with a single tester configured, `slot`
alone is enough. An explicit `slot` or `address` overrides whatever the
scanned label said, so a mislabelled socket can be corrected without
reprinting anything.

A barcode scanner card on a phone dashboard, or the companion app's own
scanner, fills these in. The slot number in a label is **1-based**, matching
the panel's silk-screen.

### What is recorded

The assignment is sticky. It stays until you scan a different cell onto that
slot or call `lion_lvrt.clear_code` — not until the end of a test, because a
characterisation runs a charge, a rest and a discharge, which are three
cycles on one cell. It also survives a Home Assistant restart.

`assign_code` also writes the code to the tester as that slot's serial, so it
reaches the unit's own stored result and the front panel. If the tester is
unreachable the assignment is still kept here and the response says the
serial did not land; pass `set_serial: false` to skip it.

Each event carries:

| Field | |
|---|---|
| `slot`, `slot_number` | zero-based index, and the panel's 1-based number |
| `direction` | `charge` or `discharge` |
| `outcome` | `complete`, `ended`, `phase`, `stopped`, `aborted` or `fault` |
| `mah`, `mwh`, `seconds` | the **unit's own** counters for that direction |
| `counter_source` | `bts`, or `proxy` on a build whose accumulators are never populated |
| `voltage_start_v`, `voltage_end_v`, `voltage_rested_v` | at the start, under load at the end, and after the current stopped |
| `voltage_min_v`, `voltage_max_v`, `temp_max_c`, `current_max_a` | the envelope seen during the run |
| `code`, `serial`, `model`, `chemistry`, `capacity_mah` | the cell this is filed under, and its profile |
| `timestamp`, `tester`, `address`, `entry_id` | when and which unit |

`outcome` is worth reading before trusting a figure. A characterisation's
charge phase ends as `phase`, which is normal; `fault` or `aborted` means
the run was cut short and the figures are partial.

The timestamp is Home Assistant's, not the tester's — the device has no
real-time clock and reports only uptime.

### Logging it

The event is in the recorder, so a logbook entry or a notification is an
ordinary automation:

```yaml
triggers:
  - trigger: event
    event_type: lion_lvrt_cycle_completed
conditions:
  - "{{ trigger.event.data.direction == 'discharge' }}"
  - "{{ trigger.event.data.outcome in ['complete', 'ended'] }}"
actions:
  - action: logbook.log
    data:
      name: "{{ trigger.event.data.tester }} slot {{ trigger.event.data.slot_number }}"
      message: >-
        {{ trigger.event.data.code or 'unidentified cell' }}
        discharged {{ trigger.event.data.mah }} mAh
        ({{ trigger.event.data.mwh }} mWh)
```

`lion_lvrt.get_cycles` returns the recent tail for a dashboard, filterable by
slot or by cell code. It is a bounded tail — the last 200 cycles per tester —
not an archive; the recorder holds the history.

### Scanning the cell afterwards

If the cell is scanned after the run rather than before, the cycle is logged
with no code. Attach one retrospectively with:

```yaml
action: lion_lvrt.assign_code
data:
  slot: 2
  code: CELL-00417
  apply_to_last_cycle: true
```

This only touches cycles that have no code, so a late scan can never
overwrite an attribution that was already right. The event that already
fired is unchanged — the relabel applies to the stored tail, so prefer
scanning before the run when the record matters.

---

## Custom cell models

The tester carries a built-in table of cells and stores up to **12 more** of
your own, in its flash. Once stored they appear in every slot's model list,
on the front panel and to any other host - not only in Home Assistant.

List them in `lion_lvrt_models.yaml` in your Home Assistant config folder:

```yaml
models:
  - name: VTC6A            # 1-7 characters: A-Z a-z 0-9 - _
    chemistry: nmc         # lco lto lfp nmc nca none
    capacity_mah: 3000     # required
    manufacturer: Sony     # optional
    charge_current_max_a: 6      # optional, 0 = chemistry default
    discharge_current_max_a: 30  # optional
    charge_v_max: 4.2            # optional, 0 = chemistry default
    discharge_v_min: 2.5         # optional
```

The file is uploaded when the integration starts, and again on
`lion_lvrt.sync_models`, which returns what it uploaded and any entry the
tester refused. A model that already matches is not re-sent.

* **Sync never deletes.** A model on the tester that is not in the file is
  left alone - a slot may be using it. Remove one with
  `lion_lvrt.delete_model`.
* **A built-in's name is refused**, so `VTC6` always means Sony's figures.
* **One bad entry does not stop the rest.** It is skipped and logged.

Uploading needs proxy firmware with `BLE_PROTO_VERSION` 8 over Bluetooth, or
the HTTP transport.

---

## Calibration

The bench procedure is in [`calibration-flow.md`](https://github.com/ArakniD/Lion-IRVT/tree/master/Hardware/source/Docs/calibration-flow.md).
From Home Assistant, each step is one service call that returns the live
telemetry, so you can watch a bench supply land inside the capture window:

```yaml
action: lion_lvrt.calibration_command
data:
  command: enter
  slot: 2
response_variable: cal
```

Then `capture_voltage` with the measured volts, `zero_current`,
`set_fixed_current` with a per-unit setpoint (0.0–0.8, where 1.0 pu ≈ 10 A),
`capture_current` with the measured amps, and `compute_save`.

Only `enter` takes a slot; every other command applies to whichever slot the
unit already holds. A reading between 0.2 and 0.8 pu is refused with
`pu_range` — the firmware cannot tell which capture point you meant.

A slot in calibration drops its reference after 120 s with no command, so
don't leave the procedure half-finished.

---

## Testing

```bash
python -m pip install -r requirements-test.txt
python -m pytest
```

The suite runs against a firmware-accurate simulator — no hardware needed. The
simulator deliberately reproduces the behaviours that break clients: the shared
slot-select cursor, exact write-length checks, refusals that carry no reason,
read-only register writes that are silently dropped, MTU truncation, and the
host watchdog.

The layout tests compare the Python codecs against the firmware's own C
headers - `registers.h` on the C2000 and `ble_proto.h` / `bts_regs.h` on the
ESP32. They find them in this order:

1. `LION_IRVT_SOURCE`, if set, naming a Lion-IRVT `Hardware/source` checkout.
2. The parent directory, which is right when this is the submodule.

With neither, those tests **skip** and say where they looked. A standalone
clone therefore passes with them skipped - so run the full suite from the
submodule, or set the variable, before trusting a change to a wire format:

```bash
LION_IRVT_SOURCE=../Lion-IRVT/Hardware/source python -m pytest
```

Two tiers:

* **Protocol, transport and device tests** import no Home Assistant and run on
  **Python 3.10+**.
* **Entity, service and config-flow tests** need Home Assistant, which needs
  **Python 3.12+**. On an older interpreter they skip with a message saying so
  rather than silently passing.

The test worth knowing about is `test_layouts_match_firmware_header`: it parses
`ble_proto.h` and compares every struct field-by-field against the Python
decoder. Every client bug on record in this project was a silent layout drift
that behavioural tests could not catch, because a dry run never serialises a
byte.

---

## Layout

```
custom_components/lion_lvrt/
  protocol/         wire codecs — no Home Assistant imports
    ble.py            little-endian GATT records
    registers.py      big-endian register map v2.1
    can.py            mixed-endian CAN frames
  transport/        one class per link
    base.py           capability interface
    ble.py  can.py  http.py
  device.py         safety policy, transport selection
  profile.py        per-slot cell profile entities
  models_file.py    the custom cell models YAML
  coordinator.py    polling + notification fan-out
  cycles.py         completed-cycle detection, no Home Assistant imports
  assignments.py    cell codes per slot, and the recent cycle tail
  sensor.py  binary_sensor.py  select.py  button.py  number.py
  config_flow.py  services.py  const.py
tests/
  simulator/        the fake unit
  test_protocol.py  test_registers.py  test_transport.py
  test_device.py    test_homeassistant.py  test_models.py
  test_cycles.py
```
