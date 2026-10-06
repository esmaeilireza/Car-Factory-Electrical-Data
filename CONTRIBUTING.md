# Contributing to NEXUS SCADA

Thanks for your interest. NEXUS SCADA is a **research prototype**. Contributions
are welcome that:

- fix bugs,
- improve the verifier's coverage or signal-to-noise,
- tighten the safety contract,
- improve documentation clarity,
- add or improve unit tests.

Contributions that **change the safety contract** (e.g., allow the LLM to
actuate anything beyond load setpoints, or weaken the deterministic rule
engine) are out of scope for this repository and will be declined.

## Development setup

1. Clone the repository and create a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # or: .venv\Scripts\activate on Windows
   pip install -r requirements.txt