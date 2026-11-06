"""静态检查代码中容易踩的 Odoo 坑（不需要 Odoo）。

运行：python3 tools/check_code.py

1. 调用翻译函数 _() 的函数里，不能有名为 user、uid 的局部变量：
   在定时任务等取不到语言的场合，_() 会把调用处的 user / uid 当成用户编号转成数字，
   变量是别的东西（例如考勤机用户）就会报错，整个同步失败。
"""
import ast
import glob
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'kairlyn_zkteco_attendance')
FORBIDDEN = {'user', 'uid'}


def local_names(func):
    """函数自身作用域里绑定的名字（参数、赋值、for、with、except），不进入嵌套函数和类。"""
    names = {a.arg for a in func.args.args + func.args.kwonlyargs + func.args.posonlyargs}
    if func.args.vararg:
        names.add(func.args.vararg.arg)
    if func.args.kwarg:
        names.add(func.args.kwarg.arg)
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names


def calls_translate(func):
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_':
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


problems = []
for path in glob.glob(os.path.join(ROOT, '**', '*.py'), recursive=True):
    if os.sep + 'lib' + os.sep in path:
        continue
    tree = ast.parse(open(path, encoding='utf-8').read())
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and calls_translate(func):
            bad = local_names(func) & FORBIDDEN
            if bad:
                problems.append(f'{os.path.relpath(path, ROOT)}:{func.lineno} {func.name}() 调用了 _()，'
                                f'但有局部变量 {", ".join(sorted(bad))}')

for problem in problems:
    print('FAIL', problem)
print('问题数:', len(problems))
sys.exit(1 if problems else 0)
