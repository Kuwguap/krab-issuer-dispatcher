# External Lead Ingest API

Integrate another website with krableadsV2 so orders are submitted over **HTTP** instead of forwarding messages through Telegram.

## Overview

1. The other site POSTs a formatted **New Lead** message to krableadsV2 admin API.
2. krableadsV2 encrypts the phone, saves the lead in Supabase, and returns a **Reference ID**.
3. Within ~10 seconds, the **krab-issuer-bot** worker posts **Accept** buttons to **all active dispatch groups**.
4. The **first team to Accept** wins; the normal dispatch flow continues (Monday.com, driver DMs, etc.).

**Do not** forward leads to `@KrabIssuerBot` from the other site once this API is enabled.

---

## Prerequisites (krableadsV2 / Render)

1. Run migration in Supabase SQL Editor:

   `database/migration_lead_api_ingest.sql`

2. Set env vars on **krab-issuer-admin** (Render web service):

   | Variable | Description |
   |----------|-------------|
   | `LEAD_INGEST_API_KEY` | Secret Bearer token (generate a long random string) |
   | `API_LEAD_USER_ID` | Virtual issuer for API leads — default `tristatetag` (@username). Resolved to Telegram numeric id via past bot activity or optional `TELEGRAM_BOT_TOKEN` + getChat |
   | `LEAD_INGEST_SOURCE_LABEL` | Optional; default `External API` |
   | `ONETIMESECRET_*` | Optional for ingest — if unset, API leads store the raw phone and still dispatch |
   | `TELEGRAM_BOT_TOKEN` | Optional on admin — helps resolve `@tristatetag` to numeric id on ingest |

3. Set on **krab-issuer-bot** worker:

   | Variable | Description |
   |----------|-------------|
   | `API_LEAD_USER_ID` | Same value as admin (default `tristatetag`) |

   **Important:** `@tristatetag` must have opened a DM with the issuer bot and tapped **Start** at least once so their Telegram id can be resolved. Or set `API_LEAD_USER_ID` to their numeric id instead.

4. Redeploy both services after env changes.

---

## Message format

Build this text on the other site (label names matter):

```
🆕 New Lead
Order #bf6923ca
Customer: Zebin Fang Fang
Phone: 2138622301
Delivery email: zebinfang1002@gmail.com
Delivery method: Email Delivery
Registration address: 28 brookside rd quincy MA 02169
Delivery address: 28 brookside rd quincy MA 02169
VIN: SCA665C56HUX86704
Vehicle: 2017 ROLLS-ROYCE Wraith, black
Insurance: AC Insurance
Policy #: 279-06071-913
Service: 30-Day NJ Temp Tag
Price: $150.00
```

### Required fields

- `Customer`
- `Phone` (9–10 digit US number)
- `Price` (must include `$` and a digit)
- `VIN` (17 characters)
- `Vehicle` (year make model; optional color after comma)
- `Registration address` and/or `Delivery address`

### Recommended

- `Order #` — stored as `external_order_id`; use for idempotency on your side
- `Delivery email`
- `Insurance`, `Policy #`, `Service`, `Delivery method`

---

## API

**URL:** `https://krab-issuer-admin.onrender.com/api/v1/leads/ingest`

Example Render URL: `https://krab-issuer-admin.onrender.com/api/v1/leads/ingest`

**Method:** `POST`

**Headers:**

```
Authorization: Bearer <LEAD_INGEST_API_KEY>
Content-Type: text/plain
```

**Body:** full message text (UTF-8)

### JSON alternative

```
Content-Type: application/json
```

```json
{
  "message": "🆕 New Lead\nOrder #bf6923ca\nCustomer: ..."
}
```

Or structured fields:

```json
{
  "fields": {
    "name": "Zebin Fang Fang",
    "phone": "2138622301",
    "price": "$150.00",
    "vin": "SCA665C56HUX86704",
    "vehicle": "2017 ROLLS-ROYCE Wraith, black",
    "registration address": "28 brookside rd quincy MA 02169",
    "delivery address": "28 brookside rd quincy MA 02169",
    "email": "zebinfang1002@gmail.com",
    "external_order_id": "bf6923ca"
  }
}
```

