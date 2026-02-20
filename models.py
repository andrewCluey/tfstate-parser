"""Data models for Terraform state parser."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class TerraformResource:
    """Represents a single resource in a Terraform state file."""

    address: str
    """Fully qualified resource address, e.g. 'aws_instance.web'."""

    module: str
    """Module path, e.g. 'module.networking'. Empty string for root module."""

    resource_type: str
    """Resource type, e.g. 'aws_instance'."""

    name: str
    """Resource logical name, e.g. 'web'."""

    provider: str
    """Provider string, e.g. 'provider["registry.terraform.io/hashicorp/aws"]'."""

    mode: str
    """Resource mode: 'managed' or 'data'."""

    instances: List[Dict[str, Any]] = field(default_factory=list)
    """List of instance objects (handles count/for_each)."""

    dependencies: List[str] = field(default_factory=list)
    """Explicit dependency addresses declared in state."""

    inferred_dependencies: Set[str] = field(default_factory=set)
    """Dependencies inferred by scanning attribute values for resource references."""

    @property
    def all_dependencies(self) -> List[str]:
        """Combined unique list of explicit and inferred dependencies."""
        combined = set(self.dependencies) | self.inferred_dependencies
        return sorted(combined)

    @property
    def instance_ids(self) -> List[Optional[str]]:
        """Return the id attribute from each instance, if present."""
        return [inst.get("attributes", {}).get("id") for inst in self.instances]

    def __repr__(self) -> str:
        return (
            f"TerraformResource(address={self.address!r}, "
            f"type={self.resource_type!r}, "
            f"dependencies={self.all_dependencies})"
        )


@dataclass
class DependencyGraph:
    """A directed dependency graph of Terraform resources."""

    resources: Dict[str, TerraformResource] = field(default_factory=dict)
    """Map of resource address -> TerraformResource."""

    def dependents_of(self, address: str) -> List[str]:
        """Return all resources that depend on the given resource address."""
        return [
            res.address
            for res in self.resources.values()
            if address in res.all_dependencies
        ]

    def dependencies_of(self, address: str) -> List[str]:
        """Return all dependencies of the given resource address."""
        resource = self.resources.get(address)
        return resource.all_dependencies if resource else []

    def topological_sort(self) -> List[str]:
        """
        Return resource addresses in topological order (dependencies first).
        Raises ValueError if a cycle is detected.
        """
        visited: Set[str] = set()
        temp: Set[str] = set()
        order: List[str] = []

        def visit(addr: str) -> None:
            if addr in temp:
                raise ValueError(f"Cycle detected involving resource: {addr}")
            if addr not in visited:
                temp.add(addr)
                for dep in self.dependencies_of(addr):
                    if dep in self.resources:
                        visit(dep)
                temp.discard(addr)
                visited.add(addr)
                order.append(addr)

        for addr in self.resources:
            visit(addr)

        return order

    def __repr__(self) -> str:
        return f"DependencyGraph(resources={list(self.resources.keys())})"


# ---------------------------------------------------------------------------
# .tf source file models
# ---------------------------------------------------------------------------

@dataclass
class LocalValue:
    """Represents a `locals { }` value declared in .tf source files."""

    name: str
    """The local name, e.g. 'common_tags'. Referenced as local.common_tags."""

    expression: str
    """Raw HCL expression string (best-effort text extraction)."""

    source_file: str
    """Path to the .tf file where this local is declared."""

    references: List[str] = field(default_factory=list)
    """Other locals or resources referenced inside this local's expression."""

    @property
    def address(self) -> str:
        """Canonical address, e.g. 'local.common_tags'."""
        return f"local.{self.name}"

    def __repr__(self) -> str:
        return f"LocalValue(address={self.address!r}, references={self.references})"


@dataclass
class TFResource:
    """
    A resource declared in .tf source files (before deployment).

    This is distinct from :class:`TerraformResource`, which comes from the
    state file (after deployment). Use :class:`MatchedResource` to correlate
    the two.
    """

    resource_type: str
    """e.g. 'aws_instance'"""

    name: str
    """Logical name, e.g. 'web'"""

    mode: str
    """'managed' or 'data'"""

    source_file: str
    """Path to the .tf file where this resource is declared."""

    raw_block: str
    """Raw HCL block text (best-effort extraction)."""

    resource_dependencies: List[str] = field(default_factory=list)
    """Other resource addresses referenced inside this block (type.name)."""

    local_dependencies: List[str] = field(default_factory=list)
    """local.xxx references found inside this block."""

    @property
    def address(self) -> str:
        if self.mode == "data":
            return f"data.{self.resource_type}.{self.name}"
        return f"{self.resource_type}.{self.name}"

    def __repr__(self) -> str:
        return (
            f"TFResource(address={self.address!r}, "
            f"resource_deps={self.resource_dependencies}, "
            f"local_deps={self.local_dependencies})"
        )


