"""
tfstate_parser - A Python library for parsing Terraform state files and
scanning Terraform source directories.

Quick start::

    # --- State file only ---
    from tfstate_parser import TerraformStateParser

    parser = TerraformStateParser("terraform.tfstate")
    resources = parser.get_resources()
    graph = parser.get_dependency_graph()

    # --- .tf source + state cross-reference ---
    from tfstate_parser import TFScanner

    scanner = TFScanner("./infra", "terraform.tfstate")
    result = scanner.scan()
    print(result.summary())

    for match in result.matched:
        print(match.address)
        print("  local deps :", match.local_dependencies)
        print("  state deps :", match.state_dependencies)
"""

from .parser import TerraformStateParser
from .tf_scanner import TFScanner
from .state_migrator import StateMigrator
from .state_compat import StateCompatFixer, detect_local_terraform_version
from .models import (
    TerraformResource,
    DependencyGraph,
    LocalValue,
    TFResource,
    MatchedResource,
    ScanResult,
)
from .state_migrator import (
    MigrationResult,
    MigratedResource,
    ValidationResult,
)
from .state_compat import (
    CompatReport,
    CompatFix,
)

__all__ = [
    # State parser
    "TerraformStateParser",
    "TerraformResource",
    "DependencyGraph",
    # Source scanner
    "TFScanner",
    "TFResource",
    "LocalValue",
    "MatchedResource",
    "ScanResult",
    # State migrator
    "StateMigrator",
    "MigrationResult",
    "MigratedResource",
    "ValidationResult",
    # Compatibility fixer
    "StateCompatFixer",
    "CompatReport",
    "CompatFix",
    "detect_local_terraform_version",
]
__version__ = "4.0.0"
