# Security Policy

## Project status

NEXUS SCADA is a **research prototype and reference architecture**, not a
certified industrial product. It is intended for local execution, education,
and prototype validation. It is **not** designed for deployment in
safety-critical or internet-exposed environments.

See the **Known Limitations** and **Security & Responsible Use** sections of
the [README](./README.md) for the full boundary description.

## Reporting a vulnerability

If you discover a security issue, please **do not open a public GitHub issue**.
Instead, use one of these channels:

- GitHub's private vulnerability reporting: *Security* tab → *Report a vulnerability*
- Email the maintainer listed under **Team** in the [README](./README.md)

Please include:
- A short description of the issue and its impact.
- Steps to reproduce, or a minimal proof of concept.
- The commit hash or release version affected.
- Any suggested remediation, if you have one.

## What to expect

This is a volunteer-maintained research prototype. Expect:
- An acknowledgment within **7 days**.
- An assessment within **30 days**.
- Public disclosure only after a fix is available and coordinated.

## Scope

In scope:
- Command injection, path traversal, or file write outside the project tree.
- Authentication bypass in `backend/api.py` when `NEXUS_API_KEY` is set.
- Remote code execution via the LLM reasoner path.
- Any unsafe interaction with Modbus, PLC registers, or the remediation engine
  that violates the documented safety contract.

Out of scope:
- Anything requiring a malicious local operator with filesystem access.
- Missing TLS / auth on the Modbus simulator (documented limitation).
- Missing TLS on the FastAPI backend (documented limitation).
- Dependency CVEs already tracked by Dependabot.

## Hardening checklist for operators

If you fork or deploy this beyond a lab machine, at minimum:

- Set `NEXUS_API_KEY` to a non-empty value and require it on all mutating endpoints.
- Run behind a reverse proxy with TLS termination.
- Restrict filesystem access to the project directory.
- Do not expose Modbus TCP (`:5020`) or the FastAPI backend (`:8000`) to untrusted networks.
- Treat the Obsidian vault as sensitive — it may contain operational data.