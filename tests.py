"""Tests for tfstate_parser library."""

import json
import pytest
from tfstate_parser import TerraformStateParser, TerraformResource, DependencyGraph


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_STATE = {
    "version": 4,
    "terraform_version": "1.5.0",
    "serial": 12,
    "lineage": "abc-123",
    "resources": [
        {
            "mode": "managed",
            "type": "aws_vpc",
            "name": "main",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 1,
                    "attributes": {
                        "id": "vpc-0abc123",
                        "cidr_block": "10.0.0.0/16",
                    },
                }
            ],
        },
        {
            "mode": "managed",
            "type": "aws_subnet",
            "name": "public",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "dependencies": ["aws_vpc.main"],
            "instances": [
                {
                    "schema_version": 1,
                    "attributes": {
                        "id": "subnet-0xyz789",
                        "vpc_id": "vpc-0abc123",
                        "cidr_block": "10.0.1.0/24",
                    },
                }
            ],
        },
        {
            "mode": "managed",
            "type": "aws_instance",
            "name": "web",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "dependencies": ["aws_subnet.public", "aws_vpc.main"],
            "instances": [
                {
                    "schema_version": 1,
                    "attributes": {
                        "id": "i-0123456789abcdef0",
                        "subnet_id": "subnet-0xyz789",
                        "ami": "ami-0abcdef1234567890",
                    },
                }
            ],
        },
        {
            "mode": "data",
            "type": "aws_ami",
            "name": "ubuntu",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 0,
                    "attributes": {
                        "id": "ami-0abcdef1234567890",
                        "name": "ubuntu-22.04",
                    },
                }
            ],
        },
        {
            "module": "module.networking",
            "mode": "managed",
            "type": "aws_security_group",
            "name": "default",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "dependencies": ["aws_vpc.main"],
            "instances": [
                {
                    "schema_version": 1,
                    "attributes": {
                        "id": "sg-0deadbeef",
                        "vpc_id": "aws_vpc.main",
                    },
                }
            ],
        },
    ],
}


@pytest.fixture
def parser():
    return TerraformStateParser(SAMPLE_STATE)


@pytest.fixture
def graph(parser):
    return parser.get_dependency_graph()


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------


def test_version(parser):
    assert parser.version == 4


def test_terraform_version(parser):
    assert parser.terraform_version == "1.5.0"


def test_serial(parser):
    assert parser.serial == 12


def test_lineage(parser):
    assert parser.lineage == "abc-123"


# ---------------------------------------------------------------------------
# Resource parsing tests
# ---------------------------------------------------------------------------


def test_resource_count(parser):
    assert len(parser.get_resources()) == 5


def test_resource_types(parser):
    types = {r.resource_type for r in parser.get_resources()}
    assert types == {
        "aws_vpc",
        "aws_subnet",
        "aws_instance",
        "aws_ami",
        "aws_security_group",
    }


def test_managed_resource_address():
    p = TerraformStateParser(SAMPLE_STATE)
    rmap = p.get_resource_map()
    assert "aws_vpc.main" in rmap
    assert "aws_subnet.public" in rmap
    assert "aws_instance.web" in rmap


def test_data_resource_address():
    p = TerraformStateParser(SAMPLE_STATE)
    rmap = p.get_resource_map()
    assert "data.aws_ami.ubuntu" in rmap


def test_module_resource_address():
    p = TerraformStateParser(SAMPLE_STATE)
    rmap = p.get_resource_map()
    assert "module.networking.aws_security_group.default" in rmap


def test_resource_mode(parser):
    rmap = parser.get_resource_map()
    assert rmap["data.aws_ami.ubuntu"].mode == "data"
    assert rmap["aws_vpc.main"].mode == "managed"


def test_resource_instance_ids(parser):
    rmap = parser.get_resource_map()
    assert rmap["aws_vpc.main"].instance_ids == ["vpc-0abc123"]


def test_resource_is_TerraformResource(parser):
    for r in parser.get_resources():
        assert isinstance(r, TerraformResource)


# ---------------------------------------------------------------------------
# Explicit dependency tests
# ---------------------------------------------------------------------------


def test_explicit_dependencies(parser):
    rmap = parser.get_resource_map()
    assert "aws_vpc.main" in rmap["aws_subnet.public"].dependencies
    assert "aws_subnet.public" in rmap["aws_instance.web"].dependencies
    assert "aws_vpc.main" in rmap["aws_instance.web"].dependencies


