"""Restricted DataFrame execution behind an independent service boundary."""

from __future__ import annotations

import ast
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, Mapping, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
import requests


MAX_SANDBOX_CODE_CHARACTERS = 12_000
MAX_SANDBOX_DATASETS = 8
MAX_SANDBOX_INPUT_ROWS = 500_000
MAX_SANDBOX_OUTPUT_ROWS = 100_000
MAX_SANDBOX_OUTPUT_COLUMNS = 256
MAX_SANDBOX_REQUEST_BYTES = 24 * 1024 * 1024
SANDBOX_TIMEOUT_SECONDS = 30
SANDBOX_CPU_SECONDS = 20
SANDBOX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
_ALLOWED_CALL_NAMES = frozenset(_SAFE_BUILTINS)
_ALLOWED_PANDAS_CALLS = frozenset(
    {
        "DataFrame",
        "Series",
        "concat",
        "crosstab",
        "cut",
        "isna",
        "merge",
        "notna",
        "qcut",
        "to_datetime",
        "to_numeric",
    }
)
_ALLOWED_NUMPY_CALLS = frozenset(
    {
        "abs",
        "exp",
        "isfinite",
        "isnan",
        "log",
        "log1p",
        "maximum",
        "minimum",
        "round",
        "select",
        "sqrt",
        "where",
    }
)
_ALLOWED_ATTRIBUTES = frozenset(
    {
        "T",
        "abs",
        "add",
        "agg",
        "aggregate",
        "apply",
        "assign",
        "astype",
        "at",
        "between",
        "cat",
        "clip",
        "columns",
        "contains",
        "copy",
        "count",
        "cummax",
        "cummin",
        "cumprod",
        "cumsum",
        "day",
        "diff",
        "div",
        "drop",
        "drop_duplicates",
        "dropna",
        "dtypes",
        "dt",
        "duplicated",
        "empty",
        "endswith",
        "eq",
        "expanding",
        "explode",
        "fillna",
        "floordiv",
        "ge",
        "groupby",
        "gt",
        "head",
        "iat",
        "iloc",
        "index",
        "isin",
        "join",
        "le",
        "loc",
        "lower",
        "lt",
        "map",
        "max",
        "mean",
        "median",
        "melt",
        "merge",
        "min",
        "month",
        "mul",
        "name",
        "ne",
        "nlargest",
        "notna",
        "nsmallest",
        "nunique",
        "pct_change",
        "pivot",
        "pivot_table",
        "pow",
        "prod",
        "quantile",
        "rank",
        "rename",
        "rename_axis",
        "replace",
        "reset_index",
        "rolling",
        "round",
        "set_index",
        "shape",
        "shift",
        "size",
        "sort_index",
        "sort_values",
        "stack",
        "startswith",
        "std",
        "str",
        "strip",
        "sub",
        "sum",
        "tail",
        "to_frame",
        "transform",
        "truediv",
        "unique",
        "unstack",
        "upper",
        "value_counts",
        "where",
        "year",
    }
)
_ALLOWED_NODES = (
    ast.Module,
    ast.Assign,
    ast.AugAssign,
    ast.Expr,
    ast.If,
    ast.For,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.keyword,
    ast.IfExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)


class SandboxValidationError(ValueError):
    """Raised when a DataFrame program exceeds the restricted execution contract."""


class SandboxDataset(BaseModel):
    """One named serialized DataFrame supplied to the isolated runner."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(
        min_length=1,
        description="Stable identifier exposed to the DataFrame program.",
    )
    frame: str = Field(
        min_length=1,
        description="Pandas split-orient JSON payload for the complete dataset.",
    )


class SandboxRequest(BaseModel):
    """Validated request accepted by the secretless sandbox service."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        min_length=1,
        max_length=MAX_SANDBOX_CODE_CHARACTERS,
        description="Restricted DataFrame program that assigns its table to result.",
    )
    datasets: list[SandboxDataset] = Field(
        min_length=1,
        max_length=MAX_SANDBOX_DATASETS,
        description="Complete input datasets available through the datasets mapping.",
    )


class SandboxResponse(BaseModel):
    """Serialized result returned by the isolated runner."""

    model_config = ConfigDict(extra="forbid")

    frame: str = Field(
        min_length=1,
        description="Pandas split-orient JSON payload for the validated result table.",
    )


