"""Core Terraform state file parser."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, IO, List, Optional, Union

from .models import DependencyGraph, TerraformResource


# Matches terraform references like aws_instance.web, module.vpc.aws_subnet.public, etc.
_RESOURCE_REF_PATTERN = re.compile(
    r'\b(?:module\.[a-zA-Z0-9_\-]+\.)?(?:data\.)?[a-zA-Z][a-zA-Z0-9_]*\.[a-zA-Z0-9_\-]+'
)


class TerraformStateParser:
    """
    Parse a Terraform state file (v4 format) and extract resources with dependencies.

    Supports:
    - terraform.tfstate files produced by Terraform 0.12+
    - Root-module and child-module resources
    - Explicit dependency lists stored in state
    - Inferred dependencies discovered by scanning attribute values

    Example::

        parser = TerraformStateParser("terraform.tfstate")

        # All resources as a flat list
        resources = parser.get_resources()

        # Resource keyed by address
        resource_map = parser.get_resource_map()

        # Full dependency graph object
        graph = parser.get_dependency_graph()

        # Topological ordering (leaves first)
        order = graph.topological_sort()
    """

    SUPPORTED_VERSION = 4

    def __init__(
        self,
        source: Union[str, Path, IO[str], dict],
        *,
        infer_dependencies: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        source:
            Path to a .tfstate file, an open file-like object, or a pre-parsed dict.
        infer_dependencies:
            When True (default), scan attribute values for resource address
            patterns and add them to ``inferred_dependencies``.
        """
        self._infer = infer_dependencies
        self._raw: Dict[str, Any] = self._load(source)
        self._version: int = self._raw.get("version", 0)
        self._resources: Optional[List[TerraformResource]] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load(source: Union[str, Path, IO[str], dict]) -> Dict[str, Any]:
        if isinstance(source, dict):
            return source
        if isinstance(source, (str, Path)):
            with open(source, "r", encoding="utf-8") as fh:
                return json.load(fh)
        # file-like object
        return json.load(source)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def version(self) -> int:
        """State file format version."""
        return self._version

    @property
    def terraform_version(self) -> str:
        """Terraform version recorded in the state file."""
        return self._raw.get("terraform_version", "unknown")

    @property
    def serial(self) -> int:
        """State serial number."""
        return self._raw.get("serial", 0)

    @property
    def lineage(self) -> str:
        """State lineage UUID."""
        return self._raw.get("lineage", "")

    def get_resources(self) -> List[TerraformResource]:
        """Return a flat list of all TerraformResource objects parsed from the state."""
        if self._resources is None:
            self._resources = self._parse_all_resources()
        return list(self._resources)

    def get_resource_map(self) -> Dict[str, TerraformResource]:
        """Return a dict mapping resource address -> TerraformResource."""
        return {r.address: r for r in self.get_resources()}

    def get_dependency_graph(self) -> DependencyGraph:
        """Build and return a DependencyGraph for all resources."""
        return DependencyGraph(resources=self.get_resource_map())

    def get_dependencies(self) -> Dict[str, List[str]]:
        """
        Return a plain dict mapping each resource address to its dependency list.

        This is the simplest way to inspect dependencies without using the
        full object model.
        """
        return {r.address: r.all_dependencies for r in self.get_resources()}

    def summary(self) -> str:
        """Return a human-readable summary of the state file."""
        resources = self.get_resources()
        lines = [
            f"Terraform State Summary",
            f"  Format version   : {self.version}",
            f"  Terraform version: {self.terraform_version}",
            f"  Serial           : {self.serial}",
            f"  Lineage          : {self.lineage}",
            f"  Total resources  : {len(resources)}",
            "",
            "Resources:",
        ]
        for res in sorted(resources, key=lambda r: r.address):
            deps = res.all_dependencies
            dep_str = ", ".join(deps) if deps else "(none)"
            lines.append(f"  [{res.mode}] {res.address}")
            lines.append(f"      provider    : {res.provider}")
            lines.append(f"      instances   : {len(res.instances)}")
            lines.append(f"      dependencies: {dep_str}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Parsing internals
    # ------------------------------------------------------------------

    def _parse_all_resources(self) -> List[TerraformResource]:
        raw_resources = self._raw.get("resources", [])
        resources: List[TerraformResource] = []

        for raw in raw_resources:
            resource = self._parse_resource(raw)
            resources.append(resource)

        if self._infer:
            address_set = {r.address for r in resources}
            for resource in resources:
                resource.inferred_dependencies = self._infer_deps(resource, address_set)

        return resources

    def _parse_resource(self, raw: Dict[str, Any]) -> TerraformResource:
        module = raw.get("module", "")
        rtype = raw.get("type", "")
        name = raw.get("name", "")
        mode = raw.get("mode", "managed")

        # Build canonical address
        address = self._build_address(module, mode, rtype, name)

        instances = raw.get("instances", [])

        # Collect explicit dependencies – Terraform stores them in two places:
        # 1. Top-level "dependencies" array (Terraform 0.13+)
        # 2. Per-instance "dependencies" array
        explicit_deps: List[str] = list(raw.get("dependencies", []))
        for inst in instances:
            for dep in inst.get("dependencies", []):
                if dep not in explicit_deps:
                    explicit_deps.append(dep)

        return TerraformResource(
            address=address,
            module=module,
            resource_type=rtype,
            name=name,
            provider=raw.get("provider", ""),
            mode=mode,
            instances=instances,
            dependencies=explicit_deps,
        )

    @staticmethod
    def _build_address(module: str, mode: str, rtype: str, name: str) -> str:
        """Construct a resource address compatible with Terraform conventions."""
        parts = []
        if module:
            parts.append(module)
        if mode == "data":
            parts.append(f"data.{rtype}.{name}")
        else:
            parts.append(f"{rtype}.{name}")
        return ".".join(parts)

    def _infer_deps(
        self, resource: TerraformResource, all_addresses: set
    ) -> set:
        """
        Scan all attribute values in a resource's instances for strings that
        look like references to other known resource addresses.
        """
        inferred: set = set()
        text = self._instances_to_text(resource.instances)

        for match in _RESOURCE_REF_PATTERN.finditer(text):
            candidate = match.group(0)
            # Check exact match or prefix match (handles attribute traversal)
            for addr in all_addresses:
                if addr == resource.address:
                    continue
                if candidate == addr or candidate.startswith(addr + "."):
                    inferred.add(addr)

        return inferred

    @staticmethod
    def _instances_to_text(instances: List[Dict[str, Any]]) -> str:
        """Flatten instance attribute values into a single searchable text blob."""
        try:
            return json.dumps(instances)
        except (TypeError, ValueError):
            return str(instances)
