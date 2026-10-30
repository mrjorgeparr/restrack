# Getting started

## Install

```bash
pip install restrack            # core
pip install "restrack[gpu]"     # + NVIDIA GPU metrics (nvidia-ml-py)
```

Requirements:

- Python 3.9 or newer.
- `psutil` is installed automatically.

### Installing without internet access

The wheel is pure Python, so the same file works on every OS. Copy
`restrack-<version>-py3-none-any.whl` plus a `psutil` wheel matching the target
machine's OS and Python version into one folder, then:

```bash
pip install --no-index --find-links . restrack-<version>-py3-none-any.whl
```

## First run

### 1. Look at the machine right now

```bash
restrack snapshot
```

This prints a one-off summary: CPU, per-core bars, memory, disk, network and the top processes.

### 2. Watch it live in the terminal

```bash
restrack top            # Ctrl+C to quit
restrack top -s mem     # sort by memory (also: cpu, io)
```

### 3. Open the Resource Monitor UI

```bash
restrack web --open
```

This starts sampling every second and serves the UI on <http://localhost:8765>.
With default settings it also:

- writes a long-term metrics log (see [Storage & alerts](storage-and-alerts.md));
- writes a text log of starts, stops, alerts and errors;
- checks the alert thresholds.

Stop it with Ctrl+C.

### 4. Keep logging without the UI

```bash
restrack run
```

This does the same sampling, storage and alerts, headless. Run it as a service or login
item if you want history to accumulate all the time (see
[Running in the background](storage-and-alerts.md#running-in-the-background)).

### 5. Look back

```bash
restrack history 24h
```

Or open the **History** tab in the UI.

## Configure it

```bash
restrack config init     # writes ~/.config/restrack/config.toml (Windows: %APPDATA%\restrack\config.toml)
restrack config path     # which file is being used
restrack config show     # the effective settings (file merged over defaults)
```

Every key is optional. See [Configuration](configuration.md) for all of them.