class RestrictedDataFrameRunner:
    """Execute validated DataFrame code in a bounded child process."""

    def run(self, request: SandboxRequest) -> SandboxResponse:
        """Return the program's ``result`` DataFrame after resource checks."""
        _validate_program(request.code)
        frames = {
            dataset.dataset_id: pd.read_json(
                StringIO(dataset.frame),
                orient="split",
            )
            for dataset in request.datasets
        }
        if len(frames) != len(request.datasets):
            raise SandboxValidationError("Sandbox dataset identifiers must be unique.")
        total_rows = sum(len(frame) for frame in frames.values())
        if total_rows > MAX_SANDBOX_INPUT_ROWS:
            raise SandboxValidationError(
                f"Sandbox input exceeds {MAX_SANDBOX_INPUT_ROWS} rows."
            )
        payload = {
            "code": request.code,
            "datasets": {
                name: frame.to_json(orient="split", date_format="iso")
                for name, frame in frames.items()
            },
        }
        with tempfile.TemporaryDirectory(prefix="research-sandbox-") as directory:
            directory_path = Path(directory)
            input_path = directory_path / "input.json"
            output_path = directory_path / "output.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            environment = {
                "LANG": "C.UTF-8",
                "PATH": os.environ.get("PATH", ""),
                "PYTHONIOENCODING": "utf-8",
            }
            try:
                process = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        str(input_path),
                        str(output_path),
                    ],
                    cwd=directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=SANDBOX_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxValidationError(
                    f"DataFrame program exceeded {SANDBOX_TIMEOUT_SECONDS} seconds."
                ) from exc
            if process.returncode != 0:
                detail = process.stderr.strip().splitlines()[-1:]
                message = detail[0] if detail else "Sandbox process failed."
                raise SandboxValidationError(message[:500])
            try:
                response = SandboxResponse.model_validate_json(
                    output_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise SandboxValidationError(
                    "Sandbox returned an invalid DataFrame contract."
                ) from exc
        frame = pd.read_json(StringIO(response.frame), orient="split")
        _validate_output_frame(frame)
        return response


class RemotePythonSandbox:
    """Call a private, secretless research sandbox over authenticated HTTP."""

    def __init__(
        self,
        url: str,
        *,
        session: Optional[requests.Session] = None,
        identity_token_provider: Optional[Callable[[str], str]] = None,
    ) -> None:
        """Store the private service URL and injectable authentication transport."""
        normalized_url = url.strip().rstrip("/")
        if not normalized_url:
            raise ValueError("Research sandbox URL is required.")
        self._url = normalized_url
        self._session = session or requests.Session()
        self._identity_token_provider = (
            identity_token_provider or _fetch_google_identity_token
        )

    def run(self, code: str, datasets: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
        """Return one validated result table from the isolated sandbox."""
        request = SandboxRequest(
            code=code,
            datasets=[
                SandboxDataset(
                    dataset_id=dataset_id,
                    frame=frame.to_json(orient="split", date_format="iso"),
                )
                for dataset_id, frame in datasets.items()
            ],
        )
        encoded = request.model_dump_json().encode("utf-8")
        if len(encoded) > MAX_SANDBOX_REQUEST_BYTES:
            raise SandboxValidationError(
                f"Sandbox request exceeds {MAX_SANDBOX_REQUEST_BYTES} bytes."
            )
        token = self._identity_token_provider(self._url)
        response = self._session.post(
            f"{self._url}/v1/execute",
            data=encoded,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=SANDBOX_TIMEOUT_SECONDS + 10,
        )
        if response.status_code >= 400:
            raise SandboxValidationError(
                f"Research sandbox returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            sandbox_response = SandboxResponse.model_validate(response.json())
            frame = pd.read_json(StringIO(sandbox_response.frame), orient="split")
        except (TypeError, ValueError) as exc:
            raise SandboxValidationError(
                "Research sandbox returned an invalid response contract."
            ) from exc
        _validate_output_frame(frame)
        return frame


def _fetch_google_identity_token(audience: str) -> str:
    """Return a Google-signed identity token for one private Cloud Run service."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)


def _validate_program(code: str) -> None:
    """Reject syntax and capabilities outside the DataFrame-only contract."""
    if not code.strip():
        raise SandboxValidationError("DataFrame program is empty.")
    if len(code) > MAX_SANDBOX_CODE_CHARACTERS:
        raise SandboxValidationError(
            f"DataFrame program exceeds {MAX_SANDBOX_CODE_CHARACTERS} characters."
        )
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxValidationError(f"Invalid DataFrame program: {exc.msg}") from exc
    assigned_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AugAssign))
        for target in _assignment_targets(node)
        if isinstance(target, ast.Name)
    }
    if "result" not in assigned_names:
        raise SandboxValidationError(
            "DataFrame program must assign the final table to result."
        )
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SandboxValidationError(
                f"Unsupported Python syntax: {type(node).__name__}."
            )
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise SandboxValidationError("Private Python names are not allowed.")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr not in _ALLOWED_ATTRIBUTES:
                raise SandboxValidationError(
                    f"Unsupported DataFrame attribute: {node.attr}."
                )
        if isinstance(node, ast.Call):
            _validate_call(node)


def _assignment_targets(node: Any) -> list[ast.expr]:
    """Return assignment targets for supported assignment nodes."""
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AugAssign):
        return [node.target]
    return []


def _validate_call(node: ast.Call) -> None:
    """Allow only bounded builtins and DataFrame-oriented calls."""
    if isinstance(node.func, ast.Name):
        if node.func.id not in _ALLOWED_CALL_NAMES:
            raise SandboxValidationError(
                f"Unsupported Python call: {node.func.id}."
            )
        return
    if not isinstance(node.func, ast.Attribute):
        raise SandboxValidationError("Dynamic Python calls are not allowed.")
    root, attribute = _attribute_root(node.func)
    if root == "pd" and attribute in _ALLOWED_PANDAS_CALLS:
        return
    if root == "np" and attribute in _ALLOWED_NUMPY_CALLS:
        return
    if node.func.attr in _ALLOWED_ATTRIBUTES:
        return
    raise SandboxValidationError(f"Unsupported DataFrame call: {node.func.attr}.")


def _attribute_root(node: ast.Attribute) -> tuple[str, str]:
    """Return the root name and first attribute of one attribute chain."""
    attributes = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        attributes.append(value.attr)
        value = value.value
    root = value.id if isinstance(value, ast.Name) else ""
    return root, attributes[-1]


def _validate_output_frame(frame: pd.DataFrame) -> None:
    """Enforce bounded, unambiguous tabular output."""
    if len(frame) > MAX_SANDBOX_OUTPUT_ROWS:
        raise SandboxValidationError(
            f"Sandbox output exceeds {MAX_SANDBOX_OUTPUT_ROWS} rows."
        )
    if len(frame.columns) > MAX_SANDBOX_OUTPUT_COLUMNS:
        raise SandboxValidationError(
            f"Sandbox output exceeds {MAX_SANDBOX_OUTPUT_COLUMNS} columns."
        )
    if not frame.columns.is_unique:
        raise SandboxValidationError("Sandbox output contains duplicate columns.")
    if any(not str(column).strip() for column in frame.columns):
        raise SandboxValidationError("Sandbox output contains an empty column name.")


def _apply_resource_limits() -> None:
    """Bound CPU, address space, and generated file size in the child process."""
    try:
        import resource
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_CPU_SECONDS, SANDBOX_CPU_SECONDS))
    # Darwin reserves a large virtual address space for scientific libraries;
    # Linux production workers can enforce a meaningful address-space ceiling.
    if sys.platform.startswith("linux") and hasattr(resource, "RLIMIT_AS"):
        resource.setrlimit(
            resource.RLIMIT_AS,
            (SANDBOX_MEMORY_BYTES, SANDBOX_MEMORY_BYTES),
        )
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))


def _worker_main(input_path: Path, output_path: Path) -> None:
    """Execute one isolated DataFrame program for the sandbox service."""
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    code = str(payload["code"])
    _validate_program(code)
    datasets = {
        name: pd.read_json(StringIO(serialized), orient="split")
        for name, serialized in dict(payload["datasets"]).items()
    }
    _apply_resource_limits()
    namespace: Dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "datasets": datasets,
        "np": np,
        "pd": pd,
    }
    exec(compile(code, "<research-sandbox>", "exec"), namespace, namespace)
    result = namespace.get("result")
    if isinstance(result, pd.Series):
        result = result.to_frame()
    if not isinstance(result, pd.DataFrame):
        raise SandboxValidationError("DataFrame program result must be a table.")
    _validate_output_frame(result)
    output_path.write_text(
        SandboxResponse(
            frame=result.to_json(orient="split", date_format="iso")
        ).model_dump_json(),
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Expected input and output paths.")
    _worker_main(Path(sys.argv[1]), Path(sys.argv[2]))
