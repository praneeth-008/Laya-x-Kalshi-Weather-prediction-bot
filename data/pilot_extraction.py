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


_GRID_CACHE: dict = {}
_GRID_CACHE_STATS = {"hits": 0, "misses": 0, "uncertain_fingerprint": 0, "periodic_validations": 0, "invalidations": 0}
_PERIODIC_REVALIDATE_EVERY = 500
_MAX_PATCH_DISTANCE_KM = 10.0  # generous vs. HRRR's measured ~1km nearest-point / 3km native spacing

_GRID_FINGERPRINT_SCALAR_KEYS = [
    "gridType", "Ni", "Nj",
    "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
    "LoVInDegrees", "Latin1InDegrees", "Latin2InDegrees",
    "DxInMetres", "DyInMetres",
]


def _grid_fingerprint(gid) -> str | None:
    """A cheap, positive identifier of this message's grid definition.
    Returns None (never a guess) if identity cannot be established with
    confidence -- callers must treat None as "cache not usable here"."""
    import eccodes

    try:
        md5 = eccodes.codes_get(gid, "md5GridSection")
        if md5:
            return f"md5:{md5}"
    except Exception:
        pass

    values = {}
    for k in _GRID_FINGERPRINT_SCALAR_KEYS:
        try:
            values[k] = eccodes.codes_get(gid, k)
        except Exception:
            return None  # any missing/unreadable key -> uncertain, no fingerprint
    return "scalar:" + json.dumps(values, sort_keys=True, default=str)


def _full_grid_search(lats, lons_wrapped, points: list[tuple[float, float]]) -> list[dict]:
    """Ground-truth nearest-neighbor search: one vectorized NumPy lookup per
    requested point over the full grid. Used on cache miss, on first
    encounter of a fingerprint, and on periodic re-validation."""
    import numpy as np

    results = []
    for lat, lon in points:
        d2 = (lats - lat) ** 2 + (lons_wrapped - lon) ** 2
        i = int(np.argmin(d2))
        # Haversine-ish small-angle distance in km, consistent with the
        # existing single-point decode_message()'s eccodes-reported distance
        # (good enough at this scale).
        dlat_km = (lats[i] - lat) * 111.0
        dlon_km = (lons_wrapped[i] - lon) * 111.0 * abs(np.cos(np.radians(lat)))
        distance_km = float((dlat_km ** 2 + dlon_km ** 2) ** 0.5)
        results.append({"index": i, "grid_lat": float(lats[i]), "grid_lon": float(lons_wrapped[i]), "distance_km": distance_km})
    return results


def _validate_patch_distances(search_results: list[dict], points: list[tuple[float, float]], max_km: float = _MAX_PATCH_DISTANCE_KM) -> None:
    """Every requested patch point, not just the center, must map to a grid
    point within a sane distance -- raises (never silently continues) if not."""
    for (lat, lon), r in zip(points, search_results):
        if r["distance_km"] > max_km:
            raise RuntimeError(
                f"Grid sanity check failed: requested point ({lat}, {lon}) nearest grid point is "
                f"{r['distance_km']:.3f} km away (max allowed {max_km} km) -- refusing to trust this mapping"
            )


def _entries_agree(cached: dict, fresh_results: list[dict], coord_tol: float = 1e-6, dist_tol: float = 1e-6) -> bool:
    fresh_indices = [r["index"] for r in fresh_results]
    if cached["indices"] != fresh_indices:
        return False
    for a, b in zip(cached["grid_lat"], [r["grid_lat"] for r in fresh_results]):
        if abs(a - b) > coord_tol:
            return False
    for a, b in zip(cached["grid_lon"], [r["grid_lon"] for r in fresh_results]):
        if abs(a - b) > coord_tol:
            return False
    for a, b in zip(cached["distance_km"], [r["distance_km"] for r in fresh_results]):
        if abs(a - b) > dist_tol:
            return False
    return True


def _build_cache_entry(search_results: list[dict], grid_ni: int | None, grid_nj: int | None) -> dict:
    return {
        "indices": [r["index"] for r in search_results],
        "grid_lat": [r["grid_lat"] for r in search_results],
        "grid_lon": [r["grid_lon"] for r in search_results],
        "distance_km": [r["distance_km"] for r in search_results],
        "grid_ni": grid_ni,
        "grid_nj": grid_nj,
        "validated_at": time.time(),
        "hits_since_validation": 0,
    }


