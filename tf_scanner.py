"""
tf_scanner - Scan a directory of .tf source files and cross-reference with a state file.

This module provides :class:`TFScanner`, which:

1. Walks a directory collecting all ``*.tf`` files.
2. Parses each file with a best-effort regex-based HCL parser to extract:
   - ``resource`` blocks  →  :class:`~tfstate_parser.models.TFResource`
   - ``locals`` blocks    →  :class:`~tfstate_parser.models.LocalValue`
   - All ``local.xxx`` and resource-reference expressions within each block.
3. Cross-references found resources against a Terraform state file, producing
   :class:`~tfstate_parser.models.MatchedResource` objects that combine the
   source declaration with the deployed state entry.

Notes
-----
HCL is not fully parseable by Python regex alone (it allows nested blocks,
heredocs, complex expressions, etc.). This scanner handles the vast majority
of real-world .tf files accurately. For production use with very complex
configs, consider pairing this with the ``python-hcl2`` package — the scanner
will automatically use it when available, falling back to the regex engine.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Dict, IO, List, Optional, Tuple, Union

from .models import LocalValue, MatchedResource, ScanResult, TFResource
from .parser import TerraformStateParser

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# resource "aws_instance" "web" { ... }
_RESOURCE_BLOCK_RE = re.compile(
    r'(resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{',
    re.MULTILINE,
)

# locals { key = expr  key2 = expr2 }
_LOCALS_BLOCK_RE = re.compile(
    r'\blocals\s*\{',
    re.MULTILINE,
)

# local.foo  or  local.foo.bar
_LOCAL_REF_RE = re.compile(
    r'\blocal\.([a-zA-Z_][a-zA-Z0-9_]*)',
)

# resource references: type.name  or  module.x.type.name  or  data.type.name
# We deliberately exclude local.xxx from this pattern.
_RESOURCE_REF_RE = re.compile(
    r'\b(?:module\.[a-zA-Z0-9_\-]+\.)?(?:data\.)?'
    r'(?!local\b)([a-zA-Z][a-zA-Z0-9_]*)\.([a-zA-Z0-9_\-]+)\b',
)

# Matches single-line comments (# or //) and multi-line /* ... */
_COMMENT_RE = re.compile(
    r'#[^\n]*|//[^\n]*|/\*.*?\*/',
    re.DOTALL,
)

# Matches string literals so we don't misparse references inside them
# Simple approximation – handles most real-world cases.
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


class TFScanner:
    """
    Scan a directory of Terraform ``.tf`` source files and cross-reference
    the discovered resources with a Terraform state file.

    Parameters
    ----------
    tf_dir:
        Path to the directory containing ``.tf`` files.
    state_source:
        Path to a ``terraform.tfstate`` file, an open file object, or a
        pre-parsed dict.  Pass ``None`` to skip state correlation.
    recursive:
        If ``True``, scan subdirectories as well (default: ``False``).
    infer_state_dependencies:
        Forwarded to :class:`~tfstate_parser.parser.TerraformStateParser`.

    Example::

        scanner = TFScanner("./infra", "terraform.tfstate")
        result = scanner.scan()
        print(result.summary())

        for match in result.matched:
            print(match.address, match.local_dependencies, match.state_dependencies)
    """

    def __init__(
        self,
        tf_dir: Union[str, Path],
        state_source: Union[str, Path, IO[str], dict, None] = None,
        *,
        recursive: bool = False,
        infer_state_dependencies: bool = True,
    ) -> None:
        self._tf_dir = Path(tf_dir)
        self._state_source = state_source
        self._recursive = recursive
        self._infer = infer_state_dependencies

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> ScanResult:
        """
        Execute the full scan and return a :class:`~tfstate_parser.models.ScanResult`.

        Steps:
        1. Collect ``.tf`` files.
        2. Parse each file for ``resource`` and ``locals`` blocks.
        3. Load the state file (if provided).
        4. Correlate .tf resources with state entries.
        5. Return a complete :class:`~tfstate_parser.models.ScanResult`.
        """
        tf_files = self._collect_tf_files()
        tf_resources: Dict[str, TFResource] = {}
        locals_map: Dict[str, LocalValue] = {}

        for tf_file in tf_files:
            try:
                source = tf_file.read_text(encoding="utf-8")
            except OSError:
                continue
            file_resources, file_locals = self._parse_file(source, str(tf_file))
            for res in file_resources:
                tf_resources[res.address] = res
            for loc in file_locals:
                locals_map[loc.address] = loc

        # Load state
        state_map: Dict[str, object] = {}
        if self._state_source is not None:
            state_parser = TerraformStateParser(
                self._state_source,
                infer_dependencies=self._infer,
            )
            state_map = state_parser.get_resource_map()

        # Build matched resources
        matched: List[MatchedResource] = []
        matched_state_addresses: set = set()

        for address, tf_res in tf_resources.items():
            state_res = state_map.get(address)
            if state_res is not None:
                matched_state_addresses.add(address)
            matched.append(MatchedResource(tf_resource=tf_res, state_resource=state_res))

        # State resources with no .tf declaration
        unmatched_state = [
            res
            for addr, res in state_map.items()
            if addr not in matched_state_addresses
        ]

        return ScanResult(
            matched=matched,
            locals=locals_map,
            unmatched_state_resources=unmatched_state,
            scanned_files=[str(f) for f in tf_files],
        )

    # ------------------------------------------------------------------
    # File collection
    # ------------------------------------------------------------------

    def _collect_tf_files(self) -> List[Path]:
        if self._recursive:
            files = sorted(self._tf_dir.rglob("*.tf"))
        else:
            files = sorted(self._tf_dir.glob("*.tf"))
        return files

    # ------------------------------------------------------------------
    # HCL parsing
    # ------------------------------------------------------------------

    def _parse_file(
        self, source: str, filename: str
    ) -> Tuple[List[TFResource], List[LocalValue]]:
        """Parse a single .tf file and return (resources, locals)."""
        # Try python-hcl2 first if available
        try:
            return self._parse_with_hcl2(source, filename)
        except ImportError:
            pass
        except Exception:
            # hcl2 parse error — fall through to regex
            pass
        return self._parse_with_regex(source, filename)

    # ---- hcl2 fast path ---------------------------------------------------

    @staticmethod
    def _parse_with_hcl2(
        source: str, filename: str
    ) -> Tuple[List[TFResource], List[LocalValue]]:
        """Parse using python-hcl2 (must be installed separately)."""
        import hcl2  # type: ignore
        import io

        data = hcl2.load(io.StringIO(source))
        resources: List[TFResource] = []
        locals_out: List[LocalValue] = []

        for resource_block in data.get("resource", []):
            for rtype, names in resource_block.items():
                for rname, body in names.items():
                    body_str = str(body)
                    addr = f"{rtype}.{rname}"
                    res = TFResource(
                        resource_type=rtype,
                        name=rname,
                        mode="managed",
                        source_file=filename,
                        raw_block=body_str,
                        resource_dependencies=_extract_resource_refs(body_str, addr),
                        local_dependencies=_extract_local_refs(body_str),
                    )
                    resources.append(res)

        for data_block in data.get("data", []):
            for rtype, names in data_block.items():
                for rname, body in names.items():
                    body_str = str(body)
                    addr = f"data.{rtype}.{rname}"
                    res = TFResource(
                        resource_type=rtype,
                        name=rname,
                        mode="data",
                        source_file=filename,
                        raw_block=body_str,
                        resource_dependencies=_extract_resource_refs(body_str, addr),
                        local_dependencies=_extract_local_refs(body_str),
                    )
                    resources.append(res)

        for locals_block in data.get("locals", []):
            for lname, expr in locals_block.items():
                expr_str = str(expr)
                loc = LocalValue(
                    name=lname,
                    expression=expr_str,
                    source_file=filename,
                    references=_extract_local_refs(expr_str) + _extract_resource_refs(expr_str, f"local.{lname}"),
                )
                locals_out.append(loc)

        return resources, locals_out

    # ---- regex fallback ---------------------------------------------------

    def _parse_with_regex(
        self, source: str, filename: str
    ) -> Tuple[List[TFResource], List[LocalValue]]:
        """Best-effort regex-based HCL parser."""
        # Strip comments before parsing
        clean = _strip_comments(source)

        resources = self._extract_resource_blocks(clean, filename)
        locals_out = self._extract_locals_blocks(clean, filename)
        return resources, locals_out

    @staticmethod
    def _extract_resource_blocks(source: str, filename: str) -> List[TFResource]:
        resources: List[TFResource] = []
        for match in _RESOURCE_BLOCK_RE.finditer(source):
            mode = match.group(1)   # "resource" or "data"
            rtype = match.group(2)
            rname = match.group(3)
            block_body = _extract_block_body(source, match.end() - 1)
            # Canonical address mirrors Terraform conventions
            if mode == "data":
                address = f"data.{rtype}.{rname}"
            else:
                address = f"{rtype}.{rname}"
            res = TFResource(
                resource_type=rtype,
                name=rname,
                mode=mode if mode == "data" else "managed",
                source_file=filename,
                raw_block=block_body,
                resource_dependencies=_extract_resource_refs(block_body, address),
                local_dependencies=_extract_local_refs(block_body),
            )
            resources.append(res)
        return resources

    @staticmethod
    def _extract_locals_blocks(source: str, filename: str) -> List[LocalValue]:
        locals_out: List[LocalValue] = []
        for match in _LOCALS_BLOCK_RE.finditer(source):
            block_body = _extract_block_body(source, match.end() - 1)
            # Each assignment inside locals: name = expr (up to next assignment or end)
            entries = _split_locals_assignments(block_body)
            for lname, expr in entries:
                loc = LocalValue(
                    name=lname,
                    expression=expr.strip(),
                    source_file=filename,
                    references=(
                        _extract_local_refs(expr)
                        + _extract_resource_refs(expr, f"local.{lname}")
                    ),
                )
                locals_out.append(loc)
        return locals_out


# ---------------------------------------------------------------------------
# Helper utilities (module-level so they can be reused by both parsers)
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Remove HCL/Terraform comments from source text."""
    return _COMMENT_RE.sub("", text)


