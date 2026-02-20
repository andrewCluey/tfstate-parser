"""
state_migrator - Migrate matched resources to a new Terraform state file.

Workflow
--------
1. Take a :class:`~tfstate_parser.models.ScanResult` (from :class:`~tfstate_parser.tf_scanner.TFScanner`).
2. Extract all *deployed* matched resources from the source state file.
3. Write them into a new ``terraform.tfstate`` file (the destination state).
4. Validate the destination state against the scanned ``.tf`` files:
   - Every deployed matched resource must appear in the destination state.
   - Instance counts must match the source.
   - No extra resources appear that weren't in the scan.
5. Generate two sets of commands:
   - ``terraform state mv`` commands that document the equivalent CLI migration.
   - ``terraform state rm`` commands to clean up the source state after migration.

No Terraform CLI is required to perform the migration — state files are
manipulated directly as JSON.  The generated CLI command lists are provided
so that operators can reproduce or audit the migration manually.

Example::

    from tfstate_parser import TFScanner, StateMigrator

    result = TFScanner("./infra", "source.tfstate").scan()

    migrator = StateMigrator(
        scan_result=result,
        source_state_path="source.tfstate",
        destination_state_path="new.tfstate",
    )

    migration = migrator.migrate()
    print(migration.summary())

    if migration.validation.passed:
        print("Migration valid — safe to remove resources from source state.")
        print(migration.cleanup_script())
    else:
        for err in migration.validation.errors:
            print("VALIDATION ERROR:", err)
"""

from __future__ import annotations

import copy
import json
import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .models import MatchedResource, ScanResult, TerraformResource
from .parser import TerraformStateParser
from .state_compat import StateCompatFixer, detect_local_terraform_version


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """Result of validating a destination state file against .tf source files."""

    passed: bool
    """True if all validation checks passed."""

    errors: List[str] = field(default_factory=list)
    """Descriptions of any validation failures."""

    warnings: List[str] = field(default_factory=list)
    """Non-fatal issues worth noting."""

    checked_resources: List[str] = field(default_factory=list)
    """Addresses of all resources that were checked."""

    def __repr__(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"ValidationResult({status}, "
            f"errors={len(self.errors)}, "
            f"warnings={len(self.warnings)}, "
            f"checked={len(self.checked_resources)})"
        )


@dataclass
class MigratedResource:
    """Record of a single resource that was migrated to the destination state."""

    address: str
    """Resource address, e.g. 'aws_instance.web'."""

    instance_count: int
    """Number of instances migrated."""

    state_mv_command: str
    """Equivalent ``terraform state mv`` CLI command."""

    state_rm_command: str
    """``terraform state rm`` command for source cleanup."""


