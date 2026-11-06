"""静态检查视图：字段、按钮方法是否在对应模型中存在（不需要 Odoo）。

运行：python3 tools/check_views.py
只检查本模块定义或继承的模型；继承 Odoo 原生模型时，原生字段放在 CORE_FIELDS 里。
"""
import ast
import glob
import os
import sys
import xml.etree.ElementTree as ET

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'kairlyn_zkteco_attendance')
# 本模块视图里用到的 Odoo 原生字段
CORE_FIELDS = {
    'hr.attendance': {'check_in', 'check_out', 'in_mode', 'out_mode', 'employee_id', 'date'},
    'hr.employee': {'name'},
    'res.config.settings': set(),
}
MAGIC = {'id', 'display_name', 'create_date', 'create_uid', 'write_date', 'write_uid',
         'message_ids', 'message_follower_ids', 'activity_ids'}


def load_models():
    fields, methods, comodels = {}, {}, {}
    for path in glob.glob(os.path.join(ROOT, '**', '*.py'), recursive=True):
        tree = ast.parse(open(path, encoding='utf-8').read())
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            name = None
            for stmt in cls.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    target = stmt.targets[0]
                    if getattr(target, 'id', '') == '_name':
                        name = stmt.value.value
                    elif getattr(target, 'id', '') == '_inherit' and not name:
                        name = stmt.value.value
            if not name:
                continue
            model_fields = fields.setdefault(name, set(CORE_FIELDS.get(name, set())) | MAGIC)
            model_methods = methods.setdefault(name, set())
            for stmt in cls.body:
                if isinstance(stmt, ast.FunctionDef):
                    model_methods.add(stmt.name)
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                    func = stmt.value.func
                    if isinstance(func, ast.Attribute) and getattr(func.value, 'id', '') == 'fields':
                        field_name = stmt.targets[0].id
                        model_fields.add(field_name)
                        args = stmt.value.args
                        if func.attr in ('Many2one', 'One2many', 'Many2many') and args \
                                and isinstance(args[0], ast.Constant):
                            comodels[(name, field_name)] = args[0].value
    # 设置页的 related 字段也是 res.config.settings 的字段
    return fields, methods, comodels


def check():
    fields, methods, comodels = load_models()
    problems = []

    def walk(node, model, path):
        for child in node:
            if child.tag == 'field' and child.get('name'):
                name = child.get('name')
                if model in fields and name not in fields[model]:
                    problems.append(f'{path}: 模型 {model} 没有字段 {name}')
                sub_model = comodels.get((model, name))
                if sub_model:
                    walk(child, sub_model, path)
                continue
            if child.tag == 'button' and child.get('type') == 'object':
                if model in methods and child.get('name') not in methods[model]:
                    problems.append(f'{path}: 模型 {model} 没有方法 {child.get("name")}')
            walk(child, model, path)

    for path in glob.glob(os.path.join(ROOT, '**', '*.xml'), recursive=True):
        rel = os.path.relpath(path, ROOT)
        for record in ET.parse(path).getroot().iter('record'):
            if record.get('model') != 'ir.ui.view':
                continue
            model_el = record.find("field[@name='model']")
            arch = record.find("field[@name='arch']")
            if model_el is None or arch is None:
                continue
            model = model_el.text
            if model not in fields:
                problems.append(f'{rel}: 本模块没有定义或继承模型 {model}')
                continue
            walk(arch, model, rel + ' ' + record.get('id'))
    return problems


problems = check()
for problem in problems:
    print('FAIL', problem)
print('问题数:', len(problems))
sys.exit(1 if problems else 0)
