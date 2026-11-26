"""Structural guard run against source and the packaged backend."""
import ast
from pathlib import Path

FORBIDDEN_METHODS = {'build_request', 'compact', 'delivery', 'pending', 'before_turn',
                     'after_response', 'tool_definitions', 'observe_model_event'}


def verify_single_runtime(package):
    package = Path(package)
    engines, violations = [], []
    for path in package.rglob('*.py'):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative = path.relative_to(package)
        desktop = relative.parts[0] == 'desktop'
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name.endswith('Engine'):
                    engines.append((str(relative), node.name))
                if any('Engine' in ast.unparse(base) for base in node.bases):
                    violations.append(f'{relative}: execution subclass {node.name}')
                if desktop:
                    for method in node.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in FORBIDDEN_METHODS:
                            violations.append(f'{relative}: desktop execution policy {method.name}')
            if isinstance(node, ast.ImportFrom) and not desktop:
                if node.module and ('goalkeeper.desktop' in node.module or node.module.startswith('desktop')):
                    violations.append(f'{relative}: core imports desktop {node.module}')
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Name) and value.id == 'Engine':
                    violations.append(f'{relative}: execution class alias')
            if desktop and isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name.endswith(('ModelRequest', 'Engine')) or name in {'task_profile', 'reasoning_effort'}:
                    violations.append(f'{relative}: desktop chooses execution strategy {name}')
    if engines != [('engine.py', 'Engine')]:
        violations.append('expected only engine.py:Engine, found ' + repr(engines))
    if violations:
        raise ValueError('\n'.join(violations))
    return {'passed': True, 'engine': 'goalkeeper.engine.Engine', 'source_files': len(list(package.rglob('*.py')))}