def decode_message_multi_point(raw_bytes: bytes, points: list[tuple[float, float]]) -> list[dict]:
    """Decode one standalone GRIB2 message and extract values at MULTIPLE
    (lat, lon) points, reusing a per-process cache of the Central Park patch's
    grid indices when the message's grid identity has been positively
    validated before (see _grid_fingerprint/_GRID_CACHE below).

    Decodes directly from the in-memory byte range via
    eccodes.codes_new_from_message() -- no temp file is created at all, which
    is both faster and avoids a Windows-specific issue where a temp-file-based
    GRIB handle would not release its underlying file lock synchronously with
    codes_release(), causing os.unlink() to fail every time (observed: 100% of
    calls in profiling). This also brings the code in line with this module's
    own documented stream-and-discard policy ("no GRIB bytes ever touch disk").

    CACHING (per-process only -- each ProcessPoolExecutor worker builds its
    own cache in its own memory, no multiprocessing shared state):
    the 9 Central-Park-patch grid indices, their coordinates, and their
    distances from the requested points depend ONLY on the grid definition
    (same for every message sharing that definition) and the requested
    points themselves -- never on the message's actual data values, which are
    always read fresh. A cache entry is only created after the full
    nearest-neighbor search has been run and every patch point has passed the
    distance sanity check (ground truth first, cache second, never the
    reverse). On every use, the cache is re-validated in full every
    _PERIODIC_REVALIDATE_EVERY hits, and disagreement invalidates and rebuilds
    it rather than silently continuing with a stale mapping. A fingerprint
    that cannot be established with confidence (missing/unreadable grid keys)
    is never treated as a cache hit -- that message always falls back to the
    full computation, uncached."""
    import eccodes
    import numpy as np

    gid = eccodes.codes_new_from_message(raw_bytes)
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

        fingerprint = _grid_fingerprint(gid)
        cache_key = (fingerprint, tuple(points)) if fingerprint is not None else None
        entry = _GRID_CACHE.get(cache_key) if cache_key is not None else None

        if cache_key is None:
            _GRID_CACHE_STATS["uncertain_fingerprint"] += 1
        elif entry is None:
            _GRID_CACHE_STATS["misses"] += 1
        else:
            _GRID_CACHE_STATS["hits"] += 1

        if entry is not None:
            entry["hits_since_validation"] += 1
            if entry["hits_since_validation"] >= _PERIODIC_REVALIDATE_EVERY:
                _GRID_CACHE_STATS["periodic_validations"] += 1
                lats = eccodes.codes_get_array(gid, "latitudes")
                lons = eccodes.codes_get_array(gid, "longitudes")
                lons_wrapped = (lons + 180) % 360 - 180
                fresh = _full_grid_search(lats, lons_wrapped, points)
                _validate_patch_distances(fresh, points)
                if _entries_agree(entry, fresh):
                    entry["hits_since_validation"] = 0
                    entry["validated_at"] = time.time()
                else:
                    _GRID_CACHE_STATS["invalidations"] += 1
                    _log(
                        f"WARNING: grid cache mismatch detected for fingerprint {fingerprint!r} after "
                        f"{_PERIODIC_REVALIDATE_EVERY} hits -- invalidating cached mapping and using freshly "
                        f"validated indices (old={entry['indices']}, new={[r['index'] for r in fresh]})"
                    )
                    grid_ni = _safe_get(gid, "Ni")
                    grid_nj = _safe_get(gid, "Nj")
                    entry = _build_cache_entry(fresh, grid_ni, grid_nj)
                    _GRID_CACHE[cache_key] = entry
            search_results = [
                {"index": i, "grid_lat": glat, "grid_lon": glon, "distance_km": d}
                for i, glat, glon, d in zip(entry["indices"], entry["grid_lat"], entry["grid_lon"], entry["distance_km"])
            ]
        else:
            lats = eccodes.codes_get_array(gid, "latitudes")
            lons = eccodes.codes_get_array(gid, "longitudes")
            lons_wrapped = (lons + 180) % 360 - 180
            search_results = _full_grid_search(lats, lons_wrapped, points)
            _validate_patch_distances(search_results, points)
            if cache_key is not None:
                grid_ni = _safe_get(gid, "Ni")
                grid_nj = _safe_get(gid, "Nj")
                _GRID_CACHE[cache_key] = _build_cache_entry(search_results, grid_ni, grid_nj)

        values = eccodes.codes_get_array(gid, "values")
        results = []
        for (lat, lon), r in zip(points, search_results):
            results.append(
                {
                    "variable": variable, "level": level, "level_type": level_type,
                    "units": units, "forecast_hour": forecast_hour,
                    "data_date": data_date, "data_time": data_time,
                    "requested_lat": lat, "requested_lon": lon,
                    "grid_lat": r["grid_lat"], "grid_lon": r["grid_lon"],
                    "value": float(values[r["index"]]), "distance_km": r["distance_km"],
                }
            )
        return results
    finally:
        eccodes.codes_release(gid)


def _safe_get(gid, key):
    import eccodes

    try:
        return eccodes.codes_get(gid, key)
    except Exception:
        return None


def grid_cache_stats() -> dict:
    """Snapshot of this process's grid cache, for diagnostics/testing only --
    never used in the extraction logic itself."""
    return {
        "hits": _GRID_CACHE_STATS["hits"],
        "misses": _GRID_CACHE_STATS["misses"],
        "uncertain_fingerprint": _GRID_CACHE_STATS["uncertain_fingerprint"],
        "periodic_validations": _GRID_CACHE_STATS["periodic_validations"],
        "invalidations": _GRID_CACHE_STATS["invalidations"],
        "distinct_fingerprints": len({k[0] for k in _GRID_CACHE}),
        "distinct_cache_keys": len(_GRID_CACHE),
        "entries": {
            f"{k[0]}": {
                "indices": v["indices"],
                "grid_ni": v["grid_ni"], "grid_nj": v["grid_nj"],
                "hits_since_validation": v["hits_since_validation"],
                "validated_at": v["validated_at"],
            }
            for k, v in _GRID_CACHE.items()
        },
    }


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