def test_no_explicit_deps_for_vpc(parser):
    rmap = parser.get_resource_map()
    assert rmap["aws_vpc.main"].dependencies == []


# ---------------------------------------------------------------------------
# Dependency map tests
# ---------------------------------------------------------------------------


def test_get_dependencies_keys(parser):
    deps = parser.get_dependencies()
    assert "aws_instance.web" in deps
    assert "aws_vpc.main" in deps


def test_get_dependencies_values(parser):
    deps = parser.get_dependencies()
    assert "aws_vpc.main" in deps["aws_subnet.public"]


# ---------------------------------------------------------------------------
# Inferred dependency tests
# ---------------------------------------------------------------------------


def test_inferred_deps_enabled_by_default(parser):
    rmap = parser.get_resource_map()
    sg = rmap["module.networking.aws_security_group.default"]
    # The attribute vpc_id contains "aws_vpc.main" as a string value
    assert "aws_vpc.main" in sg.inferred_dependencies


def test_inferred_deps_disabled():
    p = TerraformStateParser(SAMPLE_STATE, infer_dependencies=False)
    rmap = p.get_resource_map()
    sg = rmap["module.networking.aws_security_group.default"]
    assert sg.inferred_dependencies == set()


def test_all_dependencies_combines_both(parser):
    rmap = parser.get_resource_map()
    sg = rmap["module.networking.aws_security_group.default"]
    all_deps = sg.all_dependencies
    assert "aws_vpc.main" in all_deps  # from both explicit and inferred


# ---------------------------------------------------------------------------
# DependencyGraph tests
# ---------------------------------------------------------------------------


def test_graph_resources_count(graph):
    assert len(graph.resources) == 5


def test_graph_dependencies_of(graph):
    deps = graph.dependencies_of("aws_instance.web")
    assert "aws_subnet.public" in deps
    assert "aws_vpc.main" in deps


def test_graph_dependencies_of_unknown():
    g = DependencyGraph()
    assert g.dependencies_of("nonexistent.resource") == []


def test_graph_dependents_of(graph):
    dependents = graph.dependents_of("aws_vpc.main")
    assert "aws_subnet.public" in dependents
    assert "aws_instance.web" in dependents


def test_topological_sort_order(graph):
    order = graph.topological_sort()
    # VPC must come before subnet and instance
    assert order.index("aws_vpc.main") < order.index("aws_subnet.public")
    assert order.index("aws_subnet.public") < order.index("aws_instance.web")


def test_topological_sort_cycle_detection():
    # Manually craft a cyclic state
    cyclic = {
        "version": 4,
        "terraform_version": "1.5.0",
        "serial": 1,
        "lineage": "x",
        "resources": [
            {
                "mode": "managed",
                "type": "aws_a",
                "name": "foo",
                "provider": "p",
                "dependencies": ["aws_b.bar"],
                "instances": [],
            },
            {
                "mode": "managed",
                "type": "aws_b",
                "name": "bar",
                "provider": "p",
                "dependencies": ["aws_a.foo"],
                "instances": [],
            },
        ],
    }
    g = TerraformStateParser(cyclic, infer_dependencies=False).get_dependency_graph()
    with pytest.raises(ValueError, match="Cycle detected"):
        g.topological_sort()


# ---------------------------------------------------------------------------
# Loading tests
# ---------------------------------------------------------------------------


def test_load_from_file(tmp_path):
    state_path = tmp_path / "terraform.tfstate"
    state_path.write_text(json.dumps(SAMPLE_STATE))
    p = TerraformStateParser(str(state_path))
    assert len(p.get_resources()) == 5


def test_load_from_path_object(tmp_path):
    from pathlib import Path

    state_path = tmp_path / "terraform.tfstate"
    state_path.write_text(json.dumps(SAMPLE_STATE))
    p = TerraformStateParser(Path(state_path))
    assert len(p.get_resources()) == 5


def test_load_from_file_object(tmp_path):
    state_path = tmp_path / "terraform.tfstate"
    state_path.write_text(json.dumps(SAMPLE_STATE))
    with open(state_path) as fh:
        p = TerraformStateParser(fh)
    assert len(p.get_resources()) == 5


# ---------------------------------------------------------------------------
# Summary test
# ---------------------------------------------------------------------------


def test_summary_contains_addresses(parser):
    s = parser.summary()
    assert "aws_vpc.main" in s
    assert "aws_instance.web" in s
    assert "data.aws_ami.ubuntu" in s