@dataclass
class MatchedResource:
    """
    Correlates a resource declared in .tf source files with its entry in the
    Terraform state file.
    """

    tf_resource: TFResource
    """The source-code declaration."""

    state_resource: Optional[TerraformResource]
    """The corresponding state entry, or None if not yet deployed."""

    # Convenience pass-throughs ------------------------------------------

    @property
    def address(self) -> str:
        return self.tf_resource.address

    @property
    def is_deployed(self) -> bool:
        """True if this resource exists in the state file."""
        return self.state_resource is not None

    @property
    def resource_dependencies(self) -> List[str]:
        """Resource-to-resource dependencies from the .tf source."""
        return self.tf_resource.resource_dependencies

    @property
    def local_dependencies(self) -> List[str]:
        """local.xxx dependencies from the .tf source."""
        return self.tf_resource.local_dependencies

    @property
    def state_dependencies(self) -> List[str]:
        """All dependencies recorded in the state file (if deployed)."""
        return self.state_resource.all_dependencies if self.state_resource else []

    @property
    def instance_ids(self) -> List[Optional[str]]:
        """Instance IDs from the state file (empty if not deployed)."""
        return self.state_resource.instance_ids if self.state_resource else []

    def __repr__(self) -> str:
        deployed = "deployed" if self.is_deployed else "not deployed"
        return (
            f"MatchedResource(address={self.address!r}, {deployed}, "
            f"resource_deps={self.resource_dependencies}, "
            f"local_deps={self.local_dependencies})"
        )


@dataclass
class ScanResult:
    """
    Complete result of scanning a directory of .tf files and cross-referencing
    with a Terraform state file.
    """

    matched: List[MatchedResource] = field(default_factory=list)
    """Resources found in .tf files, correlated with state."""

    locals: Dict[str, LocalValue] = field(default_factory=dict)
    """All local values declared across all .tf files, keyed by address."""

    unmatched_state_resources: List[TerraformResource] = field(default_factory=list)
    """Resources in state that have no corresponding .tf declaration (e.g. orphans)."""

    scanned_files: List[str] = field(default_factory=list)
    """List of .tf files that were scanned."""

    @property
    def deployed(self) -> List[MatchedResource]:
        """Matched resources that exist in the state file."""
        return [m for m in self.matched if m.is_deployed]

    @property
    def undeployed(self) -> List[MatchedResource]:
        """Matched resources NOT yet in the state file."""
        return [m for m in self.matched if not m.is_deployed]

    def summary(self) -> str:
        lines = [
            "Terraform Scan Result",
            f"  Scanned files        : {len(self.scanned_files)}",
            f"  .tf resources        : {len(self.matched)}",
            f"    - deployed         : {len(self.deployed)}",
            f"    - not yet deployed : {len(self.undeployed)}",
            f"  State-only (orphans) : {len(self.unmatched_state_resources)}",
            f"  Local values         : {len(self.locals)}",
            "",
            "Resources:",
        ]
        for m in sorted(self.matched, key=lambda x: x.address):
            status = "✓ deployed" if m.is_deployed else "✗ not deployed"
            lines.append(f"  [{status}] {m.address}  (file: {m.tf_resource.source_file})")
            if m.resource_dependencies:
                lines.append(f"      resource deps : {', '.join(m.resource_dependencies)}")
            if m.local_dependencies:
                lines.append(f"      local deps    : {', '.join(m.local_dependencies)}")
            if m.state_dependencies:
                lines.append(f"      state deps    : {', '.join(m.state_dependencies)}")
        if self.unmatched_state_resources:
            lines.append("")
            lines.append("State-only resources (no .tf declaration found):")
            for r in self.unmatched_state_resources:
                lines.append(f"  {r.address}")
        if self.locals:
            lines.append("")
            lines.append("Local values:")
            for loc in sorted(self.locals.values(), key=lambda l: l.name):
                ref_str = ", ".join(loc.references) if loc.references else "(none)"
                lines.append(f"  {loc.address}  →  refs: {ref_str}  (file: {loc.source_file})")
        return "\n".join(lines)
