# tfstate_parser

A reusable Python library for parsing Terraform state files, scanning Terraform source directories, and migrating resources between state files. Extracts all resources and dependencies, cross-references `.tf` source declarations against deployed state — including `local.xxx` relationships — and can move matched resources to a new state file with full validation and cleanup command generation.

## Features

- Parse `terraform.tfstate` files and extract all resources with full dependency information
- Scan a directory of `.tf` source files to discover resources before deployment
- Correlate `.tf` declarations against state (deployed vs. not yet deployed)
- Extract resource-to-resource dependencies and `local.xxx` dependencies from source
- Detect orphaned state resources with no corresponding `.tf` declaration
- **Migrate** matched resources from a source state to a new destination state file
- **Validate** the destination state against the scanned `.tf` files
- **Generate** `terraform state mv` reference commands and a `terraform state rm` cleanup script
- Build a full dependency graph with topological sort and cycle detection
- Zero mandatory dependencies — uses `python-hcl2` if available, otherwise a built-in regex parser

---

## Installation

Copy the `tfstate_parser/` package directory into your project, or install as a local package:

```bash
pip install -e .
```

Optionally install `python-hcl2` for more accurate `.tf` file parsing:

```bash
pip install python-hcl2
```

---

## Quick Start

### Full pipeline: scan → migrate → validate → cleanup

```python
from tfstate_parser import TFScanner, StateMigrator

# 1. Scan .tf files and cross-reference with the source state
scanner = TFScanner("./infra", "source.tfstate")
result = scanner.scan()
print(result.summary())

# 2. Migrate all deployed matched resources to a new state file
migrator = StateMigrator(
    scan_result=result,
    source_state_path="source.tfstate",
    destination_state_path="migrated.tfstate",
)

# Optional: dry run first — validates without writing any files
dry = migrator.dry_run()
print(dry.summary())

# Real migration — writes migrated.tfstate
migration = migrator.migrate()
print(migration.summary())

# 3. Check validation result
if migration.validation.passed:
    print("✓ Destination state is valid")

    # 4. Write cleanup script for removing resources from the source state
    with open("cleanup.sh", "w") as f:
        f.write(migration.cleanup_script())

    # Optionally write a reference script of equivalent terraform state mv commands
    with open("state_mv_reference.sh", "w") as f:
        f.write(migration.mv_script())
else:
    for error in migration.validation.errors:
        print("ERROR:", error)
```

### Scanning .tf source files + state only

```python
from tfstate_parser import TFScanner

scanner = TFScanner("./infra", "terraform.tfstate")
result = scanner.scan()

for match in result.matched:
    print(match.address)
    print("  deployed:     ", match.is_deployed)
    print("  resource deps:", match.resource_dependencies)
    print("  local deps:   ", match.local_dependencies)
    print("  state deps:   ", match.state_dependencies)
```

### Parsing a state file directly

```python
from tfstate_parser import TerraformStateParser

parser = TerraformStateParser("terraform.tfstate")

for r in parser.get_resources():
    print(r.address, "->", r.all_dependencies)

graph = parser.get_dependency_graph()
print(graph.dependents_of("aws_vpc.main"))
print(graph.topological_sort())
```

---

## Example Output

### `result.summary()` after scanning

```
Terraform Scan Result
  Scanned files        : 2
  .tf resources        : 4
    - deployed         : 2
    - not yet deployed : 2
  State-only (orphans) : 1
  Local values         : 3

Resources:
  [✗ not deployed] aws_instance.web  (file: compute.tf)
      resource deps : aws_subnet.public, data.aws_ami.ubuntu
      local deps    : local.common_tags, local.instance_type
  [✓ deployed] aws_subnet.public  (file: main.tf)
      resource deps : aws_vpc.main
      local deps    : local.common_tags
      state deps    : aws_vpc.main
  [✓ deployed] aws_vpc.main  (file: main.tf)
      local deps    : local.common_tags
  [✗ not deployed] data.aws_ami.ubuntu  (file: compute.tf)

State-only resources (no .tf declaration found):
  aws_s3_bucket.orphan

Local values:
  local.common_tags  →  refs: local.env
  local.env          →  refs: (none)
```

### `migration.summary()` after migrating

```
Migration Result
  Source state      : source.tfstate
  Destination state : migrated.tfstate
  Migrated          : 2 resources
  Skipped           : 0 resources
  Validation        : PASSED ✓
    WARNING : Resource 'aws_instance.web' is declared in .tf files but has no state entry — it cannot be migrated.

Migrated resources:
  ✓  aws_vpc.main  (1 instance(s))
  ✓  aws_subnet.public  (1 instance(s))
```

### `migration.cleanup_script()`

```bash
#!/usr/bin/env bash
# Auto-generated cleanup script
# Remove migrated resources from: source.tfstate
# Generated: 2026-02-19T12:00:00+00:00
#
# Run this ONLY after confirming the destination state is valid.
# Resources being removed:
#   aws_vpc.main
#   aws_subnet.public

terraform state rm -state=source.tfstate aws_vpc.main
terraform state rm -state=source.tfstate aws_subnet.public
```