# ===========================================================================
# TFScanner tests
# ===========================================================================

import tempfile
import os
from tfstate_parser import TFScanner, ScanResult, MatchedResource

# ---------------------------------------------------------------------------
# Sample .tf content
# ---------------------------------------------------------------------------

TF_MAIN = """\
locals {
  env        = "production"
  name_prefix = "prod-${local.env}"
  common_tags = {
    Environment = local.env
    Owner       = "platform-team"
  }
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  tags       = local.common_tags
}

resource "aws_subnet" "public" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
  tags       = local.common_tags
}
"""

TF_COMPUTE = """\
locals {
  instance_type = "t3.micro"
}

resource "aws_instance" "web" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = local.instance_type
  subnet_id     = aws_subnet.public.id
  tags          = local.common_tags
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
}
"""

STATE = {
    "version": 4,
    "terraform_version": "1.5.0",
    "serial": 12,
    "lineage": "abc-123",
    "resources": [
        {
            "mode": "managed",
            "type": "aws_vpc",
            "name": "main",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {"attributes": {"id": "vpc-0abc123", "cidr_block": "10.0.0.0/16"}}
            ],
        },
        {
            "mode": "managed",
            "type": "aws_subnet",
            "name": "public",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "dependencies": ["aws_vpc.main"],
            "instances": [
                {"attributes": {"id": "subnet-xyz", "vpc_id": "vpc-0abc123"}}
            ],
        },
        # aws_instance.web is NOT in state (simulates undeployed resource)
    ],
}


