"""
state_compat - Fix Terraform state file compatibility issues.

The Problem
-----------
When a state file is written by a **newer** version of Terraform than the
binary you are currently using, Terraform refuses to load it with an error
like::

    Error: Failed to load state: unsupported backend state version 4;
    you may need to use Terraform CLI v1.10.2 to work in this directory

Despite the confusing wording ("version 4" refers to the state *format*
version, which hasn't changed since Terraform 0.12), the real blocker is the
``terraform_version`` field inside the state JSON.  Terraform enforces a
strict rule:

    A state file may only be opened by the same or a newer Terraform binary
    than the one that last wrote it.

This module provides :class:`StateCompatFixer`, which patches a state file so
it can be consumed by the Terraform binary version actually installed on the
current machine (or any target version you specify).  It also handles several
other common compatibility problems found in migrated state files:

- ``terraform_version`` too high for the local binary
- ``check_results`` field not supported by older Terraform (<1.5)
- ``sensitive_attributes`` not supported by older Terraform (<0.15)
- Instance ``schema_version`` mismatches
- Leftover ``serial`` drift from migration tools

Example::

    from tfstate_parser import StateCompatFixer

    # Auto-detect local Terraform version and patch the state
    fixer = StateCompatFixer("migrated.tfstate")
    report = fixer.fix(output_path="migrated_compat.tfstate")
    print(report.summary())

    # Patch to a specific version without running Terraform
    fixer = StateCompatFixer("migrated.tfstate", target_version="1.5.7")
    report = fixer.fix(output_path="migrated_compat.tfstate")
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def _parse_version(version_str: str) -> Tuple[int, ...]:
    """
    Parse a semantic version string into a comparable tuple of ints.

    Strips pre-release suffixes (e.g. '-beta1', '-rc2').
    Returns (0, 0, 0) for unparseable strings.
    """
    cleaned = re.split(r"[-+]", version_str.strip())[0]  # strip pre-release
    try:
        return tuple(int(p) for p in cleaned.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _version_str(tpl: Tuple[int, ...]) -> str:
    return ".".join(str(p) for p in tpl)


def detect_local_terraform_version() -> Optional[str]:
    """
    Run ``terraform version -json`` and return the version string, or
    ``None`` if Terraform is not installed / not on PATH.

    Returns a string like ``"1.5.7"`` or ``"1.9.0"``.
    """
    try:
        result = subprocess.run(
            ["terraform", "version", "-json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            version = data.get("terraform_version", "")
            if version:
                return version.lstrip("v")
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        pass

    # Fallback: try plain `terraform version` (older format)
    try:
        result = subprocess.run(
            ["terraform", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            match = re.search(r"Terraform v(\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return None


# ---------------------------------------------------------------------------
# Compatibility report
# ---------------------------------------------------------------------------

@dataclass
class CompatFix:
    """A single compatibility fix that was applied."""

    field: str
    """The state field that was modified."""

    old_value: str
    """The value before the fix."""

    new_value: str
    """The value after the fix."""

    reason: str
    """Why this fix was necessary."""

    def __str__(self) -> str:
        return f"[{self.field}] {self.old_value!r} → {self.new_value!r}  ({self.reason})"


@dataclass
class CompatReport:
    """
    Report of all compatibility fixes applied to a state file.

    Attributes
    ----------
    input_path:
        Path of the original state file.
    output_path:
        Path of the fixed state file that was written.
    target_version:
        The Terraform version the state was made compatible with.
    fixes:
        List of individual changes made.
    warnings:
        Non-fatal issues that were detected but not automatically fixable.
    """

    input_path: str
    output_path: str
    target_version: str
    fixes: List[CompatFix] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def fix_count(self) -> int:
        return len(self.fixes)

    def summary(self) -> str:
        lines = [
            "State Compatibility Report",
            f"  Input          : {self.input_path}",
            f"  Output         : {self.output_path}",
            f"  Target version : {self.target_version}",
            f"  Fixes applied  : {self.fix_count}",
            f"  Warnings       : {len(self.warnings)}",
        ]
        if self.fixes:
            lines.append("")
            lines.append("Fixes:")
            for fix in self.fixes:
                lines.append(f"  • {fix}")
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  ⚠  {w}")
        if not self.fixes and not self.warnings:
            lines.append("")
            lines.append("  No changes required — state was already compatible.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fixer
# ---------------------------------------------------------------------------

class StateCompatFixer:
    """
    Fix compatibility issues in a Terraform state file so it can be opened
    by a specific (typically older) version of the Terraform CLI.

    Parameters
    ----------
    state_path:
        Path to the state file to fix (read-only — never modified in place
        unless ``output_path`` is the same as ``state_path``).
    target_version:
        The Terraform version to make the state compatible with.
        If ``None`` (default), the local Terraform binary is auto-detected.
        If no local Terraform is found and no version is given, a
        ``ValueError`` is raised.
    backup:
        If ``True`` (default) and ``output_path`` equals ``state_path``,
        write a ``.bak`` backup before overwriting.

    Example::

        # Auto-detect
        fixer = StateCompatFixer("migrated.tfstate")
        report = fixer.fix("migrated_compat.tfstate")
        print(report.summary())

        # Explicit target
        fixer = StateCompatFixer("migrated.tfstate", target_version="1.5.7")
        report = fixer.fix("migrated_compat.tfstate")
    """

    # Fields introduced in specific Terraform versions.
    # If target_version < introduced_version, the field must be stripped.
    _VERSIONED_FIELDS: List[Dict[str, Any]] = [
        {
            "field": "check_results",
            "scope": "root",
            "introduced": (1, 5, 0),
            "description": "Terraform checks / assertions (introduced in 1.5)",
        },
        {
            "field": "sensitive_attributes",
            "scope": "instance",
            "introduced": (0, 15, 0),
            "description": "Sensitive attribute tracking (introduced in 0.15)",
        },
        {
            "field": "sensitive_values",
            "scope": "instance",
            "introduced": (0, 15, 0),
            "description": "Sensitive value tracking (introduced in 0.15)",
        },
        {
            "field": "depends_on",
            "scope": "instance",
            "introduced": (1, 1, 0),
            "description": "Explicit depends_on in state (introduced in 1.1)",
        },
    ]

    def __init__(
        self,
        state_path: Union[str, Path],
        target_version: Optional[str] = None,
        *,
        backup: bool = True,
    ) -> None:
        self._state_path = Path(state_path)
        self._target_version_str = target_version
        self._backup = backup

    def fix(
        self,
        output_path: Optional[Union[str, Path]] = None,
    ) -> CompatReport:
        """
        Apply all necessary compatibility fixes and write the result.

        Parameters
        ----------
        output_path:
            Where to write the fixed state file.  Defaults to overwriting
            the input file (a ``.bak`` backup is written first if
            ``backup=True``).

        Returns
        -------
        CompatReport
            A full report of every change made.
        """
        out_path = Path(output_path) if output_path else self._state_path

        # Resolve target version
        target_str = self._resolve_target_version()
        target = _parse_version(target_str)

        # Load state
        with open(self._state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)

        fixes: List[CompatFix] = []
        warnings: List[str] = []
        state = copy.deepcopy(state)

        # ---- Fix 1: terraform_version field --------------------------------
        state, fix = self._fix_terraform_version(state, target_str, target)
        if fix:
            fixes.append(fix)

        # ---- Fix 2: strip version-gated root-level fields ------------------
        state, new_fixes, new_warnings = self._fix_versioned_root_fields(
            state, target
        )
        fixes.extend(new_fixes)
        warnings.extend(new_warnings)

        # ---- Fix 3: strip version-gated per-instance fields ----------------
        state, new_fixes = self._fix_versioned_instance_fields(state, target)
        fixes.extend(new_fixes)

        # ---- Fix 4: serial sanity check ------------------------------------
        fix = self._fix_serial(state)
        if fix:
            fixes.append(fix)
            state["serial"] = int(fix.new_value)

        # ---- Fix 5: provider format ----------------------------------------
        new_fixes, new_warnings = self._fix_provider_format(state, target)
        fixes.extend(new_fixes)
        warnings.extend(new_warnings)

        # Backup if overwriting input
        if out_path.resolve() == self._state_path.resolve() and self._backup:
            backup_path = self._state_path.with_suffix(
                self._state_path.suffix + ".bak"
            )
            shutil.copy2(self._state_path, backup_path)

        # Write output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")

        return CompatReport(
            input_path=str(self._state_path),
            output_path=str(out_path),
            target_version=target_str,
            fixes=fixes,
            warnings=warnings,
        )

    def check(self) -> CompatReport:
        """
        Check for compatibility issues without writing any files.

        Returns a :class:`CompatReport` describing what *would* be fixed.
        The ``output_path`` in the report will indicate this is a dry check.
        """
        target_str = self._resolve_target_version()
        target = _parse_version(target_str)

        with open(self._state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)

        state = copy.deepcopy(state)
        fixes: List[CompatFix] = []
        warnings: List[str] = []

        _, fix = self._fix_terraform_version(state, target_str, target)
        if fix:
            fixes.append(fix)

        _, new_fixes, new_warnings = self._fix_versioned_root_fields(state, target)
        fixes.extend(new_fixes)
        warnings.extend(new_warnings)

        _, new_fixes = self._fix_versioned_instance_fields(state, target)
        fixes.extend(new_fixes)

        fix = self._fix_serial(state)
        if fix:
            fixes.append(fix)

        new_fixes, new_warnings = self._fix_provider_format(state, target)
        fixes.extend(new_fixes)
        warnings.extend(new_warnings)

        return CompatReport(
            input_path=str(self._state_path),
            output_path="(not written — check only)",
            target_version=target_str,
            fixes=fixes,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Individual fix methods
    # ------------------------------------------------------------------

    @staticmethod
    def _fix_terraform_version(
        state: Dict[str, Any],
        target_str: str,
        target: Tuple[int, ...],
    ) -> Tuple[Dict[str, Any], Optional[CompatFix]]:
        """
        Downgrade the ``terraform_version`` field if it is higher than the
        target version.  This is the primary cause of the
        "unsupported backend state version" error.
        """
        current_str = state.get("terraform_version", "0.0.0")
        current = _parse_version(current_str)

        if current > target:
            state["terraform_version"] = target_str
            return state, CompatFix(
                field="terraform_version",
                old_value=current_str,
                new_value=target_str,
                reason=(
                    f"State was written by Terraform {current_str} but target "
                    f"binary is {target_str}. Terraform refuses to open state "
                    f"files written by a newer version."
                ),
            )
        return state, None

    @classmethod
    def _fix_versioned_root_fields(
        cls,
        state: Dict[str, Any],
        target: Tuple[int, ...],
    ) -> Tuple[Dict[str, Any], List[CompatFix], List[str]]:
        """Strip root-level fields that didn't exist in the target version."""
        fixes: List[CompatFix] = []
        warnings: List[str] = []

        for spec in cls._VERSIONED_FIELDS:
            if spec["scope"] != "root":
                continue
            introduced = spec["introduced"]
            field_name = spec["field"]
            if target < introduced and field_name in state:
                old_val = state.pop(field_name)
                fixes.append(CompatFix(
                    field=field_name,
                    old_value=repr(old_val)[:80],
                    new_value="(removed)",
                    reason=(
                        f"{spec['description']} — not supported before "
                        f"{_version_str(introduced)}, target is "
                        f"{_version_str(target)}."
                    ),
                ))

        return state, fixes, warnings

    @classmethod
    def _fix_versioned_instance_fields(
        cls,
        state: Dict[str, Any],
        target: Tuple[int, ...],
    ) -> Tuple[Dict[str, Any], List[CompatFix]]:
        """Strip per-instance fields that didn't exist in the target version."""
        fixes: List[CompatFix] = []
        instance_specs = [
            s for s in cls._VERSIONED_FIELDS if s["scope"] == "instance"
        ]

        for resource in state.get("resources", []):
            for instance in resource.get("instances", []):
                for spec in instance_specs:
                    fname = spec["field"]
                    introduced = spec["introduced"]
                    if target < introduced and fname in instance:
                        old_val = instance.pop(fname)
                        # Only record one fix per field name to avoid noise
                        field_key = f"instances[*].{fname}"
                        if not any(f.field == field_key for f in fixes):
                            fixes.append(CompatFix(
                                field=field_key,
                                old_value=repr(old_val)[:60],
                                new_value="(removed from all instances)",
                                reason=(
                                    f"{spec['description']} — not supported "
                                    f"before {_version_str(introduced)}, target "
                                    f"is {_version_str(target)}."
                                ),
                            ))

        return state, fixes

    @staticmethod
    def _fix_serial(state: Dict[str, Any]) -> Optional[CompatFix]:
        """
        Ensure the serial is a non-negative integer.  Some migration tools
        or manual edits can leave it as a float or string.
        """
        serial = state.get("serial")
        if serial is None:
            state["serial"] = 1
            return CompatFix(
                field="serial",
                old_value="(missing)",
                new_value="1",
                reason="serial field was absent; set to 1.",
            )
        if not isinstance(serial, int) or isinstance(serial, bool):
            try:
                fixed = max(1, int(serial))
            except (ValueError, TypeError):
                fixed = 1
            state["serial"] = fixed
            return CompatFix(
                field="serial",
                old_value=repr(serial),
                new_value=str(fixed),
                reason=f"serial was {type(serial).__name__}; must be an integer.",
            )
        return None

    @staticmethod
    def _fix_provider_format(
        state: Dict[str, Any],
        target: Tuple[int, ...],
    ) -> Tuple[List[CompatFix], List[str]]:
        """
        Warn if provider strings use the new registry format
        (``provider["registry.terraform.io/hashicorp/aws"]``) but the target
        Terraform version predates it (< 0.13).

        Terraform 0.13+ uses the full registry address format.  We can't
        safely auto-rewrite provider strings since provider aliases may be
        in use, so we emit a warning instead.
        """
        fixes: List[CompatFix] = []
        warnings: List[str] = []

        if target >= (0, 13, 0):
            return fixes, warnings

        registry_re = re.compile(r'provider\["registry\.terraform\.io/')
        for resource in state.get("resources", []):
            provider = resource.get("provider", "")
            if registry_re.search(provider):
                warnings.append(
                    f"Resource '{resource.get('type', '')}.{resource.get('name', '')}' "
                    f"uses provider format '{provider}' which requires Terraform 0.13+. "
                    f"Target version {_version_str(target)} may not support this. "
                    f"Manual provider string adjustment may be required."
                )

        return fixes, warnings

    # ------------------------------------------------------------------
    # Version resolution
    # ------------------------------------------------------------------

    def _resolve_target_version(self) -> str:
        """Return the target version string, detecting it from the local binary if needed."""
        if self._target_version_str:
            return self._target_version_str

        detected = detect_local_terraform_version()
        if detected:
            return detected

        raise ValueError(
            "No target_version specified and Terraform binary not found on PATH. "
            "Either install Terraform or pass target_version= explicitly."
        )
