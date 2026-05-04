# Development Notes

## API compatibility
The Python bot uses `utils/onetimesecret.py` which calls a OneTimeSecret-compatible API:
- `POST /api/v1/share`
- Basic auth using `ONETIMESECRET_USERNAME` / `ONETIMESECRET_API_KEY`
- Accepts form fields: `secret`, `passphrase`, `ttl`

This app implements the same endpoint and returns:
- `secret_key`
- `metadata_key`

## Supabase tables
- `clientsphonenumber_config` (single row id=1)
- `clientsphonenumber_secrets` (key/value store for secrets)

## Unlock behavior
Unlock checks:
1. Secret exists and is not marked deleted
2. If expiration is enabled, `expires_at` has not passed
3. Passphrase matches admin-configured passphrase
4. Optionally deletes the secret after unlock (toggle default OFF)

