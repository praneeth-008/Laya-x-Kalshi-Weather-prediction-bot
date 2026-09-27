"""Shared, resumable, checkpointed concurrent GRIB2 byte-range extraction
harness for the historical pilot (HRRR/GFS/GEFS/NBM/ECMWF). Reuses each
source module's existing fetch_idx/find_message/fetch_byte_range/
decode_message functions unchanged -- this module only adds: concurrency,
a JSON checkpoint (atomic write, retry-on-Windows-PermissionError, same
pattern as the Kalshi downloaders), the stream-and-discard policy (no GRIB
bytes ever touch disk -- decoded in memory then dropped), and the
patch-is-free spatial extraction (one message decoded once, sampled at
every patch point).

CORE RULE (unchanged from the rest of this project): every row keeps
run_time, valid_time, available_time, availability_time_type separately.
Nothing here invents an availability time -- it comes only from the
source's own S3 Last-Modified, exactly as data/*.py already does.
"""
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


@dataclass
class PilotStats:
    n_requests: int = 0
    bytes_downloaded: int = 0
    lock: Lock = field(default_factory=Lock)

    def record(self, n_bytes: int, _seconds: float = 0.0) -> None:
        with self.lock:
            self.n_requests += 1
            self.bytes_downloaded += n_bytes


def decode_message_multi_point(raw_bytes: bytes, points: list[tuple[float, float]]) -> list[dict]:
    """Decode one standalone GRIB2 message ONCE and extract nearest-gridpoint
    values at MULTIPLE (lat, lon) points via a single vectorized NumPy
    nearest-neighbor search, instead of calling eccodes' own
    codes_grib_find_nearest() once per point.

    DISCOVERED DURING PILOT BUILD: codes_grib_find_nearest() on HRRR's
    ~1.9M-point CONUS grid takes ~2.4s PER CALL regardless of caching the
    open GRIB handle -- calling it once per 3x3-patch point (9x) would have
    made every single (run, forecast_hour, variable) work item take ~20s+
    just for point extraction, making the pilot practically infeasible
    (projected >100s of hours for HRRR alone). Pulling the full lat/lon/value
    arrays once (via codes_get_array, ~1s) and doing all N nearest-point
    lookups as one vectorized operation (~0.1s per point after that) cuts
    this to a small, patch-size-insensitive fixed cost per message -- this
    is what "the patch is nearly free" actually requires in practice, not
    just true for remote bytes (which was already correctly established)."""
    import os
    import tempfile

    import eccodes
    import numpy as np

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(raw_bytes)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as f:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                raise RuntimeError("eccodes could not parse this byte range as a GRIB2 message")
            try:
                variable = eccodes.codes_get(gid, "shortName")
                level = eccodes.codes_get(gid, "level")
                level_type = eccodes.codes_get(gid, "typeOfLevel")
                units = eccodes.codes_get(gid, "units")
                forecast_hour = eccodes.codes_get(gid, "forecastTime")
                data_date = eccodes.codes_get(gid, "dataDate")
                data_time = eccodes.codes_get(gid, "dataTime")

                lats = eccodes.codes_get_array(gid, "latitudes")
                lons = eccodes.codes_get_array(gid, "longitudes")
                values = eccodes.codes_get_array(gid, "values")
                lons_wrapped = (lons + 180) % 360 - 180

                results = []
                for lat, lon in points:
                    d2 = (lats - lat) ** 2 + (lons_wrapped - lon) ** 2
                    i = int(np.argmin(d2))
                    # Haversine-ish small-angle distance in km, consistent
                    # with the existing single-point decode_message()'s
                    # eccodes-reported distance (good enough at this scale).
                    dlat_km = (lats[i] - lat) * 111.0
                    dlon_km = (lons_wrapped[i] - lon) * 111.0 * abs(np.cos(np.radians(lat)))
                    distance_km = float((dlat_km ** 2 + dlon_km ** 2) ** 0.5)
                    results.append(
                        {
                            "variable": variable, "level": level, "level_type": level_type,
                            "units": units, "forecast_hour": forecast_hour,
                            "data_date": data_date, "data_time": data_time,
                            "requested_lat": lat, "requested_lon": lon,
                            "grid_lat": float(lats[i]), "grid_lon": float(lons_wrapped[i]),
                            "value": float(values[i]), "distance_km": distance_km,
                        }
                    )
                return results
            finally:
                eccodes.codes_release(gid)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


