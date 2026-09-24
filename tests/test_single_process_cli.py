"""Production CLI entry points must never launch child processes."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (ROOT / "src" / "evm_inventory", ROOT / "scripts")
FORBIDDEN_MODULES = {"subprocess", "multiprocessing", "pty", "asyncio.subprocess"}
FORBIDDEN_OS_CALLS = {
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
}
FORBIDDEN_ASYNCIO_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}


def _is_process_call(node: ast.Call, aliases: dict[str, str]) -> bool:
    target = node.func
    if isinstance(target, ast.Name):
        name = aliases.get(target.id, "")
    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        module = aliases.get(target.value.id, "")
        name = f"{module}.{target.attr}"
    elif (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute)
          and isinstance(target.value.value, ast.Name)):
        module = aliases.get(target.value.value.id, "")
        name = f"{module}.{target.value.attr}.{target.attr}"
    else:
        return False
    if name.startswith("os.exec") or name.startswith("os.spawn"):
        return True
    return name in (
        {f"os.{method}" for method in FORBIDDEN_OS_CALLS}
        | {f"asyncio.{method}" for method in FORBIDDEN_ASYNCIO_CALLS}
        | {"concurrent.futures.ProcessPoolExecutor"}
    )


def test_production_cli_code_does_not_create_child_processes():
    violations = []
    for directory in SOURCE_DIRS:
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for item in node.names:
                        aliases[item.asname or item.name.split(".")[0]] = (
                            item.name if item.asname else item.name.split(".")[0]
                        )
                        if item.name in FORBIDDEN_MODULES:
                            violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: "
                                              f"import {item.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for item in node.names:
                        aliases[item.asname or item.name] = f"{module}.{item.name}"
                        if (module in FORBIDDEN_MODULES
                                or module == "os" and (
                                    item.name in FORBIDDEN_OS_CALLS
                                    or item.name.startswith(("exec", "spawn"))
                                )
                                or module == "asyncio"
                                and item.name in FORBIDDEN_ASYNCIO_CALLS
                                or module == "concurrent.futures"
                                and item.name == "ProcessPoolExecutor"):
                            violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: "
                                              f"from {module} import {item.name}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_process_call(node, aliases):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: child process")
    assert not violations, "Production CLI may not create child processes:\n" + "\n".join(
        violations
    )