### Success (201)

```json
{
  "ok": true,
  "lead_id": "uuid",
  "reference_id": "A1B2C3D4",
  "external_order_id": "bf6923ca"
}
```

Store `reference_id` on your order record for support and tracking.

### Errors

| Status | Meaning |
|--------|---------|
| 401 | Missing or wrong `Authorization: Bearer` token |
| 422 | Parse/validation error — see `errors` array in body |
| 500 | Server/DB/encryption failure |
| 503 | `LEAD_INGEST_API_KEY` not configured on admin |

---

## Integration examples

### curl

```bash
curl -X POST "https://krab-issuer-admin.onrender.com/api/v1/leads/ingest" \
  -H "Authorization: Bearer YOUR_SECRET_KEY" \
  -H "Content-Type: text/plain" \
  --data-binary @lead.txt
```

### Node.js

```javascript
function buildLeadMessage(order) {
  return [
    "🆕 New Lead",
    `Order #${order.id}`,
    `Customer: ${order.customerName}`,
    `Phone: ${order.phone}`,
    `Delivery email: ${order.email}`,
    `Delivery method: ${order.deliveryMethod || "Email Delivery"}`,
    `Registration address: ${order.registrationAddress}`,
    `Delivery address: ${order.deliveryAddress || order.registrationAddress}`,
    `VIN: ${order.vin}`,
    `Vehicle: ${order.vehicleYear} ${order.vehicleMake} ${order.vehicleModel}, ${order.color}`,
    `Insurance: ${order.insuranceCompany}`,
    `Policy #: ${order.policyNumber}`,
    `Service: ${order.serviceName}`,
    `Price: $${order.price.toFixed(2)}`,
  ].join("\n");
}

async function submitLeadToKrableads(order) {
  if (order.krableadsReferenceId) {
    return order.krableadsReferenceId; // idempotency — already submitted
  }
  const message = buildLeadMessage(order);
  const res = await fetch(process.env.KRABLEADS_INGEST_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.KRABLEADS_INGEST_API_KEY}`,
      "Content-Type": "text/plain",
    },
    body: message,
  });
  const body = await res.json();
  if (!res.ok) {
    throw new Error(body.errors?.join("; ") || body.error || res.statusText);
  }
  await saveReferenceId(order.id, body.reference_id);
  return body.reference_id;
}
```

### PHP

```php
$message = implode("\n", [
    "🆕 New Lead",
    "Order #{$order['id']}",
    "Customer: {$order['customer_name']}",
    "Phone: {$order['phone']}",
    // ... remaining lines
    "Price: \$" . number_format($order['price'], 2),
]);

$ch = curl_init(getenv('KRABLEADS_INGEST_URL'));
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_POSTFIELDS => $message,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . getenv('KRABLEADS_INGEST_API_KEY'),
        'Content-Type: text/plain',
    ],
    CURLOPT_RETURNTRANSFER => true,
]);
$response = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);
```

---

## Disable Telegram forwarding on the other site

1. Remove or disable any script/bot that sends the New Lead text to `@KrabIssuerBot` or the issuer Telegram group.
2. Replace that step with `submitLeadToKrableads(order)` (or equivalent) **server-side** when an order is paid/ready.
3. Never expose `LEAD_INGEST_API_KEY` in browser JavaScript — call your own backend, which calls krableadsV2.

---

## What happens after ingest

Same as a human issuer submitting via Telegram:

1. Phone encrypted (clientsphonenumber / OneTimeSecret)
2. Lead row created with Reference ID
3. Accept buttons sent to **all active groups** (~10s)
4. First group Accept → drivers get Accept/Decline DMs
5. Driver accept → full client details, Monday sync, renewals, receipts

---

## Troubleshooting

- **422 Invalid phone** — send 10-digit US number without formatting issues
- **422 Invalid price** — must include `$` (e.g. `$150.00`)
- **Lead saved but no Telegram Accept buttons** — check `krab-issuer-bot` logs and `API_LEAD_USER_ID`; confirm migration ran
- **401** — rotate `LEAD_INGEST_API_KEY` and update the other site

Run parser tests locally:

```bash
cd krableadsV2
python -m unittest tests.test_external_lead_parser
```
