# Serial protocol v1

This is the contract between the Mega firmware (`firmware/vsbc_firmware`) and the
Python host (`vsbc/`). The simulator in `vsbc/sim.py` implements the same
protocol, so any change here must be made in all three places.

- USB serial, **115200 baud**, 8N1, ASCII.
- Every line ends with a newline (`\n`, `\r` or `\r\n` all work). A line typed
  without any line ending is accepted after 100 ms of silence, so the Arduino
  Serial Monitor works with any line-ending setting.
- Opening the port resets the Mega (DTR). Wait for `EVT READY` (about 2 s on a
  real board) before sending commands.

## Line types (Mega → host)

| Starts with | Meaning |
|---|---|
| `OK <CMD> [key=value ...]` | Command succeeded |
| `ERR <CMD> <CODE> <message>` | Command failed |
| `EVT <NAME> [key=value ...]` | Something happened on its own (move ended, load cell lost...) |
| `D <t_ms> <pos_mm> <load_N> <raw> <state>` | One data sample |
| `# <text>` | Human-readable text (menu, hints). Hosts ignore it. |

Every command gets **exactly one** `OK` or `ERR` line, with the canonical command
name (never the alias). Most replies are immediate; `TARE` and `CAL` reply when
their averaging is finished. `EVT` and `D` lines can appear between a command
and its reply.

Values are plain numbers; unknown or missing values print as `nan`.

## Data lines

```
D <t_ms> <pos_mm> <load_N> <raw> <state>
D 123456 12.3450 87.512 1419842 R
```

| Field | Meaning |
|---|---|
| `t_ms` | Mega `millis()` when the sample was read (use this, not the PC clock) |
| `pos_mm` | Crosshead position in mm, 4 decimals (+ = tension) |
| `load_N` | Load in N, 3 decimals; `nan` when the load cell is missing or not calibrated; `inf`/`-inf` if out of printable range |
| `raw` | Load-cell ADC counts (0 when no load cell) |
| `state` | `I` idle, `M` manual move (`MOVE`/`GOTO`/`HOME`), `R` test run (`RUN`) |

- `STREAM ON`: one line per load-cell sample (10 or 80 per second, depending on
  the HX711 RATE pin), idle or moving. Without a load cell: one line every 100 ms.
- `STREAM OFF` (the default at power-up): one line every 500 ms, only while moving.
- If the serial transmit buffer is full the line is skipped, never delayed.
  Skipped lines are counted in `STATUS drops=`.

## Commands

Commands are case-insensitive. Arguments are separated by spaces. A one-letter
alias can be followed directly by its number: `v0.5` = `RATE 0.5`, `m-5` = `MOVE -5`.

**While a move is running**, only the commands marked *any time* are carried out.
Any other line, including an unknown one, **stops the move** and gets
`ERR <CMD> BUSY motion stopped`. This keeps the old "any key cancels" behaviour.

| Command | Alias | Any time | Reply |
|---|---|---|---|
| `HELP` | `?` | yes | `#` menu lines, then `OK HELP` |
| `ID` | | yes | `OK ID name=vsbc_firmware ver=2.0.0 proto=1 loadcell=HX711` |
| `STATUS` | `S` | yes | `OK STATUS state= pos= load= raw= rate= motors= ref= lc= cal= sps= stream= drops= t=` |
| `GET` | | yes | `OK GET` followed by every setting as `key=value` |
| `PING` | | yes | `OK PING` (resets the host watchdog, like any line) |
| `STREAM ON\|OFF` | | yes | `OK STREAM on=1` |
| `STOP` | `X` | yes | `OK STOP`; a running move then ends with `EVT MOVE_END reason=STOP` |
| `RATE <mm/s>` | `V` | | `OK RATE rate= actual= max=` |
| `MOVE <±mm>` | `M` | | `OK MOVE from= target= steps= rate= expected=` |
| `GOTO <mm>` | `G` | | as `MOVE` |
| `HOME` | `H` | | as `MOVE` (goes to position 0) |
| `RUN <±mm>` | `R` | | as `MOVE`; this is a test move (see below) |
| `ZERO [mm]` | `Z` | | `OK ZERO pos=` |
| `TARE` | `T` | | when done: `OK TARE offset= noise=` |
| `CAL <N>` | | | when done: `OK CAL scale= known=` |
| `MOTORS AB\|A\|B` | | | `OK MOTORS motors=` |
| `SET <key> <value>` | | | `OK SET <key>=<value>` |
| `SAVE` | | | `OK SAVE` |
| `DEFAULTS` | | | `OK DEFAULTS` |

### Details

- **RATE** sets the crosshead rate for every following move. Range
  0.002 mm/s up to the motor limit (`max=`, 10 mm/s at 80 steps/mm).
  `actual=` is the rate the timer really produces after rounding.
- **MOVE / GOTO / HOME** move at the set rate. The target is rounded to whole
  steps. A target outside the soft limits `min_pos`..`max_pos` is refused with
  `ERR LIMIT`, unless the move heads back toward the allowed range. `expected=`
  is the expected duration in seconds.
