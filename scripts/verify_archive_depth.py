"""Cheap archive-depth verification: bucket LISTING/HEAD only, no full GRIB
downloads. Reuses each source module's existing list_objects()/fetch_idx()
functions -- no new download logic. Prints results; does not save large data.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from data import hrrr, gfs, gefs, nbm, ecmwf

DATES = [
    datetime(2023, 1, 15), datetime(2023, 7, 15),
    datetime(2024, 1, 15), datetime(2024, 7, 15),
    datetime(2025, 1, 15), datetime(2025, 7, 15),
    datetime(2026, 1, 15), datetime(2026, 7, 15),
]
EARLY_PROBE_DATES = [
    datetime(2022, 1, 15), datetime(2021, 6, 15), datetime(2021, 1, 15),
    datetime(2020, 1, 15), datetime(2018, 1, 15), datetime(2015, 1, 15),
]


def check_hrrr(date):
    prefix = f"hrrr.{date:%Y%m%d}/conus/"
    objs = hrrr.list_objects(prefix, max_keys=5)
    return len(objs) > 0, [o.get("key") for o in objs[:2]]


def check_gfs(date):
    prefix = f"gfs.{date:%Y%m%d}/12/atmos/"
    objs, _ = gfs.list_objects(prefix, max_keys=5)
    return len(objs) > 0, [o.get("key") for o in objs[:2]]


def check_gefs(date):
    prefix = f"gefs.{date:%Y%m%d}/12/atmos/pgrb2sp25/"
    objs, _ = gefs.list_objects(prefix, max_keys=5)
    return len(objs) > 0, [o.get("key") for o in objs[:2]]


def check_nbm(date):
    prefix = f"blend.{date:%Y%m%d}/12/core/"
    objs, _ = nbm.list_objects(prefix, max_keys=5)
    return len(objs) > 0, [o.get("key") for o in objs[:2]]


def check_ecmwf(date):
    prefix = f"{date:%Y%m%d}/12z/ifs/0p25/oper/"
    try:
        objs, _ = ecmwf.list_objects(prefix, max_keys=5)
    except Exception as e:
        return False, [f"ERROR: {e}"]
    return len(objs) > 0, [o.get("key") for o in objs[:2]]


def check_observation_year(year, usaf, wban):
    url = f"https://noaa-global-hourly-pds.s3.amazonaws.com/{year}/{usaf}{wban}.csv"
    try:
        r = requests.head(url, timeout=15)
        size = r.headers.get("Content-Length")
        return r.status_code == 200, size
    except Exception as e:
        return False, str(e)


def main():
    print("=" * 100)
    print("HRRR (bucket noaa-hrrr-bdp-pds, prefix listing only)")
    print("=" * 100)
    for d in DATES + EARLY_PROBE_DATES:
        ok, sample = check_hrrr(d)
        print(f"  {d:%Y-%m-%d}: {'EXISTS' if ok else 'MISSING'}  sample={sample}")

    print("\n" + "=" * 100)
    print("GFS (bucket noaa-gfs-bdp-pds, prefix listing only)")
    print("=" * 100)
    for d in DATES + EARLY_PROBE_DATES:
        ok, sample = check_gfs(d)
        print(f"  {d:%Y-%m-%d}: {'EXISTS' if ok else 'MISSING'}  sample={sample}")

    print("\n" + "=" * 100)
    print("GEFS (bucket noaa-gefs-pds, prefix listing only)")
    print("=" * 100)
    for d in DATES + EARLY_PROBE_DATES:
        ok, sample = check_gefs(d)
        print(f"  {d:%Y-%m-%d}: {'EXISTS' if ok else 'MISSING'}  sample={sample}")

    print("\n" + "=" * 100)
    print("NBM (bucket noaa-nbm-grib2-pds, prefix listing only)")
    print("=" * 100)
    for d in DATES + EARLY_PROBE_DATES:
        ok, sample = check_nbm(d)
        print(f"  {d:%Y-%m-%d}: {'EXISTS' if ok else 'MISSING'}  sample={sample}")

    print("\n" + "=" * 100)
    print("ECMWF (bucket ecmwf-forecasts, prefix listing only)")
    print("=" * 100)
    for d in DATES:
        ok, sample = check_ecmwf(d)
        print(f"  {d:%Y-%m-%d}: {'EXISTS' if ok else 'MISSING'}  sample={sample}")

    print("\n" + "=" * 100)
    print("Surface observations (bucket noaa-global-hourly-pds, HEAD requests only)")
    print("=" * 100)
    stations = {
        "KNYC": ("725053", "94728"),
        "KLGA": ("725030", "14732"),
        "KJFK": ("744860", "94789"),
        "KEWR": ("725020", "14734"),
    }
    for label, (usaf, wban) in stations.items():
        for year in [2022, 2023, 2024, 2025, 2026]:
            ok, size = check_observation_year(year, usaf, wban)
            print(f"  {label} {year}: {'EXISTS' if ok else 'MISSING'}  size={size}")


if __name__ == "__main__":
    main()