### `migration.mv_script()` (for reference/audit)

```bash
#!/usr/bin/env bash
# Auto-generated state mv reference script
# Source state     : source.tfstate
# Destination state: migrated.tfstate

terraform state mv -state=source.tfstate -state-out=migrated.tfstate aws_vpc.main aws_vpc.main
terraform state mv -state=source.tfstate -state-out=migrated.tfstate aws_subnet.public aws_subnet.public
```

---

## API Reference

### `StateMigrator`

Migrates deployed resources from a source Terraform state into a new destination state file.

```python
StateMigrator(scan_result, source_state_path, destination_state_path, *, overwrite=False)
```

| Parameter | Type | Description |
|---|---|---|
| `scan_result` | `ScanResult` | Result from `TFScanner.scan()` |
| `source_state_path` | `str \| Path` | Path to the source `terraform.tfstate` |
| `destination_state_path` | `str \| Path` | Path where the new state file will be written |
| `overwrite` | `bool` | Allow overwriting an existing destination file (default: `False`) |

#### Methods

| Method | Returns | Description |
|---|---|---|
| `migrate()` | `MigrationResult` | Execute the full migration and write the destination state |
| `dry_run()` | `MigrationResult` | Validate the migration plan without writing any files |

#### How migration works

1. All `deployed` resources from the `ScanResult` are extracted from the source state JSON.
2. A new destination state file is written containing only those resources, with the serial number incremented by 1 and the lineage preserved.
3. The destination state is validated against the scan result.
4. CLI command lists are generated for reference and cleanup.

No Terraform CLI is required — state files are manipulated directly as JSON.

---

### `MigrationResult`

Returned by `StateMigrator.migrate()` and `StateMigrator.dry_run()`.

| Attribute/Method | Type | Description |
|---|---|---|
| `migrated` | `List[MigratedResource]` | Resources successfully written to the destination state |
| `skipped` | `List[str]` | Resource addresses skipped (e.g. found in scan but not in source state) |
| `migrated_addresses` | `List[str]` | Convenience list of migrated addresses |
| `destination_state_path` | `str` | Path of the written destination state |
| `source_state_path` | `str` | Path of the source state |
| `validation` | `ValidationResult` | Result of post-migration validation |
| `state_mv_commands()` | `List[str]` | `terraform state mv` commands (reference/audit) |
| `state_rm_commands()` | `List[str]` | `terraform state rm` commands for source cleanup |
| `cleanup_script()` | `str` | Shell script to remove migrated resources from source state |
| `mv_script()` | `str` | Shell script of equivalent `terraform state mv` commands |
| `summary()` | `str` | Human-readable report |

---

### `MigratedResource`

A record of a single resource that was migrated.

| Attribute | Type | Description |
|---|---|---|
| `address` | `str` | Resource address, e.g. `aws_instance.web` |
| `instance_count` | `int` | Number of instances migrated |
| `state_mv_command` | `str` | Equivalent `terraform state mv` CLI command |
| `state_rm_command` | `str` | `terraform state rm` command for source cleanup |

---

### `ValidationResult`

Result of validating the destination state against `.tf` source files.

| Attribute | Type | Description |
|---|---|---|
| `passed` | `bool` | `True` if all checks passed |
| `errors` | `List[str]` | Validation failures (migration unsafe if non-empty) |
| `warnings` | `List[str]` | Non-fatal issues (e.g. undeployed resources) |
| `checked_resources` | `List[str]` | Addresses of all resources that were checked |

#### Validation checks performed

1. Every migrated resource address is present in the destination state.
2. Instance counts in the destination match the source exactly.
3. No unexpected resources appear in the destination that weren't in the migration plan.
4. Warnings are raised for deployed resources that weren't migrated, and for `.tf`-declared resources with no state entry.

---

### `TFScanner`

Scans a directory of `.tf` files and cross-references discovered resources with a Terraform state file.

```python
TFScanner(tf_dir, state_source=None, *, recursive=False, infer_state_dependencies=True)
```

| Parameter | Type | Description |
|---|---|---|
| `tf_dir` | `str \| Path` | Directory containing `.tf` files |
| `state_source` | `str \| Path \| IO \| dict \| None` | State file to cross-reference against. Pass `None` to skip. |
| `recursive` | `bool` | Recurse into subdirectories (default: `False`) |
| `infer_state_dependencies` | `bool` | Infer extra deps by scanning state attribute values (default: `True`) |

| Method | Returns | Description |
|---|---|---|
| `scan()` | `ScanResult` | Execute the scan and return full results |

---

### `ScanResult`

