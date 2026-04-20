# Security Policy

## Supported Use

This repository is public, but it is not configured for zero-touch deployment. Operators are responsible for:

- supplying strong secrets
- restricting infrastructure access
- validating provider callback configuration
- rotating credentials before production use

## Sensitive Configuration

At minimum, set and protect these values:

- `APP_SECRET_KEY`
- `BOT_TOKEN`
- `ADMIN_API_KEY`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `XUI_PASSWORD`
- `NOWPAYMENTS_API_KEY`
- `NOWPAYMENTS_IPN_SECRET`
- `TETRAPAY_API_KEY`

## Reporting

If you discover a security issue, do not open a public issue with exploit details.

Contact the repository owner privately through GitHub before public disclosure.

## Current Cautions

- Production deployments should always define `NOWPAYMENTS_IPN_SECRET`.
- Production deployments should always define a strong `REDIS_PASSWORD`.
- Review callback URLs carefully before enabling live payment flows.
