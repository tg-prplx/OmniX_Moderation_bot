# Security Policy

We take the security of Spisdil Moderation Bot seriously. This document outlines how to report vulnerabilities and what to expect once you do.

## Supported Versions

| Version | Supported |
|---------|-----------|
| `master` branch | ✅ |
| Tagged releases in the last 12 months | ✅ |
| Older releases | ⚠️ Security fixes provided on a best-effort basis only |

## Reporting a Vulnerability

1. **Do not create a public issue.**  
   Email security reports to **security@your-org.example** (replace with your organisation’s contact) or open a private security advisory on GitHub.

2. **Provide details**:
   - Affected component (e.g., `ChatGPTLayer`, `RuleService`, admin DM panel).
   - Steps to reproduce, including payload samples when possible.
   - Impact assessment (data exposure, privilege escalation, service interruption, etc.).
   - Suggested remediation or patches, if available.

3. **Encrypt if necessary**: If you prefer encrypted communication, request our PGP key via the contact above.

## Disclosure Process

1. We acknowledge receipt within **3 business days**.
2. We investigate, reproduce, and assess severity. You may be contacted for additional information.
3. Once confirmed, we coordinate a fix and plan a disclosure timeline. We aim to release patches within **14 days** for critical issues.
4. After a fix is published, we notify the reporter and provide recommendations for mitigation.
5. With your consent, we credit reporters in release notes.

## Security Best Practices

- Rotate API keys and bot tokens regularly.
- Store secrets in Vaults or encrypted CI variables; never commit `.env`.
- Run the bot with the minimum Telegram permissions necessary for moderation.
- Monitor structured logs (`chatgpt_violation`, `rule_added`, `telegram_decision_error`) for anomalies.
- Configure retention policies for `moderation.db` or your external storage.

## Responsible Disclosure

We appreciate coordinated disclosure. Please avoid exploiting vulnerabilities beyond what is necessary to demonstrate the issue and respect user privacy at all times.

Thank you for helping keep Spisdil Moderation Bot safe!