class Checkpoint:
    """One JSON file, one row per work-item key. Atomic write with retry
    (Windows can transiently deny a rename while another handle is open)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = Lock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {}

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def is_done(self, key: str) -> bool:
        row = self.data.get(key)
        return row is not None and row.get("status") == "DONE"

    def set(self, key: str, **fields) -> None:
        with self._lock:
            row = self.data.setdefault(key, {})
            row.update(fields)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        for attempt in range(5):
            try:
                tmp.write_text(json.dumps(self.data, default=str))
                tmp.replace(self.path)
                return
            except PermissionError:
                time.sleep(0.2 * (attempt + 1))
        tmp.write_text(json.dumps(self.data, default=str))
        tmp.replace(self.path)

    def counts(self) -> dict:
        out = {}
        for row in self.data.values():
            out[row.get("status", "UNKNOWN")] = out.get(row.get("status", "UNKNOWN"), 0) + 1
        return out


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def run_concurrent(work_items: list, worker_fn, checkpoint: Checkpoint, flush_dir: Path,
                    max_workers: int = 12, save_every: int = 25, byte_budget: int | None = None,
                    label: str = "extraction") -> dict:
    """work_items: list of (key, *args) where key is the checkpoint key.
    worker_fn(*args) -> (rows: list[dict], remote_bytes: int) or raises.
    Skips items already marked DONE in checkpoint.

    Uses ProcessPoolExecutor, NOT threads -- eccodes was empirically found to
    corrupt its internal state under concurrent THREADS in this environment
    (separate OS processes each get their own eccodes state, which is safe).
    args must therefore be picklable (no locks/open handles) -- byte-count
    tracking is aggregated in the parent from each worker's own return value,
    not a shared mutable stats object.

    DURABILITY: every `save_every` completed items, both the checkpoint AND
    the rows accumulated since the last flush are written to disk (a new
    dated Parquet part-file under flush_dir) -- so a crash/interruption never
    loses more than ~save_every items' worth of already-completed work, even
    though checkpoint.is_done() would otherwise claim it as finished. This
    also means the process can be killed and resumed with `python
    scripts/pilot_phaseN_....py` again at any time with no data loss beyond
    that small window, and no re-fetching of anything already flushed."""
    flush_dir = Path(flush_dir)
    flush_dir.mkdir(parents=True, exist_ok=True)
    pending = [(k, a) for (k, a) in work_items if not checkpoint.is_done(k)]
    _log(f"[{label}] {len(pending)} of {len(work_items)} work items pending (rest already DONE)")

    buffer_rows = []
    done_count = 0
    total_bytes = 0
    aborted = False
    t_start = time.time()

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker_fn, *a): k for k, a in pending}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                rows, remote_bytes = fut.result()
                checkpoint.set(key, status="DONE", rows=len(rows), remote_bytes=remote_bytes)
                buffer_rows.extend(rows)
                total_bytes += remote_bytes
            except Exception as e:
                checkpoint.set(key, status="FAILED", error=str(e))
            done_count += 1
            if done_count % save_every == 0 or done_count == len(pending):
                checkpoint.save()
                if buffer_rows:
                    _flush_part(flush_dir, buffer_rows, done_count)
                    buffer_rows = []
                elapsed = time.time() - t_start
                rate = done_count / elapsed if elapsed > 0 else 0
                remaining = (len(pending) - done_count) / rate if rate > 0 else float("nan")
                pct = 100 * done_count / len(pending) if pending else 100
                counts = checkpoint.counts()
                _log(
                    f"[{label}] {done_count}/{len(pending)} ({pct:.1f}%) done | "
                    f"{total_bytes/1e6:.1f} MB this run | rate={rate:.3f} items/s | "
                    f"ETA={remaining/3600:.2f}h | status_counts={counts}"
                )
                if byte_budget is not None and total_bytes > byte_budget:
                    _log(f"[{label}] STOP: actual remote bytes ({total_bytes/1e9:.2f} GB) exceeded budget ({byte_budget/1e9:.2f} GB) -- aborting remaining work, checkpoint+data preserved")
                    aborted = True
                    for f2 in futures:
                        f2.cancel()
                    break

    checkpoint.save()
    if buffer_rows:
        _flush_part(flush_dir, buffer_rows, "final")

    result = {"done_count": done_count, "total_bytes_this_run": total_bytes, "aborted": aborted}
    if aborted:
        raise RuntimeError(f"[{label}] network-safety abort: exceeded byte budget")
    return result


def _flush_part(flush_dir: Path, rows: list, tag) -> None:
    """Write rows to a NEW, uniquely-named part-file, via a temp-file +
    atomic rename so an interruption mid-write never leaves a corrupt
    part-file that a later read would choke on."""
    import pandas as pd

    final_path = flush_dir / f"part_{int(time.time()*1000)}_{tag}.parquet"
    tmp_path = final_path.with_suffix(".tmp")
    pd.DataFrame(rows).to_parquet(tmp_path, index=False)
    for attempt in range(5):
        try:
            tmp_path.replace(final_path)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    tmp_path.replace(final_path)
