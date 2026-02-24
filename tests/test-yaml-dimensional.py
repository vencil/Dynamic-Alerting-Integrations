#!/usr/bin/env python3
"""Quick test: verify PyYAML handles dimensional keys correctly."""
import yaml

# Simulate what patch_config.py does
d = {
    "tenants": {
        "db-a": {
            'redis_queue_length{queue="tasks"}': "500",
            'redis_queue_length{queue="events", priority="high"}': "1000:critical",
            "mysql_connections": "70",
        }
    }
}

dumped = yaml.dump(d, sort_keys=False)
print("=== YAML Output ===")
print(dumped)

# Round-trip test
loaded = yaml.safe_load(dumped)
print("=== Round-trip Test ===")
tenant = loaded["tenants"]["db-a"]
assert tenant['redis_queue_length{queue="tasks"}'] == "500", "Failed: single-label key"
assert tenant['redis_queue_length{queue="events", priority="high"}'] == "1000:critical", "Failed: multi-label key"
assert tenant["mysql_connections"] == "70", "Failed: base key"
print("All 3 round-trip assertions PASSED")