@dataclass
class MigrationResult:
    """
    Complete result of a state migration operation.

    Attributes
    ----------
    migrated:
        Resources successfully written to the destination state.
    skipped:
        Matched resources that were skipped (e.g. not deployed in source).
    destination_state_path:
        Path where the new state file was written.
    validation:
        Result of validating the destination state against ``.tf`` files.
    source_state_path:
        Path of the original source state file.
    """

    migrated: List[MigratedResource] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    destination_state_path: str = ""
    source_state_path: str = ""
    validation: Optional[ValidationResult] = None
    compat_report: Optional[Any] = None  # CompatReport, imported lazily to avoid circular

    @property
    def migrated_addresses(self) -> List[str]:
        return [m.address for m in self.migrated]

    def state_mv_commands(self) -> List[str]:
        """
        Return a list of ``terraform state mv`` commands that document the
        equivalent CLI-based migration from source to destination state.

        These commands are for reference / auditing — the migration has already
        been performed by writing the destination state file directly.
        """
        return [m.state_mv_command for m in self.migrated]

    def state_rm_commands(self) -> List[str]:
        """
        Return a list of ``terraform state rm`` commands to remove migrated
        resources from the *source* state file once you are satisfied the
        destination state is correct.
        """
        return [m.state_rm_command for m in self.migrated]

    def cleanup_script(self, shell: str = "#!/usr/bin/env bash") -> str:
        """
        Return a shell script that removes all migrated resources from the
        source state file.

        Parameters
        ----------
        shell:
            Shebang line for the script (default: bash).
        """
        lines = [
            shell,
            "# Auto-generated cleanup script",
            f"# Remove migrated resources from: {self.source_state_path}",
            f"# Generated: {datetime.now(timezone.utc).isoformat()}",
            "#",
            "# Run this ONLY after confirming the destination state is valid.",
            "# Resources being removed:",
        ]
        for m in self.migrated:
            lines.append(f"#   {m.address}")
        lines.append("")
        lines.extend(self.state_rm_commands())
        return "\n".join(lines) + "\n"

    def mv_script(self, shell: str = "#!/usr/bin/env bash") -> str:
        """
        Return a shell script of ``terraform state mv`` commands that
        documents the equivalent CLI migration for audit purposes.
        """
        lines = [
            shell,
            "# Auto-generated state mv reference script",
            f"# Source state     : {self.source_state_path}",
            f"# Destination state: {self.destination_state_path}",
            f"# Generated        : {datetime.now(timezone.utc).isoformat()}",
            "#",
            "# NOTE: Migration has already been performed by writing the",
            "# destination state file directly. These commands are for",
            "# reference / manual replay only.",
            "",
        ]
        lines.extend(self.state_mv_commands())
        return "\n".join(lines) + "\n"

    def summary(self) -> str:
        lines = [
            "Migration Result",
            f"  Source state      : {self.source_state_path}",
            f"  Destination state : {self.destination_state_path}",
            f"  Migrated          : {len(self.migrated)} resources",
            f"  Skipped           : {len(self.skipped)} resources",
        ]

        if self.validation:
            status = "PASSED ✓" if self.validation.passed else "FAILED ✗"
            lines.append(f"  Validation        : {status}")
            for err in self.validation.errors:
                lines.append(f"    ERROR   : {err}")
            for warn in self.validation.warnings:
                lines.append(f"    WARNING : {warn}")

        if self.compat_report:
            compat_status = "applied" if self.compat_report.fix_count > 0 else "not required"
            lines.append(
                f"  Compat fixes      : {self.compat_report.fix_count} ({compat_status})"
            )
            for fix in self.compat_report.fixes:
                lines.append(f"    FIX     : {fix}")
            for warn in self.compat_report.warnings:
                lines.append(f"    WARNING : {warn}")

        if self.migrated:
            lines.append("")
            lines.append("Migrated resources:")
            for m in self.migrated:
                lines.append(f"  ✓  {m.address}  ({m.instance_count} instance(s))")

        if self.skipped:
            lines.append("")
            lines.append("Skipped (not deployed in source state):")
            for addr in self.skipped:
                lines.append(f"  –  {addr}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Migrator
# ---------------------------------------------------------------------------

class StateMigrator:
    """
    Migrate deployed resources from a source Terraform state file into a new
    destination state file, then validate and generate cleanup commands.

    Parameters
    ----------
    scan_result:
        A :class:`~tfstate_parser.models.ScanResult` produced by
        :class:`~tfstate_parser.tf_scanner.TFScanner`. Only ``deployed``
        resources (those matched between ``.tf`` files and the source state)
        are migrated.
    source_state_path:
        Path to the source ``terraform.tfstate`` file.
    destination_state_path:
        Path where the new state file will be written.  If the file already
        exists it will be overwritten.
    overwrite:
        If ``False`` (default) and the destination file already exists, raise
        ``FileExistsError``.  Set to ``True`` to allow overwriting.

    Example::

        migrator = StateMigrator(
            scan_result=result,
            source_state_path="terraform.tfstate",
            destination_state_path="migrated.tfstate",
        )
        migration = migrator.migrate()
        print(migration.summary())

        # Write the cleanup script to disk
        with open("cleanup.sh", "w") as f:
            f.write(migration.cleanup_script())

        # Write the mv reference script to disk
        with open("state_mv_reference.sh", "w") as f:
            f.write(migration.mv_script())
    """

    def __init__(
        self,
        scan_result: ScanResult,
        source_state_path: Union[str, Path],
        destination_state_path: Union[str, Path],
        *,
        overwrite: bool = False,
        target_terraform_version: Optional[str] = None,
        auto_fix_compat: bool = True,
    ) -> None:
        self._scan_result = scan_result
        self._source_path = Path(source_state_path)
        self._dest_path = Path(destination_state_path)
        self._overwrite = overwrite
        self._target_version = target_terraform_version
        self._auto_fix_compat = auto_fix_compat

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def migrate(self) -> MigrationResult:
        """
        Execute the full migration pipeline:

        1. Load the source state file.
        2. Extract state entries for all deployed matched resources.
        3. Write a new destination state file containing only those entries.
        4. Validate the destination state against the scan result.
        5. Build and return a :class:`MigrationResult`.
        """
        if not self._overwrite and self._dest_path.exists():
            raise FileExistsError(
                f"Destination state file already exists: {self._dest_path}. "
                "Pass overwrite=True to allow overwriting."
            )

        # --- 1. Load source state ---
        source_raw = self._load_json(self._source_path)
        source_parser = TerraformStateParser(source_raw, infer_dependencies=False)
        source_map: Dict[str, TerraformResource] = source_parser.get_resource_map()

        # --- 2. Identify resources to migrate ---
        to_migrate: List[MatchedResource] = self._scan_result.deployed
        migrated: List[MigratedResource] = []
        skipped: List[str] = []
        dest_resource_blocks: List[Dict[str, Any]] = []

        for match in to_migrate:
            addr = match.address
            state_res = source_map.get(addr)
            if state_res is None:
                skipped.append(addr)
                continue

            # Pull the raw block from the source state (preserves all fields)
            raw_block = self._find_raw_block(source_raw, addr)
            if raw_block is None:
                skipped.append(addr)
                continue

            dest_resource_blocks.append(copy.deepcopy(raw_block))

            migrated.append(MigratedResource(
                address=addr,
                instance_count=len(state_res.instances),
                state_mv_command=self._mv_command(addr),
                state_rm_command=self._rm_command(addr),
            ))

        # --- 3. Write destination state ---
        dest_state = self._build_dest_state(source_raw, dest_resource_blocks)
        self._write_json(self._dest_path, dest_state)

        # --- 3b. Apply compatibility fixes if requested ---
        compat_report = None
        if self._auto_fix_compat:
            compat_report = self._apply_compat(self._dest_path)

        # --- 4. Validate ---
        # Re-read dest state after compat fixes may have modified it
        final_dest_state = self._load_json(self._dest_path) if compat_report else dest_state
        validation = self._validate(final_dest_state, migrated, self._scan_result)

        return MigrationResult(
            migrated=migrated,
            skipped=skipped,
            source_state_path=str(self._source_path),
            destination_state_path=str(self._dest_path),
            validation=validation,
            compat_report=compat_report,
        )

    def dry_run(self) -> MigrationResult:
        """
        Perform all steps of the migration except writing the destination file.

        Returns a :class:`MigrationResult` where ``validation`` reflects what
        *would* happen.  No files are created or modified.
        """
        source_raw = self._load_json(self._source_path)
        source_parser = TerraformStateParser(source_raw, infer_dependencies=False)
        source_map = source_parser.get_resource_map()

        to_migrate = self._scan_result.deployed
        migrated: List[MigratedResource] = []
        skipped: List[str] = []
        dest_resource_blocks: List[Dict[str, Any]] = []

        for match in to_migrate:
            addr = match.address
            state_res = source_map.get(addr)
            if state_res is None:
                skipped.append(addr)
                continue
            raw_block = self._find_raw_block(source_raw, addr)
            if raw_block is None:
                skipped.append(addr)
                continue
            dest_resource_blocks.append(copy.deepcopy(raw_block))
            migrated.append(MigratedResource(
                address=addr,
                instance_count=len(state_res.instances),
                state_mv_command=self._mv_command(addr),
                state_rm_command=self._rm_command(addr),
            ))

        dest_state = self._build_dest_state(source_raw, dest_resource_blocks)
        validation = self._validate(dest_state, migrated, self._scan_result)

        # Simulate compat check (no file written, so we run check() not fix())
        compat_report = None
        if self._auto_fix_compat:
            try:
                import tempfile, os
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".tfstate", delete=False
                ) as tmp:
                    json.dump(dest_state, tmp, indent=2)
                    tmp_path = tmp.name
                fixer = StateCompatFixer(tmp_path, self._target_version, backup=False)
                compat_report = fixer.check()
                compat_report.output_path = str(self._dest_path) + " (dry run — not written)"
            except Exception:
                pass
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        return MigrationResult(
            migrated=migrated,
            skipped=skipped,
            source_state_path=str(self._source_path),
            destination_state_path=str(self._dest_path) + " (dry run — not written)",
            validation=validation,
            compat_report=compat_report,
        )

    # ------------------------------------------------------------------
    # State construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_dest_state(
        source_raw: Dict[str, Any],
        resource_blocks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Build a new state dict based on the source state metadata but
        containing only the supplied resource blocks.

        The serial is incremented by 1 so Terraform recognises this as a
        newer state version than the source.
        """
        dest = {
            "version": source_raw.get("version", 4),
            "terraform_version": source_raw.get("terraform_version", ""),
            "serial": source_raw.get("serial", 0) + 1,
            "lineage": source_raw.get("lineage", ""),
            "outputs": copy.deepcopy(source_raw.get("outputs", {})),
            "resources": resource_blocks,
            "check_results": source_raw.get("check_results", None),
        }
        # Remove null check_results key to keep the file clean
        if dest["check_results"] is None:
            del dest["check_results"]
        return dest

    @staticmethod
    def _find_raw_block(
        source_raw: Dict[str, Any], address: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find the raw resource block in the source state JSON that corresponds
        to the given address.  Returns ``None`` if not found.
        """
        for block in source_raw.get("resources", []):
            block_addr = StateMigrator._block_address(block)
            if block_addr == address:
                return block
        return None

    @staticmethod
    def _block_address(block: Dict[str, Any]) -> str:
        """Reconstruct the canonical address from a raw state resource block."""
        module = block.get("module", "")
        mode = block.get("mode", "managed")
        rtype = block.get("type", "")
        name = block.get("name", "")
        parts = []
        if module:
            parts.append(module)
        if mode == "data":
            parts.append(f"data.{rtype}.{name}")
        else:
            parts.append(f"{rtype}.{name}")
        return ".".join(parts)

    # ------------------------------------------------------------------
    # CLI command builders
    # ------------------------------------------------------------------

    def _mv_command(self, address: str) -> str:
        """
        Build a ``terraform state mv`` command that documents the equivalent
        CLI migration from the source to the destination state.

        terraform state mv -state=<src> -state-out=<dest> <addr> <addr>
        """
        src = shlex.quote(str(self._source_path))
        dest = shlex.quote(str(self._dest_path))
        addr = shlex.quote(address)
        return f"terraform state mv -state={src} -state-out={dest} {addr} {addr}"

    def _rm_command(self, address: str) -> str:
        """
        Build a ``terraform state rm`` command to remove a resource from the
        source state file.

        terraform state rm -state=<src> <addr>
        """
        src = shlex.quote(str(self._source_path))
        addr = shlex.quote(address)
        return f"terraform state rm -state={src} {addr}"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(
        dest_state: Dict[str, Any],
        migrated: List[MigratedResource],
        scan_result: ScanResult,
    ) -> ValidationResult:
        """
        Validate the destination state against the scan result.

        Checks:
        1. Every migrated resource address appears in the destination state.
        2. Instance count in destination matches the migrated record.
        3. No resources appear in the destination state that were not in the
           migration plan (guards against accidental pollution).
        4. Warn about deployed resources in the scan that were NOT migrated
           (i.e. resources present in source state but skipped somehow).
        """
        errors: List[str] = []
        warnings: List[str] = []
        checked: List[str] = []

        # Build a lookup of destination state resources
        dest_parser = TerraformStateParser(dest_state, infer_dependencies=False)
        dest_map: Dict[str, TerraformResource] = dest_parser.get_resource_map()

        migrated_addresses = {m.address for m in migrated}

        # Check 1 & 2: every migrated resource is present with correct instance count
        for m in migrated:
            checked.append(m.address)
            if m.address not in dest_map:
                errors.append(
                    f"Resource '{m.address}' was migrated but is MISSING from "
                    f"the destination state."
                )
                continue
            dest_res = dest_map[m.address]
            dest_count = len(dest_res.instances)
            if dest_count != m.instance_count:
                errors.append(
                    f"Resource '{m.address}': expected {m.instance_count} instance(s) "
                    f"in destination state, found {dest_count}."
                )

        # Check 3: no unexpected resources in destination
        for addr in dest_map:
            if addr not in migrated_addresses:
                errors.append(
                    f"Unexpected resource '{addr}' found in destination state — "
                    f"it was not part of the migration plan."
                )

        # Check 4: warn about deployed scan resources that weren't migrated
        all_deployed_addrs = {m.address for m in scan_result.deployed}
        unmigrated_deployed = all_deployed_addrs - migrated_addresses
        for addr in sorted(unmigrated_deployed):
            warnings.append(
                f"Resource '{addr}' is deployed in source state but was NOT migrated. "
                f"It will remain in the source state only."
            )

        # Check 5: warn about .tf resources that are not deployed at all
        for m in scan_result.undeployed:
            warnings.append(
                f"Resource '{m.address}' is declared in .tf files but has no "
                f"state entry — it cannot be migrated."
            )

        passed = len(errors) == 0
        return ValidationResult(
            passed=passed,
            errors=errors,
            warnings=warnings,
            checked_resources=checked,
        )

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _apply_compat(self, path: Path):
        """Apply StateCompatFixer to the written destination state file."""
        try:
            fixer = StateCompatFixer(path, self._target_version, backup=False)
            return fixer.fix(output_path=path)
        except ValueError:
            # No Terraform binary found and no version specified — skip silently
            return None

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