def _extract_block_body(source: str, open_brace_pos: int) -> str:
    """
    Given the position of an opening ``{``, find its matching closing ``}``
    and return everything in between (supporting nested braces).
    """
    depth = 0
    i = open_brace_pos
    start = None
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
            if start is None:
                start = i + 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i]
        i += 1
    # Unclosed block — return what we have
    return source[start:] if start is not None else ""


def _extract_local_refs(text: str) -> List[str]:
    """Return sorted unique local.xxx addresses found in text."""
    return sorted({f"local.{m.group(1)}" for m in _LOCAL_REF_RE.finditer(text)})


def _extract_resource_refs(text: str, self_address: str) -> List[str]:
    """
    Return sorted unique resource addresses found in text, excluding the
    resource's own address and local.xxx references.

    Handles both ``type.name`` and ``data.type.name`` patterns.
    """
    found: set = set()

    # Match data.type.name references specifically first
    data_ref_re = re.compile(
        r'\bdata\.([a-zA-Z][a-zA-Z0-9_]*)\.([a-zA-Z0-9_\-]+)\b'
    )
    for m in data_ref_re.finditer(text):
        addr = f"data.{m.group(1)}.{m.group(2)}"
        if addr != self_address:
            found.add(addr)

    # Match module.x.type.name references
    module_ref_re = re.compile(
        r'\bmodule\.([a-zA-Z0-9_\-]+)\.([a-zA-Z][a-zA-Z0-9_]*)\.([a-zA-Z0-9_\-]+)\b'
    )
    for m in module_ref_re.finditer(text):
        addr = f"module.{m.group(1)}.{m.group(2)}.{m.group(3)}"
        if addr != self_address:
            found.add(addr)

    # Match plain type.name references (not preceded by data. or module.)
    for m in _RESOURCE_REF_RE.finditer(text):
        rtype = m.group(1)
        rname = m.group(2)
        if rtype in _HCL_KEYWORDS:
            continue
        addr = f"{rtype}.{rname}"
        if addr != self_address:
            # Only add if a data.type.name version isn't already captured
            data_version = f"data.{rtype}.{rname}"
            if data_version not in found:
                found.add(addr)

    return sorted(found)