@pytest.fixture
def tf_dir(tmp_path):
    (tmp_path / "main.tf").write_text(TF_MAIN, encoding="utf-8")
    (tmp_path / "compute.tf").write_text(TF_COMPUTE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def scan_result(tf_dir):
    scanner = TFScanner(tf_dir, STATE)
    return scanner.scan()


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def test_scanned_files_count(scan_result):
    assert len(scan_result.scanned_files) == 2


def test_scanned_files_names(scan_result):
    names = {Path(f).name for f in scan_result.scanned_files}
    assert names == {"main.tf", "compute.tf"}


# ---------------------------------------------------------------------------
# Resource discovery from .tf files
# ---------------------------------------------------------------------------


def test_matched_resource_count(scan_result):
    # aws_vpc.main, aws_subnet.public, aws_instance.web, data.aws_ami.ubuntu
    assert len(scan_result.matched) == 4


def test_matched_resource_addresses(scan_result):
    addresses = {m.address for m in scan_result.matched}
    assert "aws_vpc.main" in addresses
    assert "aws_subnet.public" in addresses
    assert "aws_instance.web" in addresses
    assert "data.aws_ami.ubuntu" in addresses


def test_all_are_matched_resource_instances(scan_result):
    for m in scan_result.matched:
        assert isinstance(m, MatchedResource)


# ---------------------------------------------------------------------------
# Deployed vs undeployed
# ---------------------------------------------------------------------------


def test_deployed_resources(scan_result):
    deployed = {m.address for m in scan_result.deployed}
    assert "aws_vpc.main" in deployed
    assert "aws_subnet.public" in deployed


def test_undeployed_resources(scan_result):
    undeployed = {m.address for m in scan_result.undeployed}
    assert "aws_instance.web" in undeployed


def test_is_deployed_flag(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    assert by_addr["aws_vpc.main"].is_deployed is True
    assert by_addr["aws_instance.web"].is_deployed is False


# ---------------------------------------------------------------------------
# Local variable parsing
# ---------------------------------------------------------------------------


def test_locals_found(scan_result):
    assert "local.env" in scan_result.locals
    assert "local.name_prefix" in scan_result.locals
    assert "local.common_tags" in scan_result.locals
    assert "local.instance_type" in scan_result.locals


def test_local_references(scan_result):
    # common_tags references local.env
    common_tags = scan_result.locals["local.common_tags"]
    assert "local.env" in common_tags.references

    # name_prefix also references local.env
    name_prefix = scan_result.locals["local.name_prefix"]
    assert "local.env" in name_prefix.references


def test_local_address_property(scan_result):
    loc = scan_result.locals["local.env"]
    assert loc.address == "local.env"
    assert loc.name == "env"


# ---------------------------------------------------------------------------
# Resource local dependency extraction
# ---------------------------------------------------------------------------


def test_vpc_local_deps(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    vpc = by_addr["aws_vpc.main"]
    assert "local.common_tags" in vpc.local_dependencies


def test_subnet_local_deps(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    subnet = by_addr["aws_subnet.public"]
    assert "local.common_tags" in subnet.local_dependencies


def test_instance_local_deps(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    inst = by_addr["aws_instance.web"]
    assert "local.instance_type" in inst.local_dependencies
    assert "local.common_tags" in inst.local_dependencies


# ---------------------------------------------------------------------------
# Resource-to-resource dependency extraction
# ---------------------------------------------------------------------------


def test_subnet_resource_deps(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    subnet = by_addr["aws_subnet.public"]
    assert "aws_vpc.main" in subnet.resource_dependencies


def test_instance_resource_deps(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    inst = by_addr["aws_instance.web"]
    assert "aws_subnet.public" in inst.resource_dependencies


def test_vpc_no_resource_deps(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    assert by_addr["aws_vpc.main"].resource_dependencies == []


# ---------------------------------------------------------------------------
# State dependency pass-through
# ---------------------------------------------------------------------------


def test_state_deps_for_deployed(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    subnet = by_addr["aws_subnet.public"]
    assert "aws_vpc.main" in subnet.state_dependencies


def test_state_deps_empty_for_undeployed(scan_result):
    by_addr = {m.address: m for m in scan_result.matched}
    assert by_addr["aws_instance.web"].state_dependencies == []


# ---------------------------------------------------------------------------
# Unmatched state resources
# ---------------------------------------------------------------------------


def test_no_unmatched_state_resources(scan_result):
    # Both state resources (vpc + subnet) are matched by .tf declarations
    assert scan_result.unmatched_state_resources == []


def test_unmatched_state_resources_detected():
    # Add an extra state resource with no .tf counterpart
    extra_state = dict(STATE)
    extra_state["resources"] = list(STATE["resources"]) + [
        {
            "mode": "managed",
            "type": "aws_s3_bucket",
            "name": "orphan",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "main.tf").write_text(TF_MAIN)
        scanner = TFScanner(tmpdir, extra_state)
        result = scanner.scan()
    orphan_addrs = [r.address for r in result.unmatched_state_resources]
    assert "aws_s3_bucket.orphan" in orphan_addrs


# ---------------------------------------------------------------------------
# No state file
# ---------------------------------------------------------------------------


def test_scan_without_state(tf_dir):
    scanner = TFScanner(tf_dir, None)
    result = scanner.scan()
    assert len(result.matched) == 4
    assert all(not m.is_deployed for m in result.matched)
    assert result.unmatched_state_resources == []


# ---------------------------------------------------------------------------
# Summary smoke test
# ---------------------------------------------------------------------------


def test_summary_output(scan_result):
    summary = scan_result.summary()
    assert "aws_vpc.main" in summary
    assert "aws_instance.web" in summary
    assert "local.env" in summary
    assert "local.common_tags" in summary


# ===========================================================================
# StateMigrator tests
# ===========================================================================

import json
import tempfile
from pathlib import Path
from tfstate_parser import TFScanner, StateMigrator, MigrationResult, ValidationResult

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TF_MAIN_MIG = """\
locals {
  env = "production"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  tags       = local.env
}

resource "aws_subnet" "public" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
}
"""

TF_COMPUTE_MIG = """\
resource "aws_instance" "web" {
  ami       = "ami-123"
  subnet_id = aws_subnet.public.id
}
"""

# Source state contains vpc + subnet (deployed). aws_instance.web is NOT in state.
SOURCE_STATE = {
    "version": 4,
    "terraform_version": "1.5.0",
    "serial": 5,
    "lineage": "test-lineage-abc",
    "outputs": {},
    "resources": [
        {
            "mode": "managed",
            "type": "aws_vpc",
            "name": "main",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 1,
                    "attributes": {"id": "vpc-abc", "cidr_block": "10.0.0.0/16"},
                }
            ],
        },
        {
            "mode": "managed",
            "type": "aws_subnet",
            "name": "public",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "dependencies": ["aws_vpc.main"],
            "instances": [
                {
                    "schema_version": 1,
                    "attributes": {"id": "subnet-xyz", "vpc_id": "vpc-abc"},
                }
            ],
        },
        # Extra resource in state that has NO .tf declaration — should be unmatched
        {
            "mode": "managed",
            "type": "aws_s3_bucket",
            "name": "orphan",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [{"schema_version": 0, "attributes": {"id": "my-bucket"}}],
        },
    ],
}


@pytest.fixture
def migration_dirs(tmp_path):
    """Set up temp dirs with .tf files and a source state file."""
    tf_dir = tmp_path / "infra"
    tf_dir.mkdir()
    (tf_dir / "main.tf").write_text(TF_MAIN_MIG)
    (tf_dir / "compute.tf").write_text(TF_COMPUTE_MIG)

    source_state = tmp_path / "source.tfstate"
    source_state.write_text(json.dumps(SOURCE_STATE))

    dest_state = tmp_path / "dest.tfstate"

    return tf_dir, source_state, dest_state, tmp_path


@pytest.fixture
def scan_result_mig(migration_dirs):
    tf_dir, source_state, _, _ = migration_dirs
    return TFScanner(tf_dir, str(source_state)).scan()


@pytest.fixture
def migration_result(migration_dirs, scan_result_mig):
    _, source_state, dest_state, _ = migration_dirs
    migrator = StateMigrator(
        scan_result=scan_result_mig,
        source_state_path=str(source_state),
        destination_state_path=str(dest_state),
        overwrite=True,
    )
    return migrator.migrate(), dest_state


# ---------------------------------------------------------------------------
# Migration: basic outcome
# ---------------------------------------------------------------------------


def test_migration_returns_result(migration_result):
    result, _ = migration_result
    assert isinstance(result, MigrationResult)


def test_migration_migrated_count(migration_result):
    result, _ = migration_result
    # vpc + subnet deployed; aws_instance.web not in state; orphan has no .tf
    assert len(result.migrated) == 2


def test_migration_migrated_addresses(migration_result):
    result, _ = migration_result
    addrs = result.migrated_addresses
    assert "aws_vpc.main" in addrs
    assert "aws_subnet.public" in addrs


def test_migration_instance_counts(migration_result):
    result, _ = migration_result
    by_addr = {m.address: m for m in result.migrated}
    assert by_addr["aws_vpc.main"].instance_count == 1
    assert by_addr["aws_subnet.public"].instance_count == 1


def test_migration_skipped_is_empty(migration_result):
    # Nothing should be skipped (all deployed matched resources are in the source state)
    result, _ = migration_result
    assert result.skipped == []


# ---------------------------------------------------------------------------
# Migration: destination file written
# ---------------------------------------------------------------------------


def test_destination_file_created(migration_result):
    _, dest_state = migration_result
    assert dest_state.exists()


def test_destination_file_is_valid_json(migration_result):
    _, dest_state = migration_result
    with open(dest_state) as f:
        data = json.load(f)
    assert "resources" in data


def test_destination_serial_incremented(migration_result):
    _, dest_state = migration_result
    with open(dest_state) as f:
        data = json.load(f)
    assert data["serial"] == SOURCE_STATE["serial"] + 1


def test_destination_lineage_preserved(migration_result):
    _, dest_state = migration_result
    with open(dest_state) as f:
        data = json.load(f)
    assert data["lineage"] == SOURCE_STATE["lineage"]


def test_destination_contains_only_migrated(migration_result):
    result, dest_state = migration_result
    with open(dest_state) as f:
        data = json.load(f)
    dest_addrs = {
        (b.get("module", "") + "." if b.get("module") else "")
        + (
            f"data.{b['type']}.{b['name']}"
            if b.get("mode") == "data"
            else f"{b['type']}.{b['name']}"
        )
        for b in data["resources"]
    }
    assert dest_addrs == set(result.migrated_addresses)


def test_destination_does_not_contain_orphan(migration_result):
    _, dest_state = migration_result
    with open(dest_state) as f:
        data = json.load(f)
    dest_types = {b["type"] for b in data["resources"]}
    assert "aws_s3_bucket" not in dest_types


# ---------------------------------------------------------------------------
# Migration: validation
# ---------------------------------------------------------------------------


def test_validation_passed(migration_result):
    result, _ = migration_result
    assert result.validation is not None
    assert result.validation.passed is True


def test_validation_no_errors(migration_result):
    result, _ = migration_result
    assert result.validation.errors == []


def test_validation_warns_about_undeployed(migration_result):
    result, _ = migration_result
    # aws_instance.web is declared in .tf but not in state
    warning_text = " ".join(result.validation.warnings)
    assert "aws_instance.web" in warning_text


def test_validation_warns_about_orphan(migration_result):
    result, _ = migration_result
    # aws_s3_bucket.orphan is in state but has no .tf declaration —
    # it should appear as an unmatched resource (not a warning in validation,
    # but confirmed not migrated)
    assert "aws_s3_bucket.orphan" not in result.migrated_addresses


def test_validation_checked_resources(migration_result):
    result, _ = migration_result
    assert "aws_vpc.main" in result.validation.checked_resources
    assert "aws_subnet.public" in result.validation.checked_resources


# ---------------------------------------------------------------------------
# Migration: CLI commands
# ---------------------------------------------------------------------------


def test_state_mv_commands_generated(migration_result):
    result, _ = migration_result
    cmds = result.state_mv_commands()
    assert len(cmds) == 2
    for cmd in cmds:
        assert cmd.startswith("terraform state mv")


def test_state_mv_command_contains_address(migration_result):
    result, _ = migration_result
    cmds = result.state_mv_commands()
    combined = " ".join(cmds)
    assert "aws_vpc.main" in combined
    assert "aws_subnet.public" in combined


def test_state_rm_commands_generated(migration_result):
    result, _ = migration_result
    cmds = result.state_rm_commands()
    assert len(cmds) == 2
    for cmd in cmds:
        assert "terraform state rm" in cmd


def test_state_rm_commands_reference_source(migration_result):
    result, _ = migration_result
    cmds = result.state_rm_commands()
    combined = " ".join(cmds)
    assert "source.tfstate" in combined


# ---------------------------------------------------------------------------
# Migration: generated scripts
# ---------------------------------------------------------------------------


def test_cleanup_script_is_shell(migration_result):
    result, _ = migration_result
    script = result.cleanup_script()
    assert script.startswith("#!/usr/bin/env bash")


def test_cleanup_script_contains_rm_commands(migration_result):
    result, _ = migration_result
    script = result.cleanup_script()
    assert "terraform state rm" in script
    assert "aws_vpc.main" in script
    assert "aws_subnet.public" in script


def test_mv_script_is_shell(migration_result):
    result, _ = migration_result
    script = result.mv_script()
    assert script.startswith("#!/usr/bin/env bash")


def test_mv_script_contains_mv_commands(migration_result):
    result, _ = migration_result
    script = result.mv_script()
    assert "terraform state mv" in script


# ---------------------------------------------------------------------------
# Migration: overwrite guard
# ---------------------------------------------------------------------------


def test_overwrite_raises_if_dest_exists(migration_dirs, scan_result_mig):
    _, source_state, dest_state, _ = migration_dirs
    dest_state.write_text("{}")  # pre-create
    migrator = StateMigrator(
        scan_result=scan_result_mig,
        source_state_path=str(source_state),
        destination_state_path=str(dest_state),
        overwrite=False,
    )
    with pytest.raises(FileExistsError):
        migrator.migrate()


def test_overwrite_allowed_when_flag_set(migration_dirs, scan_result_mig):
    _, source_state, dest_state, _ = migration_dirs
    dest_state.write_text("{}")  # pre-create
    migrator = StateMigrator(
        scan_result=scan_result_mig,
        source_state_path=str(source_state),
        destination_state_path=str(dest_state),
        overwrite=True,
    )
    result = migrator.migrate()
    assert len(result.migrated) == 2


# ---------------------------------------------------------------------------
# Migration: dry run
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_file(migration_dirs, scan_result_mig):
    _, source_state, dest_state, _ = migration_dirs
    migrator = StateMigrator(
        scan_result=scan_result_mig,
        source_state_path=str(source_state),
        destination_state_path=str(dest_state),
    )
    result = migrator.dry_run()
    assert not dest_state.exists()
    assert len(result.migrated) == 2


def test_dry_run_validation_passes(migration_dirs, scan_result_mig):
    _, source_state, dest_state, _ = migration_dirs
    migrator = StateMigrator(
        scan_result=scan_result_mig,
        source_state_path=str(source_state),
        destination_state_path=str(dest_state),
    )
    result = migrator.dry_run()
    assert result.validation.passed is True


# ---------------------------------------------------------------------------
# Migration: summary smoke test
# ---------------------------------------------------------------------------


def test_summary_contains_key_info(migration_result):
    result, _ = migration_result
    s = result.summary()
    assert "aws_vpc.main" in s
    assert "aws_subnet.public" in s
    assert "PASSED" in s