- **RUN** is the test move. It is a `MOVE` that also arms the per-test stops
  (`stop_load`, break detection). It is refused unless the load cell is
  working (`ERR NOLOAD`), calibrated (`ERR UNCAL`), the position is referenced
  (`ERR NOREF`) and both motors are selected (`ERR ARG`).
- **ZERO** sets the current position (default 0) and marks it **referenced**.
  The position is unreferenced after power-up or reset, and after a
  single-motor move.
- **TARE** averages 16 samples with no load and stores the offset.
  `noise=` is the standard deviation in counts.
- **CAL <N>** averages 16 samples with a known load of N newtons applied and
  sets `load_scale = (mean − load_offset) / N`. Run `TARE` first.
  Needs at least 1000 counts of change (`ERR RANGE` otherwise).
- **MOTORS A** or **B** pulses only one driver, for bring-up or service. The
  crosshead racks if the screws are coupled, so this clears "referenced".
- **SET** changes a setting in RAM. Only **SAVE** writes the saved settings to
  EEPROM; **DEFAULTS** restores defaults in RAM (then `SAVE` to keep them).

### Error codes

| Code | Meaning |
|---|---|
| `UNKNOWN` | Unknown command |
| `ARG` | Missing or malformed argument, or unknown setting key |
| `RANGE` | Value out of range |
| `LIMIT` | Target outside the soft travel limits, or a limit switch is active |
| `BUSY` | A move was running (and has been stopped), or TARE/CAL is averaging |
| `NOLOAD` | Load cell not responding |
| `UNCAL` | Load cell not calibrated (`load_scale` is 0) |
| `NOREF` | Position not referenced (use `ZERO` at the reference position) |
| `ESTOP` | E-stop input active (only if one is configured) |

## Events

| Event | When |
|---|---|
| `EVT READY name= ver= proto=` | After power-up or reset, when the firmware is ready |
| `EVT MOVE_END reason= mode= pos= moved= time= peak=` | Every move ends with exactly one of these |
| `EVT LOADCELL state=OK\|LOST` | The load cell starts or stops responding |
| `EVT OVERLOAD load=` | Idle and \|load\| ≥ `max_load` (at most once a second) |
| `EVT INPUT name= state=` | An optional limit/E-stop input changed |

`MOVE_END` fields: `mode` is `MOVE` or `RUN`; `pos` is the final position (mm);
`moved` is the signed distance moved (mm); `time` is in seconds; `peak` is the
peak load during a `RUN` in the run direction (`nan` for `MOVE`).

| `reason` | Meaning |
|---|---|
| `DONE` | Reached the target |
| `STOP` | `STOP` command, or another command arrived during the move |
| `BREAK` | RUN: load fell `break_drop` % below its peak (after the peak reached `break_min`) on two samples in a row |
| `LOAD` | RUN: \|load\| reached `stop_load` |
| `OVERLOAD` | \|load\| reached `max_load` while rising, or the ADC saturated |
| `LOADCELL` | RUN: the load cell stopped responding |
| `WATCHDOG` | No line from the host for `watchdog_ms` |
| `TIMEOUT` | The move took more than its expected time + 1 s (should not happen) |
| `LIMIT` | A limit switch triggered (only if configured) |
| `ESTOP` | The E-stop input triggered (only if configured) |

## Settings

`GET` lists them all; `SET <key> <value>` changes one.

**Saved** by `SAVE` (EEPROM) — machine calibration:

| Key | Default | Meaning |
|---|---|---|
| `steps_per_mm` | 80 | Crosshead steps per mm (400 steps/rev ÷ 5 mm lead). Range 40–160. |
| `load_scale` | 0 | Load-cell counts per newton. 0 = not calibrated. Negative is fine. |
| `load_offset` | 0 | Load-cell counts at zero load |
| `max_load` | 0 | N. Any move stops when \|load\| reaches this while rising. 0 = off |
| `min_pos` / `max_pos` | −100 / 100 | mm. Soft travel limits, relative to the reference zero |
| `dir_invert` | 0 | 1 = swap which DIR level moves + (tension) |
| `invert_b` | 0 | 1 = motor B turns opposite to motor A |

**Not saved** — per-test values the host sets before each `RUN`:

| Key | Default | Meaning |
|---|---|---|
| `stop_load` | 0 | N. RUN stops when \|load\| reaches this. 0 = off |
| `break_drop` | 0 | %. Break detection threshold. 0 = off |
| `break_min` | 0 | N. Peak needed before break detection arms. 0 = off |
| `watchdog_ms` | 0 | Stop a move if no line arrives for this long. 0 = off. The Python host sets 3000 and pings every 0.5 s. |

Safety checks use a median of the last three samples, so one bad sample cannot
trigger a stop.

## Example session

```
EVT READY name=vsbc_firmware ver=2.0.0 proto=1
> STREAM ON
OK STREAM on=1
D 2051 0.0000 nan 84310 I
> ZERO
OK ZERO pos=0.0000
> RATE 0.0833
OK RATE rate=0.0833 actual=0.0833 max=10.0000
> RUN 20
OK RUN from=0.0000 target=20.0000 steps=1600 rate=0.0833 expected=240.10
D 2163 0.0000 0.012 84360 R
...
EVT MOVE_END reason=BREAK mode=RUN pos=3.9875 moved=3.9875 time=47.86 peak=401.220
```
