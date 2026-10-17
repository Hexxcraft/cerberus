import os

os.environ.setdefault("CERBERUS_NO_WARDEN", "1")   # tests must never start the real screen watcher
os.environ.setdefault("CERBERUS_NO_TELEMETRY", "1")   # ...or write to a real review log (the telemetry tests turn it back on)