def _split_locals_assignments(block: str) -> List[Tuple[str, str]]:
    """
    Split the body of a ``locals { }`` block into (name, expression) pairs.

    Handles:
    - Simple scalar assignments: ``name = "value"``
    - Map/object assignments: ``name = { ... }``
    - List assignments: ``name = [ ... ]``
    - Multi-line expressions with nested brackets
    """
    entries: List[Tuple[str, str]] = []
    # Match each top-level assignment: identifier = <expr>
    # We use a simple state machine rather than a complex regex.
    i = 0
    block = block.strip()

    assign_re = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*', re.MULTILINE)

    while i < len(block):
        m = assign_re.search(block, i)
        if not m:
            break
        lname = m.group(1)
        expr_start = m.end()
        expr, expr_end = _consume_expression(block, expr_start)
        entries.append((lname, expr))
        i = expr_end

    return entries


def _consume_expression(text: str, start: int) -> Tuple[str, int]:
    """
    Consume a complete HCL expression starting at ``start``.
    Returns (expression_text, end_index).
    Handles nested {}, [], () and quoted strings.
    """
    depth_brace = 0
    depth_bracket = 0
    depth_paren = 0
    in_string = False
    escape_next = False
    i = start

    while i < len(text):
        ch = text[i]

        if escape_next:
            escape_next = False
            i += 1
            continue

        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            if depth_brace > 0:
                depth_brace -= 1
            else:
                # End of the enclosing locals block
                return text[start:i].strip(), i
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            if depth_bracket > 0:
                depth_bracket -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            if depth_paren > 0:
                depth_paren -= 1
        elif ch == "\n" and depth_brace == 0 and depth_bracket == 0 and depth_paren == 0:
            # End of a simple single-line expression
            expr = text[start:i].strip()
            if expr:
                return expr, i + 1

        i += 1

    return text[start:i].strip(), i


# HCL built-in identifiers that look like resource type references but aren't
_HCL_KEYWORDS = frozenset({
    "var", "local", "module", "data", "path", "terraform",
    "each", "count", "self", "true", "false", "null",
    "for", "if", "in", "toset", "tolist", "tomap",
    "lookup", "merge", "concat", "length", "keys", "values",
    "contains", "element", "flatten", "distinct", "compact",
    "try", "can", "one", "range", "format", "formatlist",
    "join", "split", "replace", "regex", "regexall",
    "substr", "trimspace", "upper", "lower",
    "file", "templatefile", "jsonencode", "jsondecode",
    "yamlencode", "yamldecode", "base64encode", "base64decode",
    "md5", "sha256", "uuid", "timestamp",
    "max", "min", "abs", "ceil", "floor", "log", "pow", "signum",
    "cidrhost", "cidrnetmask", "cidrsubnet", "cidrsubnets",
    "provider", "resource",
})
