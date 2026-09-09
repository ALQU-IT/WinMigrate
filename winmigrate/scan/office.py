"""Microsoft Office detection: what is installed, and how it is licensed.

Two questions, two sources:

* **What to reinstall** comes from the Click-to-Run configuration in the
  registry -- product ids, bitness, language and update channel. That is enough
  to generate an Office Deployment Tool configuration that reproduces the same
  installation on the new machine.
* **How it is licensed** comes from ``ospp.vbs /dstatus``, which reports the
  licence family and status.

**No product key is ever extracted.** ``ospp.vbs`` prints only the last five
characters of an installed key, and that is all this module records -- enough
for the user to recognise which key they need to find, useless to anyone else.
Recovering a full key is out of scope in the same way browser password
decryption is: reinstalling Office is a supported operation, extracting its
licence material is not.

Reactivation is always the user's step. A Microsoft 365 installation reactivates
when they sign in; a retail one needs the key they own. The tool prepares the
install and says which applies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..platform_win import HKLM, Environment
from ..util import process

log = logging.getLogger(__name__)

CLICK_TO_RUN_CONFIG = r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration"

#: Update-channel GUIDs as they appear in the CDN base URL / AudienceId.
CHANNEL_BY_GUID = {
    "492350f6-3a01-4f97-b9c0-c7c6ddf67d60": "Current",
    "55336b82-a18d-4dd6-b5f6-9e5095c314a6": "MonthlyEnterprise",
    "7ffbc6bf-bc32-4f92-8982-f9dd17fd3114": "SemiAnnual",
    "b8f9b850-328d-4355-9145-c59439a0c4cf": "CurrentPreview",
    "f2e724c1-748f-4b47-8fb8-8e0d210e9208": "PerpetualVL2019",
    "5030841d-c919-4594-8d2d-84ae4f96e58e": "SemiAnnualPreview",
    "2e148de9-61c8-4051-b103-4af54baffbb4": "PerpetualVL2021",
}

#: Human-readable names for the product ids that appear in ProductReleaseIds.
PRODUCT_TITLES = {
    "O365ProPlusRetail": "Microsoft 365 Apps for enterprise",
    "O365BusinessRetail": "Microsoft 365 Apps for business",
    "O365HomePremRetail": "Microsoft 365 (Home / Personal)",
    "ProPlus2019Volume": "Office Professional Plus 2019 (volume)",
    "ProPlus2021Volume": "Office Professional Plus 2021 (volume)",
    "ProPlus2024Volume": "Office Professional Plus 2024 (volume)",
    "HomeStudent2019Retail": "Office Home & Student 2019",
    "HomeStudent2021Retail": "Office Home & Student 2021",
    "HomeBusiness2019Retail": "Office Home & Business 2019",
    "HomeBusiness2021Retail": "Office Home & Business 2021",
    "Professional2019Retail": "Office Professional 2019",
    "Professional2021Retail": "Office Professional 2021",
    "VisioProRetail": "Visio Plan 2",
    "ProjectProRetail": "Project Online Desktop Client",
}

OSPP_RELATIVE_PATHS = (
    r"Microsoft Office\Office16\ospp.vbs",
    r"Microsoft Office\root\Office16\ospp.vbs",
    r"Microsoft Office\Office15\ospp.vbs",
)


@dataclass(slots=True)
class OfficeLicence:
    """What ospp.vbs reported about one licence."""

    name: str = ""
    description: str = ""
    status: str = ""
    key_last_five: str = ""      # printed by ospp itself; never a full key

    @property
    def activation_type(self) -> str:
        """``subscription``, ``retail``, ``volume`` or ``unknown``."""
        text = f"{self.name} {self.description}".upper()
        if "SUBSCRIPTION" in text:
            return "subscription"
        if "KMSCLIENT" in text or "VOLUME" in text:
            return "volume"
        if "RETAIL" in text or "OEM" in text or "MAK" in text:
            return "retail"
        return "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "key_last_five": self.key_last_five,
            "activation_type": self.activation_type,
        }


@dataclass(slots=True)
class OfficeInstallation:
    """A Click-to-Run Office installation, described well enough to reproduce."""

    product_ids: list[str] = field(default_factory=list)
    platform: str = ""                 # x86 | x64
    client_culture: str = ""           # e.g. en-us
    channel: str = ""
    version: str = ""
    install_path: str = ""
    licences: list[OfficeLicence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.product_ids)

    @property
    def titles(self) -> list[str]:
        return [PRODUCT_TITLES.get(pid, pid) for pid in self.product_ids]

    @property
    def activation_type(self) -> str:
        for licence in self.licences:
            kind = licence.activation_type
            if kind != "unknown":
                return kind
        return "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "product_ids": list(self.product_ids),
            "titles": self.titles,
            "platform": self.platform,
            "client_culture": self.client_culture,
            "channel": self.channel,
            "version": self.version,
            "install_path": self.install_path,
            "activation_type": self.activation_type,
            "licences": [licence.to_json() for licence in self.licences],
            "notes": list(self.notes),
        }


def detect(env: Environment) -> OfficeInstallation:
    """Read the Click-to-Run configuration and, on Windows, the licence status."""
    values = env.read_registry_key(HKLM, CLICK_TO_RUN_CONFIG) or {}
    installation = read_configuration(values)
    if installation.present and env.is_windows:
        licences, error = read_licences(env)
        installation.licences = licences
        if error:
            installation.notes.append(f"licence status unavailable: {error}")
    return installation


def read_configuration(values: dict) -> OfficeInstallation:
    """Turn the ClickToRun\\Configuration values into an installation record."""
    raw_products = str(values.get("ProductReleaseIds") or "").strip()
    product_ids = [part.strip() for part in raw_products.split(",") if part.strip()]
    return OfficeInstallation(
        product_ids=product_ids,
        platform=str(values.get("Platform") or "").strip(),
        client_culture=str(values.get("ClientCulture") or "").strip(),
        channel=resolve_channel(values),
        version=str(values.get("VersionToReport") or "").strip(),
        install_path=str(values.get("InstallationPath") or "").strip(),
    )


def resolve_channel(values: dict) -> str:
    """Work out the update channel from whichever value this build records.

    Office has moved this between ``UpdateChannel`` (a CDN URL ending in a GUID),
    ``AudienceId`` and ``AudienceData`` across versions, so all are consulted
    before giving up and reporting the raw value.
    """
    for key in ("UpdateChannel", "CDNBaseUrl", "AudienceId", "AudienceData", "UpdateChannelChanged"):
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue
        match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", raw)
        if match:
            guid = match.group(0).lower()
            if guid in CHANNEL_BY_GUID:
                return CHANNEL_BY_GUID[guid]
        if raw in CHANNEL_BY_GUID.values():
            return raw
    return str(values.get("UpdateChannel") or "").strip()


def find_ospp(env: Environment) -> Path | None:
    """Locate ospp.vbs under either Program Files tree."""
    roots = [
        env.env_var("ProgramFiles") or r"C:\Program Files",
        env.env_var("ProgramFiles(x86)") or r"C:\Program Files (x86)",
    ]
    for root in roots:
        for relative in OSPP_RELATIVE_PATHS:
            candidate = Path(root) / relative
            if candidate.is_file():
                return candidate
    return None


def read_licences(env: Environment, runner=process.run) -> tuple[list[OfficeLicence], str | None]:
    """Run ``ospp.vbs /dstatus`` and parse what it reports."""
    script = find_ospp(env)
    if script is None:
        return [], "ospp.vbs not found; Office may be a Store or perpetual install"
    result = runner(["cscript.exe", "//Nologo", str(script), "/dstatus"], timeout=180)
    if not result.ok:
        return [], result.summary()
    return parse_ospp_dstatus(result.stdout), None


LICENCE_FIELD_PATTERN = re.compile(
    r"^\s*(LICENSE NAME|LICENSE DESCRIPTION|LICENSE STATUS|"
    r"Last 5 characters of installed product key)\s*:\s*(.*)$",
    re.IGNORECASE,
)


def parse_ospp_dstatus(text: str) -> list[OfficeLicence]:
    """Parse ospp.vbs /dstatus output into licence records.

    Only the four fields below are read. ospp prints the last five characters of
    the key itself; nothing here reconstructs, decrypts or looks up a full key.
    """
    licences: list[OfficeLicence] = []
    current: OfficeLicence | None = None
    for line in (text or "").splitlines():
        match = LICENCE_FIELD_PATTERN.match(line)
        if not match:
            continue
        field_name, value = match.group(1).upper(), match.group(2).strip()
        if field_name == "LICENSE NAME":
            current = OfficeLicence(name=value)
            licences.append(current)
            continue
        if current is None:
            current = OfficeLicence()
            licences.append(current)
        if field_name == "LICENSE DESCRIPTION":
            current.description = value
        elif field_name == "LICENSE STATUS":
            current.status = value
        else:
            current.key_last_five = value
    return licences


REACTIVATION_STEPS = {
    "subscription": [
        "Install Office on the new machine (the tool writes a matching configuration).",
        "Open any Office app and sign in with the Microsoft account or work account "
        "that holds the subscription.",
        "Activation completes on sign-in; there is no key to enter.",
    ],
    "retail": [
        "Install Office on the new machine (the tool writes a matching configuration).",
        "Have the product key you bought to hand -- WinMigrate does not copy it, "
        "and cannot recover it from this machine.",
        "If the key is linked to a Microsoft account, sign in at account.microsoft.com "
        "and find it under Services & subscriptions.",
        "Enter the key when Office asks for it.",
    ],
    "volume": [
        "Install Office on the new machine (the tool writes a matching configuration).",
        "Volume-licensed Office activates against your organisation's KMS or via ADBA "
        "once the machine is on the corporate network.",
        "If it does not activate, your IT administrator handles it -- there is no key "
        "for you to enter.",
    ],
    "unknown": [
        "Install Office on the new machine (the tool writes a matching configuration).",
        "Open an Office app and follow whatever it asks for: a sign-in for a "
        "subscription, or the key you own for a retail copy.",
    ],
}
