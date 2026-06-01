#!/usr/bin/env bash
# Smoke test Service A without Meta or OpenAI (replay endpoint still needs OPENAI for full flow).
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8080}"

echo "== health =="
curl -sf "$BASE/health" | head -c 200
echo

echo "== POST /admin/replay =="
curl -sf -X POST "$BASE/admin/replay" \
  -H "Content-Type: application/json" \
  -d '{"psid":"curl_test_psid","text":"Hi, my name is Jane Doe. Email jane@example.com phone 845-555-1234"}'
echo
echo "OK — replay endpoint responded (check slots in JSON)."

echo "== sample Meta webhook payload (optional; may 403 without valid signature) =="
cat <<'EOF' | curl -s -X POST "$BASE/webhook" \
  -H "Content-Type: application/json" \
  -d @- || true
{
  "object": "page",
  "entry": [{
    "messaging": [{
      "sender": {"id": "meta_test_psid"},
      "message": {"text": "Hello from webhook test"}
    }]
  }]
}
EOF
echo
echo "Done. For signed webhooks set META_APP_SECRET or leave unset to skip verification."
