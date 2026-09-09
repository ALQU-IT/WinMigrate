"""WinMigrate: owner-run Windows user-profile migration and backup utility.

Design principles that the code is expected to uphold (see docs/ARCHITECTURE.md):

* Owner-run and visible -- every stage reports what it touched.
* No network egress of captured data -- the only output is a local bundle.
* Consent-first restore -- anything the OS gates behind human authentication
  stays human-in-the-loop; the tool prepares and hands off.
* Secrets are encrypted, never logged, and never written to the plaintext
  sidecar manifest.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
