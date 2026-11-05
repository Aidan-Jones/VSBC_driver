# VSBC camera + DIC scripts

Basler a2A1920-160umBAS (Mono8, USB3) + Pyvale DIC + crack-tip tracking.

**Status:**
- **Standalone scripts:** tested end-to-end on a Windows laptop.
- **On the Pi:** the `vsbc` GUI's Camera tab runs these scripts. The integration is tested with a simulated camera only. Run `vsbc camera check` on the Pi before the first real test.

This folder is the **swappable** part. Drop in updated versions of these files and the GUI uses them: the camera process re-imports them at **Camera panel > Reload scripts**, or whenever the app starts, and every post-processing job runs the current files. Keep the [interface](#interface-used-by-the-gui) below and nothing else needs to change.

## Scripts

| Script | When | What it does |
|---|---|---|
| `capture_dic_live.py` | test (standalone) | Preview (exposure ±, saturation, 100 % zoom) → draw ROI → record; full-rate crack window on `c`. After stopping: frame viewer, DIC, then `pyvale_dic_run.py` automatically. **In the GUI:** its USER SETTINGS are the camera's settings, and the GUI calls its `configure_camera()` and `dic_worker()`. |
| `pyvale_dic_run.py` | after the test | Strain, plots vs time, whole-field motion diagnostics, strain animation of the crack window |
| `view_frames.py` | after the test | Play / scrub the saved frames (opens on the crack window); export the crack window as `.avi` |
| `crack_tip_viewer.py` | after the test | Step through the DIC frames with the strain overlay and both trackers' tips; click to mark the tip by hand (`crack_tip_manual.csv`) |
| `crack_tip_image.py` | after the test | Image-based tracker (warped reference, notch-connected path); crack length vs time |
| `crack_tip_opening.py` | after the test | Crack tip from the DIC crack opening; crack length and speed; compares with the image tracker |

Both crack trackers are kept until a real brittle crack shows which is more reliable. (The opening method failed on the earlier ductile material; the image tracker matched manual labels there.) Neither has been checked on a real crack yet: mark 5–10 frames by hand in `crack_tip_viewer.py` and compare.

The after-test scripts take a run folder on the command line (`python3 view_frames.py ~/vsbc_captures/<run>`; for a GUI test, `<test folder>/camera`), or use the newest run in `~/vsbc_captures` (`C:\VSBC_DIC_Test\captures` on Windows). The GUI runs only `pyvale_dic_run.py` and the two trackers; the viewers are for looking at a run by hand.

## Standalone on the Pi (without the GUI)

```bash
source ~/VSBC_driver/.venv-camera/bin/activate
python3 ~/VSBC_driver/camera/capture_dic_live.py
```

Results go to `~/vsbc_captures/<date>_<time>_<TEST_NAME>/`. Recording saves ~`SAVE_FPS` frames/s to disk and keeps the last `BUFFER_SECONDS` of every frame in RAM. `LIVE_DIC = False` (the default) runs no DIC during the test, so the Pi's cores stay free for capture. After `q`, the script writes the buffer and opens `view_frames.py`. Then it runs DIC on all cores and `pyvale_dic_run.py <run>`, which saves the plots as PNG in `pyvale_results/`.

On the first real Pi run, check:
- the capture summary: **0 dropped frames, 0 grab errors, 0 write errors** at the chosen crop (USB at ~360 MB/s);
- how long the crack buffer takes to write to the SD card, and the DIC seconds/frame (that sets how long you wait after a test);
- whether OpenCV's windows and Pyvale's ROI window (Qt) conflict ("Qt platform plugin" errors).

## In the vsbc GUI

- **Camera tab** (the default tab, left):
  - live preview with exposure ± and 1:1 zoom;
  - **Edit ROI**: rectangle or polygon, the seed, the notch tip and the crack direction;
  - Marker, **⚡ High speed**, manual Record.
- **Camera panel** (right): save mode, RAM buffer, settle time, load-cell delay, DIC options, what runs after the test, and the script check.
- **With each test:**
  1. Hands-off countdown (`SETTLE_SECONDS`), then frame 0, the DIC reference, then the test starts.
  2. During the test, the camera grabs every frame into a RAM ring (`BUFFER_SECONDS`). It **saves the frame nearest each load sample**, so every data.csv row has its own image.
  3. **⚡ High speed** saves the ring plus every frame until it is turned off or the test ends.
  4. When the Mega detects the break, the ring is saved at full rate.
  5. **The test and the camera stop together.** If the camera fails (disk full, unplugged), the test stops too.
- **After the test**, in this order, at low priority:
  1. sync;
  2. `video.mp4`;
  3. DIC (`dic_worker`);
  4. `pyvale_dic_run.py`;
  5. the crack trackers.

  Plots from `plt.show()` are saved to `camera/plots/`. To re-run: Camera panel > **Process a folder…**, or `vsbc camera process <test folder>`.
- **Results tab** (left; **View results** in the Test or Outputs tab): scrub or play the saved frames with the ROI, the notch, the crack tips and the DIC map drawn on them. The plots and the summary are there too.
  - **Crack frames**: the frames between the last load reading before the drop and the first one after it, moved back by the load-cell delay. Around 48 frames at 10 load readings/s and 478 fps.
  - The crack window adds 50 frames either side.
  - The estimated crack frame is the largest frame-to-frame change inside the ROI. This is in `vsbc/camera/results.py`; it reads only frames.csv, data.csv and the TIFFs, so it needs nothing from this folder.
- **DIC tab** (left, after Results):
  - Run DIC, strain and the crack trackers on any test, after a checklist (ROI, notch, scripts, pyvale in the camera environment, disk). It uses the same jobs as after a test.
  - View pyvale's displacement and strain over the frames.
  - Mark crack tips by hand. They are written to `crack_tip_manual.csv` in `crack_tip_viewer.py`'s format, so either tool can add to them.
- **Files**, per test:

  ```
  Outputs/tests/<date>/<time>_<specimen>/
  ├── data.csv              + columns frame, frame_dt_ms (the saved frame nearest each row)
  ├── video.mp4             playback time = data.csv time_s; time, load and extension on each frame
  └── camera/               (this is RUN_FOLDER for the scripts)
      ├── images/frame_000000.tif ...   frame number = camera frame since recording started
      ├── frames.csv        every frame grabbed (see below)
      ├── capture_settings.json, sync.json, roi.npz, samples.csv, camera_clock.csv
      ├── pyvale_results/   dic_results_*, strain_*
      ├── plots/            the scripts' figures
      └── crack_tip_image.csv, crack_tip_opening.csv   (+ test_time_s, load_N, extension_mm)
  ```

`frames.csv` columns:
- From the script's format: `frame_index`, `filename`, `saved`, `block_id`, `camera_timestamp_ns`, `pc_time_s`, `marker`.
- Added by the GUI: `host_ns`, `matched`, `full_rate`, then `time_s` (**data.csv's clock**), `load_N`, `load_method`, `sample_row`, `position_mm` and `extension_mm`.
- `load_method`:
  - `interp`: the load interpolated to the frame's time, for frames saved 1:1.
  - `latest`: the last reading at or before the frame, inside full-rate windows, so nothing is interpolated across the break.
- With display units other than N/mm, columns in those units are added as well.

## Interface used by the GUI

Keep these when you update a script. `vsbc camera check` and the Camera panel's "Camera scripts" box check them, without importing anything.

- **`capture_dic_live.py`**
  - **USER SETTINGS** as top-level names: `EXPOSURE_US`, `FPS`, `CROP`, `SAVE_EVERY`, `BUFFER_SECONDS`, `MAX_SECONDS`, `SETTLE_SECONDS`, `DIC_STEP`, `CRACK_DIC_STEP`, `subset_size`, `subset_step`, `max_displacement`, `DIC_THREADS`, `FILL_BATCH`, `N_WRITERS`, `PREVIEW_SCALE`, `TEST_NAME`, `APERTURE_LABEL`. A missing one falls back to today's value.
  - **`configure_camera(camera, exposure_us, crop, fps) -> (width, height)`**. Optional: without it, the GUI uses a copy of today's setup.
  - **`dic_worker(cfg, done_q, status_q)`**, required for DIC. The GUI passes the same `cfg` keys as `main()` (`frames_dir`, `out_dir`, `step`, `roi_mask`, `roi_seed`, `subset_size`, `subset_step`, `max_displacement`, `threads`, `fill_batch`, `window`). It uses `step = 1` and only queues the frames to analyse. A new parameter should be read inside `dic_worker` from a module setting.
  - Nothing heavy at import time: `main()` only under `if __name__ == "__main__":`, and pypylon / pyvale imported inside functions.
- **Analysis scripts**: they need top-level assignments for the settings the GUI points at each test.

  | Script | Settings |
  |---|---|
  | `pyvale_dic_run.py` | `image_folder`, `output_folder`, `RUN_DIC`, `MAKE_ANIMATION`, `ANIM_STEP` |
  | `crack_tip_image.py` | `RUN_FOLDER`, `NOTCH_TIP`, `CRACK_FROM_LEFT` |
  | `crack_tip_opening.py` | `RUN_FOLDER`, `NOTCH_TIP`, `CRACK_FROM_LEFT`, `subset_size` |

  The job runner inserts `NAME = <value>` right after each of those lines and runs the file as `__main__`. So a script with or without `main()` works, and so does a plain top-to-bottom script like `pyvale_dic_run.py`. Each `plt.show()` saves the open figures as `plots/<script>_NN.png`.

## Time sync

Three clocks are involved:
- the Mega's clock, which data.csv uses (firmware 2.3.0 stamps each load reading to the microsecond, when the HX711 signals it ready);
- the camera's timestamp counter;
- the Pi's monotonic clock.

How they are tied together:
- The GUI logs when every load sample arrived (`samples.csv`).
- The camera process logs when every frame arrived, and latches the camera clock every 2 s (`TimestampLatch`, which only reads the clock and changes no setting), saved in `camera_clock.csv`.
- Linear fits map camera → Pi → Mega. The Mega fit follows the fastest arrivals and absorbs a clone's resonator drift.
- So every frame gets a `time_s` with data.csv's clock and zero (the first RUN sample). Frames before the test have negative times.
- `sync.json` records the fits and how closely the rows and frames pair. In the simulator: 95 % within about 5 ms at 100 fps; at 478 fps expect about ±1 ms.

The load itself lags the image by the HX711's filter (roughly 2 conversion periods: ≈ 25 ms at 80 samples/s, ≈ 200 ms at 10/s). Measure it once with a tap test and enter it as the **load-cell delay** (Camera panel, or `[camera] load_delay_ms`).

## Current capture settings (`capture_dic_live.py`)

| Setting | Value | Meaning |
|---|---|---|
| `CROP` | 1920 x 400; set `offset_y` so the band covers the notch (check in the preview) | 478 fps measured, 0 dropped |
| `EXPOSURE_US` | 1488 | at f/2.8, gain 0 |
| `SAVE_FPS` | 10 | standalone: frames/s saved continuously (~8 MB/s at 400 rows, fine for a microSD). The script derives its save step from the camera's frame rate |
| `SAVE_EVERY` | 9 | GUI only: the "every k-th frame" save mode (the GUI saves 1 frame per load sample by default) |
| `BUFFER_SECONDS` | 2 | every frame of the last 2 s held in RAM |
| `POST_SECONDS` | 1 | standalone: after `c`, every frame for 1 more s. The GUI stops the camera with the test instead |
| `OVERVIEW_DIC_FPS` | 2 | standalone: DIC rate over the loading phase (always a multiple of the save step) |
| `DIC_STEP` | 27 | GUI only: overview DIC on every 27/9 = 3rd saved frame |
| `CRACK_DIC_STEP` | 1 | DIC on every frame of the full-rate window |
| `LIVE_DIC` | False | standalone: DIC during the test too (laptop). The GUI's live DIC is set in the Camera panel |
| `MAX_SECONDS` | 1200 | safety stop, 20 min (standalone and GUI) |
| `SETTLE_SECONDS` | 5 | wait before the reference frame (hands off) |

Standalone keys while recording: `m` marker, `c` crack (saves the 2 s buffer + 1 s more at full rate), `q` stop. Frame 0 is the DIC reference: start recording with the specimen unloaded.

## Measured on the laptop
- Full frame 1920x1200: 169 fps. 400 rows: 478 fps. 300 rows: 612 fps (all 0 dropped).
- 300-row dry run: 18 s at 612 fps, 0 dropped / 0 errors; 1,906-frame crack window; DIC 0.13 s/frame (~5 min after the test).
- Still-specimen noise (f/2.8): random ~0.003-0.008 px, strain ~45 microstrain; whole-field wobble from the mount dominates.
- Image tracker ~0.17 s/frame. Both trackers within ~3-8 px of truth on a synthetic fast crack (not yet validated on a real crack).

## Changes made for the GUI (marked `FIX:` in the code)

`capture_dic_live.py`:
1. `set_feature`, `get_feature`, `round_to_inc` and `fps_now` moved to top level, and `configure_camera()` added, so the GUI can call them. `main()` uses them, so standalone use is unchanged.
2. `MAX_SECONDS = 45` ended a normal tensile test early. Now it is 1200 (20 min), for the GUI's safety stop too.
3. The unbounded save queue fills RAM when the disk is slower than the save rate (an SD card), until the program is killed. Now a warning prints while it happens.
4. Write errors (for example a full disk) were only counted at the end. Now the first one is reported at once.
5. `set(write_errors)` was rebuilt for every frame. Crack windows were guessed from runs of consecutive frames, so with `SAVE_EVERY = 1` the whole recording counted as a crack window. Now the set is built once, and the windows are recorded as they happen (`crack_windows` in `capture_settings.json`).
6. `DIC_STEP` not being a multiple of `SAVE_EVERY` silently gave far fewer DIC frames. Standalone, both steps now come from `SAVE_FPS` / `OVERVIEW_DIC_FPS`, and the overview step is always a multiple of the save step.
7. `round_to_inc` now also clamps to Max. `offset_x = 8` with `width = 1920` fails if the sensor is exactly 1920 wide, and before this the only sign was a printed "not set".
8. `fps_now()` returns None on models without that node, which gave a TypeError. It now falls back to measuring the rate.
9. `GrabOne` is checked with `GrabSucceeded()` before `.Array` is read.
10. `OUTPUT_ROOT`: a Windows path became a folder literally named `C:\...` on Linux. It is now `~/vsbc_captures` there.
11. `dic_worker` reads `cfg.get("live", True)`: standalone with `LIVE_DIC = False`, it waits for the stop. The GUI does not pass `live`, so its live DIC is unchanged.
12. The ROI seed is normalised to one `[x, y]` pair of ints before `calculate_2d`.

`pyvale_dic_run.py`:
- frames.csv is read with the `csv` module (`genfromtxt(dtype=None)` misreads an empty `marker` column), and its test clock (`time_s`) is used when present;
- a clear warning when `strain_*.csv` files from an earlier run are mixed in;
- `python3 pyvale_dic_run.py <run>` uses that run and saves the plots as PNG (capture_dic_live.py calls it this way). `image_folder` / `output_folder` stay top-level settings for the GUI (`None` = the newest run);
- `ANIM_RANGE = "crack"` animates only the full-rate window, which also applies to GUI runs that have one.

`crack_tip_image.py`:
- `griddata` raised (QhullError) when a frame had fewer than 3 converged points, right after full separation, which ended the run. It now keeps the previous tip.
- The first row's crack length was the notch's x position instead of 0, so the plot started with a spike. It is now 0.
- It uses frames.csv's `time_s` when present.

`crack_tip_opening.py`:
- Full-rate windows are read from `capture_settings.json`. Detecting them with `diff == 1` found nothing when `CRACK_DIC_STEP > 1`.
- A clear error when the crack line cannot be found, instead of "too close to the ROI edge".
- It uses frames.csv's `time_s` when present.

## Requirements

**Install these into the separate camera environment**, not the app's `.venv`:

```bash
bash scripts/install_pi.sh --camera       # creates .venv-camera, udev rule, USB memory, ffmpeg
vsbc camera check                         # camera fps / dropped frames, disk speed, a small pyvale DIC
```

Why a separate environment: pyvale 2026.x requires PyQt6, opencv-python (with its own Qt) and NumPy ≥ 2. The app's `.venv` uses the Pi's apt PyQt5, NumPy 1 and matplotlib, and mixing them would break the GUI. The GUI runs all camera code as separate processes in `.venv-camera`. See `requirements.txt`.

On the laptop, point the app at the conda environment instead: `[camera] python = "C:/Users/<you>/miniforge3/envs/dic/python.exe"` in `config.toml`.

**Version note:** the Pi installs **pyvale 2026.10.2**, the version the scripts were developed on (prebuilt for 64-bit ARM, Python 3.11–3.14). `vsbc camera check` runs a tiny synthetic DIC through the same `calculate_2d` call as `dic_worker` to confirm it works.

## Disk and RAM
- A frame is 0.77 MB (1920 × 400).
  - 1 frame per load sample at 80 samples/s is 61 MB/s, about 37 GB for a 10-minute test. At 10 samples/s it is 7.7 MB/s.
  - High speed is about 367 MB/s while on: the RAM queue absorbs it for a few seconds, and if the disk falls behind, the oldest unsaved high-speed frames are dropped first.
  - Use a USB 3 SSD for `output_dir` on the Pi, or the "every k-th sample" option. The Camera panel shows the estimate, and the GUI refuses to record a test that won't fit.
- The 2 s RAM ring at 478 fps is 0.73 GB, so an 8 GB Pi is the safer choice.
- After DIC, **Outputs tab > Delete camera images** frees most of the space and keeps data.csv, the video, the DIC results and the plots.

## Paths
Standalone runs go to `~/vsbc_captures` on the Pi (`C:\VSBC_DIC_Test\captures` on Windows). In the GUI, the paths are set for each test automatically.
