# Data & numeric precision

pandas often uses far more memory than the data needs: `float64` for values with two
decimals, `int64` for numbers below 200, `object` strings for a column with three
distinct values. restrack measures every column and suggests smaller dtypes **only where
the values stay exactly the same**.

Requires `pandas ≥ 2.0` (`pip install "restrack[pandas]"`).

```bash
restrack dtypes transactions.csv
```

```text
transactions.csv: 400,000 rows x 5 columns, 48.4 MB in memory -> 6.9 MB with suggested dtypes (85.8% smaller)

column      dtype     memory      %  suggested   saving  note
cur         str      19.8 MB   40.9  category   19.5 MB  3 distinct values repeated
country     str      19.5 MB   40.2  category   19.1 MB  5 distinct values repeated
id          int64     3.1 MB    6.3  uint32      1.5 MB  values 0..399,999
amount      float64   3.1 MB    6.3  float32     1.5 MB  float32 keeps the 2 decimals present (max abs error 2.9e-05)
score       float64   3.1 MB    6.3  -                   keep float64: float32 would change values (max abs error 2.98e-08, ...)
```

```bash
restrack dtypes transactions.csv --write transactions.parquet   # optimised copy (needs pyarrow)
restrack dtypes big.csv --rows 1000000                          # analyse a sample
```

In Python:

```python
from restrack.dataframes import memory_report, optimize, float32_precision

memory_report(df)               # DataFrame: one row per column (summary in .attrs)
small = optimize(df)            # copy with the safe suggestions applied
prof.inspect(df, "orders")      # inside a Profiler: adds the table to the HTML report
```

## How float precision is checked

`float32` keeps about 7 significant digits, while `float64` keeps about 16. Whether
that loses anything depends on the data, not the dtype:

| Values | float32? | Why |
|---|---|---|
| `19.99`, `250.00`, `1234.56` (prices) | ✅ safe | rounded back to 2 decimals they're identical |
| `123456789.12` (large amounts with cents) | ❌ keep float64 | 11 significant digits: the cents would change |
| `0.7364918253…` (measurements, model scores) | ❌ keep float64 | every digit is meaningful and float32 rounds it |
| `1e300` | ❌ keep float64 | outside float32's range |

`float32_precision(values)` does the work:

1. It finds how many decimal places the data actually carries (up to 6), by checking
   which rounding leaves every value unchanged.
2. It converts to float32 and back.
3. It calls the column **safe** only if every value, rounded to those decimals, is
   unchanged. It also reports the maximum absolute and relative error.

Large columns are checked on a 200,000-row random sample that always includes the
largest magnitude, since that's where float32 is least precise.

## Other suggestions

| Column | Suggestion | When |
|---|---|---|
| integers | smallest `int8`…`int32` / `uint8`…`uint32` that fits min..max | always exact |
| whole numbers stored as float (often because of NaN) | nullable `Int8`…`UInt32` | keeps NaN as `<NA>` |
| text with few distinct values | `category` | distinct ≤ 50 % of rows (`unique_ratio`) |
| numbers stored as text | `int64` / `float64` | > 99 % of values parse; converted only with `text_to_numbers=True` |
| dates stored as text | `datetime64[ns]` | same rule |
| mostly-unique text | `string[pyarrow]` | if pyarrow is installed; applied only with `strings=True` |

`optimize()` options:

| Parameter | Default | |
|---|---|---|
| `categories` | `True` | apply category suggestions |
| `integers` | `True` | downcast integers and whole-number floats |
| `floats` | `"safe"` | `"safe"`: only precision-checked columns; `True`: every float64 (values change); `False`: none |
| `text_to_numbers` | `False` | convert numeric and date text |
| `strings` | `False` | Arrow strings for unique text |
| `verbose` | `False` | print before/after and each change |

!!! tip "CSV forgets dtypes"
    Reading a CSV gives back the large defaults every time. Save the optimised frame
    as Parquet (`--write file.parquet` or `df.to_parquet`) to keep the small dtypes,
    and read only the columns you need (`pd.read_parquet(path, columns=[...])`).
