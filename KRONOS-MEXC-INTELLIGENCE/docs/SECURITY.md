# Security controls

The dashboard uses signed cookie sessions and CSRF checks for state-changing authenticated routes. Production requires a random secret and strong admin password. Put the service behind HTTPS; cookies are Secure when `APP_ENV=production`. Restrict allowed CORS origins if a separate frontend domain is configured. Keep `.env`, access tokens and database URLs outside version control.

This package is a single-admin research dashboard. This is a single-admin research dashboard. Before multi-user production use, add role-based user records, password hashing/rotation for stored user credentials, login rate limiting backed by a shared store, formal session revocation, HSTS at the trusted TLS edge, audit policy and a formal threat model. The app currently includes baseline security headers and a CSP, but has not undergone a professional penetration test. Never expose `/docs` or admin access publicly without an access-control decision.
