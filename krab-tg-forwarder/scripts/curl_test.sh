#!/usr/bin/env bash
# Smoke test Service B without Telegram (uses POST /test).
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8090}"
KEY="${FORWARDER_API_KEY:-change-me-dev-key}"

echo "== health =="
curl -sf "$BASE/health" | head -c 200
echo

echo "== POST /test (dry run) =="
curl -sf -X POST "$BASE/test" \
  -H "Content-Type: application/json" \
  -H "X-Forwarder-Key: $KEY" \
  -d '{
    "name": "Ed Castello",
    "address": "98 Academy Street",
    "city_state_zip": "Poughkeepsie, NY 12601",
    "delivery_address": "98 Academy Street",
    "delivery_city_state_zip": "Poughkeepsie, NY 12601",
    "vin": "1HGBH41JXMN109186",
    "year_make_model": "2020 Honda Accord",
    "color": "Tan",
    "current_insurance": "GEICO",
    "policy_number": "POL-123",
    "delivery_time": "ASAP 1 hour",
    "phone": "845-941-0159",
    "email": "client@example.com",
    "fb_psid": "test_psid_001"
  }'
echo
echo "OK — forwarder test endpoint works."
