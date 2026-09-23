# Repository development rules

- This is an internet-facing URL-fetching service. Treat every supplied URL and all fetched content as untrusted.
- SSRF protection is a critical security boundary. Do not weaken URL, DNS, address, redirect, or response-size checks casually.
- Never commit credentials, tokens, or other secrets. Keep `EXTRACT_API_TOKEN` in the deployment environment.
- Production changes are Git-driven. Codex may edit, test, and commit locally, but MUST NOT PUSH.
- Keep dependencies and architecture lightweight. Do not add browser automation without explicit approval.
- Generate review diffs outside the repository.
- Use Luna for normal implementation work and escalate only when genuinely needed.