| Attribute | Type | Description |
|---|---|---|
| `matched` | `List[MatchedResource]` | All resources found in `.tf` files, correlated with state |
| `locals` | `Dict[str, LocalValue]` | All `local.xxx` values keyed by address |
| `unmatched_state_resources` | `List[TerraformResource]` | State resources with no `.tf` declaration (orphans) |
| `scanned_files` | `List[str]` | `.tf` files that were scanned |
| `deployed` | `List[MatchedResource]` | Subset of `matched` that exist in state |
| `undeployed` | `List[MatchedResource]` | Subset of `matched` not yet in state |
| `summary()` | `str` | Human-readable report |

---

### `MatchedResource`

| Attribute/Property | Type | Description |
|---|---|---|
| `address` | `str` | Resource address, e.g. `aws_instance.web` |
| `is_deployed` | `bool` | `True` if the resource exists in the state file |
| `resource_dependencies` | `List[str]` | Resource-to-resource deps from `.tf` source |
| `local_dependencies` | `List[str]` | `local.xxx` references from `.tf` source |
| `state_dependencies` | `List[str]` | Dependencies recorded in the state file |
| `instance_ids` | `List[str]` | Deployed instance IDs from state |
| `tf_resource` | `TFResource` | The raw source declaration |
| `state_resource` | `TerraformResource \| None` | The raw state entry |

---

### `TFResource`

| Attribute | Type | Description |
|---|---|---|
| `address` | `str` | e.g. `aws_instance.web` or `data.aws_ami.ubuntu` |
| `resource_type` | `str` | e.g. `aws_instance` |
| `name` | `str` | Logical name |
| `mode` | `str` | `"managed"` or `"data"` |
| `source_file` | `str` | Path to the declaring `.tf` file |
| `resource_dependencies` | `List[str]` | Resource addresses referenced in the block |
| `local_dependencies` | `List[str]` | `local.xxx` references in the block |

---

### `LocalValue`

| Attribute | Type | Description |
|---|---|---|
| `address` | `str` | e.g. `local.common_tags` |
| `name` | `str` | The key name |
| `expression` | `str` | Raw HCL expression text |
| `source_file` | `str` | Path to the declaring `.tf` file |
| `references` | `List[str]` | Other `local.xxx` values or resources referenced in the expression |

---

### `TerraformStateParser`

Parses a `terraform.tfstate` file directly.

```python
TerraformStateParser(source, *, infer_dependencies=True)
```

| Method | Returns | Description |
|---|---|---|
| `get_resources()` | `List[TerraformResource]` | All resources as a flat list |
| `get_resource_map()` | `Dict[str, TerraformResource]` | Resources keyed by address |
| `get_dependencies()` | `Dict[str, List[str]]` | Address → dependency list |
| `get_dependency_graph()` | `DependencyGraph` | Full graph object |
| `summary()` | `str` | Human-readable summary |

Properties: `version`, `terraform_version`, `serial`, `lineage`

---

### `TerraformResource`

| Attribute | Type | Description |
|---|---|---|
| `address` | `str` | Full address, e.g. `module.vpc.aws_subnet.public` |
| `module` | `str` | Module path (empty string for root module) |
| `resource_type` | `str` | e.g. `aws_instance` |
| `name` | `str` | Logical name |
| `provider` | `str` | Provider string |
| `mode` | `str` | `"managed"` or `"data"` |
| `instances` | `List[dict]` | Raw instance objects |
| `dependencies` | `List[str]` | Explicit deps recorded in state |
| `inferred_dependencies` | `Set[str]` | Deps inferred by attribute scanning |
| `all_dependencies` | `List[str]` | Combined sorted union |
| `instance_ids` | `List[str]` | `id` attribute from each instance |

---

### `DependencyGraph`

| Method | Returns | Description |
|---|---|---|
| `dependencies_of(address)` | `List[str]` | What does this resource depend on? |
| `dependents_of(address)` | `List[str]` | What depends on this resource? |
| `topological_sort()` | `List[str]` | Dependency order, leaves first. Raises `ValueError` on cycles. |

---

## Dependency Detection

### In `.tf` source files (`TFScanner`)

Three strategies are used inside resource blocks:

1. **`data.type.name` references** — `data.aws_ami.ubuntu.id` → `data.aws_ami.ubuntu`
2. **Resource references** — `aws_vpc.main.id` → `aws_vpc.main`
3. **Local references** — any `local.xxx` expression → captured in `local_dependencies`

Local values are scanned for their own cross-references, allowing chains like `local.name_prefix` → `local.env` to be traced.

### In state files (`TerraformStateParser`)

1. **Explicit dependencies** — written directly into the state file by Terraform (both resource-level and per-instance).
2. **Inferred dependencies** — when `infer_dependencies=True`, attribute values are scanned for strings matching known resource addresses.

`all_dependencies` always returns the sorted union of both.

---

## Running Tests

```bash
pip install pytest
pytest tfstate_parser/tests.py -v
```

---

## Supported Formats

- Terraform state format **version 4** (Terraform 0.12 and later)
- HCL2 `.tf` files (Terraform 0.12 and later)
- Root module and child module resources
- `managed` and `data` source resources
- `count` and `for_each` instances (multiple instances per resource block)