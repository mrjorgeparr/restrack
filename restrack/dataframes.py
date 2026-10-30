"""pandas memory diagnostics: where a DataFrame's memory goes and how to shrink it safely.

    from restrack.dataframes import memory_report, optimize

    memory_report(df)            # one row per column: memory, dtype, suggestion, saving
    small = optimize(df)         # apply the safe suggestions

Float columns are only narrowed to float32 when the values survive the round trip
at the precision they actually carry (see :func:`float32_precision`), so amounts
with cents are never silently changed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

_DATE_LIKE = r"^\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})"
_INT_TYPES = [np.int8, np.int16, np.int32, np.int64]
_UINT_TYPES = [np.uint8, np.uint16, np.uint32, np.uint64]
_NULLABLE = {np.int8: "Int8", np.int16: "Int16", np.int32: "Int32", np.int64: "Int64",
             np.uint8: "UInt8", np.uint16: "UInt16", np.uint32: "UInt32", np.uint64: "UInt64"}


def _pd():
    import pandas as pd  # imported lazily: pandas is optional for restrack
    return pd


def _smallest_int(lo, hi):
    types = _UINT_TYPES if lo >= 0 else _INT_TYPES
    for t in types:
        info = np.iinfo(t)
        if info.min <= lo and hi <= info.max:
            return t
    return None


def _decimals(values: np.ndarray, max_decimals: int = 6) -> Optional[int]:
    """Smallest number of decimal places that represents every value exactly (None if > max)."""
    if values.size == 0:
        return 0
    for d in range(max_decimals + 1):
        if np.allclose(np.round(values, d), values, rtol=0, atol=10.0 ** -(d + 3)):
            return d
    return None


def float32_precision(values, sample: int = 200_000) -> Dict[str, Any]:
    """Would casting these float64 values to float32 lose information?

    Returns the maximum absolute and relative round-trip error, the number of decimal
    places the data appears to carry, and ``safe``: True when every value rounded back
    to that many decimals is unchanged (e.g. prices with cents stay exact).
    """
    arr = np.asarray(values, dtype="float64")
    arr = arr[np.isfinite(arr)]
    if arr.size > sample:
        idx = np.random.default_rng(0).choice(arr.size, sample, replace=False)
        sub = arr[idx]
        # always include the extremes: precision is worst at the largest magnitudes
        sub = np.concatenate([sub, arr[[np.argmax(np.abs(arr))]]])
    else:
        sub = arr
    if sub.size == 0:
        return {"safe": True, "max_abs_error": 0.0, "max_rel_error": 0.0, "decimals": 0, "sampled": False}
    with np.errstate(over="ignore"):  # values beyond float32 range become inf: reported as unsafe
        back = sub.astype(np.float32).astype(np.float64)
    abs_err = np.abs(back - sub)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(sub != 0, abs_err / np.abs(sub), 0.0)
    d = _decimals(sub)
    if np.isinf(back).any():
        safe = False
    elif d is None:
        safe = bool(abs_err.max() == 0)
    else:
        safe = bool(np.array_equal(np.round(back, d), np.round(sub, d)))
    return {"safe": safe, "max_abs_error": float(abs_err.max()), "max_rel_error": float(rel.max()),
            "decimals": d, "sampled": arr.size > sample}


def _is_text(s) -> bool:
    pd = _pd()
    return s.dtype == object or isinstance(s.dtype, pd.StringDtype)


def _analyse(name, s, n, sample: int, unique_ratio: float) -> Dict[str, Any]:
    pd = _pd()
    mem = int(s.memory_usage(index=False, deep=True))
    row = {"column": str(name), "dtype": str(s.dtype), "memory": mem, "nulls": int(s.isna().sum()),
           "n_unique": None, "unique_ratio": None, "suggested": None, "est_memory": None,
           "saving": 0, "note": ""}
    if n == 0:
        return row
    big = n > sample
    view = s.sample(sample, random_state=0) if big else s

    if _is_text(s):
        nu = int(view.nunique(dropna=True))
        ratio = nu / max(1, view.notna().sum())
        row["n_unique"], row["unique_ratio"] = (nu if not big else None), round(ratio, 4)
        non_null = view.dropna()
        sample_vals = non_null.head(2000)
        # numbers or dates stored as text?
        if len(sample_vals):
            num = pd.to_numeric(sample_vals, errors="coerce")
            if num.notna().mean() > 0.99:
                integral = bool(np.all(np.mod(num.dropna().to_numpy(dtype="float64"), 1) == 0))
                row.update(suggested="int64" if integral and not row["nulls"] else "float64",
                           est_memory=8 * n, note="numbers stored as text: pd.to_numeric()")
            elif sample_vals.astype(str).str.match(_DATE_LIKE).mean() > 0.99:
                try:
                    dt = pd.to_datetime(sample_vals, errors="coerce", format="mixed")
                    if dt.notna().mean() > 0.99:
                        row.update(suggested="datetime64[ns]", est_memory=8 * n,
                                   note="dates stored as text: pd.to_datetime() (also enables .dt)")
                except (TypeError, ValueError):
                    pass
        if row["suggested"] is None and ratio <= unique_ratio:
            uniques = view.dropna().drop_duplicates()
            per_unique = uniques.memory_usage(index=False, deep=True) / max(1, len(uniques))
            n_cat = nu if not big else int(min(n, ratio * n))
            code_t = _smallest_int(-1, max(1, n_cat))
            est = np.dtype(code_t).itemsize * n + int(per_unique * n_cat)
            row.update(suggested="category", est_memory=est,
                       note=f"{'≈' if big else ''}{n_cat:,} distinct values repeated")
        elif row["suggested"] is None:
            try:
                import pyarrow  # noqa: F401
                lengths = view.dropna().astype(str).str.len().mean() if len(view.dropna()) else 0
                est = int((lengths + 8) * n)
                if est < mem * 0.8:
                    row.update(suggested="string[pyarrow]", est_memory=est,
                               note="mostly unique text: Arrow strings store it compactly")
            except ImportError:
                row["note"] = "mostly unique text (install pyarrow for a compact string dtype)"

    elif pd.api.types.is_bool_dtype(s.dtype) or isinstance(s.dtype, pd.CategoricalDtype):
        pass
    elif pd.api.types.is_integer_dtype(s.dtype) and not isinstance(s.dtype, pd.api.extensions.ExtensionDtype):
        lo, hi = s.min(), s.max()
        t = _smallest_int(lo, hi)
        if t is not None and np.dtype(t).itemsize < s.dtype.itemsize:
            row.update(suggested=np.dtype(t).name, est_memory=np.dtype(t).itemsize * n,
                       note=f"values {lo:,}..{hi:,}")
    elif pd.api.types.is_float_dtype(s.dtype) and s.dtype.itemsize == 8:
        vals = s.to_numpy(dtype="float64", na_value=np.nan)
        finite = vals[np.isfinite(vals)]
        if finite.size and np.all(np.mod(finite, 1) == 0):
            t = _smallest_int(finite.min(), finite.max())
            if t is not None:
                if row["nulls"]:
                    row.update(suggested=_NULLABLE[t], est_memory=(np.dtype(t).itemsize + 1) * n,
                               note="whole numbers with NaN: nullable integer keeps them exact")
                else:
                    row.update(suggested=np.dtype(t).name, est_memory=np.dtype(t).itemsize * n,
                               note="whole numbers stored as float")
        if row["suggested"] is None:
            p = float32_precision(vals)
            row["float32"] = p
            dec = "unknown" if p["decimals"] is None else p["decimals"]
            if p["safe"]:
                row.update(suggested="float32", est_memory=4 * n,
                           note=f"float32 keeps the {dec} decimals present (max abs error {p['max_abs_error']:.2g})")
            else:
                row["note"] = (f"keep float64: float32 would change values (max abs error {p['max_abs_error']:.3g}, "
                               f"data has {dec} decimals)")
    if row["est_memory"] is not None:
        row["saving"] = max(0, mem - row["est_memory"])
        if row["saving"] == 0:
            row["suggested"], row["est_memory"] = None, None
    return row


def memory_report(df, deep: bool = True, as_frame: bool = True, sample: int = 1_000_000,
                  unique_ratio: float = 0.5):
    """Per-column memory use and dtype suggestions.

    Columns: ``column, dtype, memory, pct, nulls, n_unique, unique_ratio, suggested,
    est_memory, saving, note``. Cardinality is estimated on a ``sample`` of rows for
    larger frames. ``as_frame=False`` returns ``{"columns": [...], "total": …, …}``.
    """
    pd = _pd()
    if isinstance(df, pd.Series):
        df = df.to_frame()
    n = len(df)
    rows = [_analyse(c, df[c], n, sample, unique_ratio) for c in df.columns]
    idx_mem = int(df.index.memory_usage(deep=deep))
    total = sum(r["memory"] for r in rows) + idx_mem
    for r in rows:
        r["pct"] = round(r["memory"] / total * 100, 2) if total else 0.0
    saving = sum(r["saving"] for r in rows)
    summary = {"rows": n, "columns_count": len(rows), "total": total, "index": idx_mem,
               "est_total": total - saving, "saving": saving,
               "saving_pct": round(saving / total * 100, 1) if total else 0.0}
    if not as_frame:
        return {**summary, "columns": rows}
    out = pd.DataFrame(rows, columns=["column", "dtype", "memory", "pct", "nulls", "n_unique", "unique_ratio",
                                      "suggested", "est_memory", "saving", "note"])
    out = out.sort_values("memory", ascending=False, ignore_index=True)
    out.attrs.update(summary)
    return out


def optimize(df, *, categories: bool = True, integers: bool = True, floats: str = "safe",
             text_to_numbers: bool = False, strings: bool = False, unique_ratio: float = 0.5,
             verbose: bool = False):
    """Return a copy of ``df`` with memory-saving dtypes applied.

    floats: ``"safe"`` (default) narrows to float32 only where values stay exact at
    their decimal precision; ``True`` narrows all float64; ``False`` leaves them.
    text_to_numbers / strings: also convert numeric/date text and use Arrow strings.
    """
    pd = _pd()
    rep = memory_report(df, as_frame=False, unique_ratio=unique_ratio)
    out = df.copy()
    changed: List[str] = []
    for r in rep["columns"]:
        col = df.columns[[str(c) for c in df.columns].index(r["column"])]
        sug = r["suggested"]
        try:
            if sug == "category" and categories:
                out[col] = out[col].astype("category")
            elif sug in ("int8", "int16", "int32", "uint8", "uint16", "uint32") and integers:
                out[col] = out[col].astype(sug)
            elif sug in _NULLABLE.values() and integers:
                out[col] = out[col].astype(sug)
            elif sug == "float32" and floats:
                out[col] = out[col].astype("float32")
            elif sug in ("int64", "float64") and text_to_numbers:
                out[col] = pd.to_numeric(out[col], errors="coerce")
            elif sug == "datetime64[ns]" and text_to_numbers:
                out[col] = pd.to_datetime(out[col], errors="coerce", format="mixed")
            elif sug == "string[pyarrow]" and strings:
                out[col] = out[col].astype("string[pyarrow]")
            elif floats is True and sug is None and r.get("float32"):
                out[col] = out[col].astype("float32")
                changed.append(f"{r['column']}: float64 -> float32 (forced; values change, max abs error "
                               f"{r['float32']['max_abs_error']:.3g})")
                continue
            else:
                continue
            changed.append(f"{r['column']}: {r['dtype']} -> {sug}")
        except (TypeError, ValueError) as e:
            changed.append(f"{r['column']}: skipped ({e})")
    if verbose:
        before = rep["total"]
        after = int(out.memory_usage(deep=True).sum())
        print(f"optimize: {before / 2**20:.1f} MB -> {after / 2**20:.1f} MB")
        for c in changed:
            print("  " + c)
    return out
