# -*- coding: utf-8 -*-
"""静态检查插件 __init__.py 无未定义名（AST 实现，不 import nonebot）。

起因：qcache/webfind 漏 import、handler 内 cfg 未定义两类 NameError 先后
上线（单测因不 import nonebot 全部漏网，只有线上触发才炸）。本检查近似
pyflakes：模块级 + 各函数（含嵌套）逐层收集绑定名，函数体内的 Name 载入
必须可解析（模块级/祖先函数/本函数/内建）。故意宽松：不校验导入名是否
真实存在、lambda 体不检查、装饰器不检查——只堵「漏 import/漏赋值」这一类。
"""
import ast
import builtins
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "src" / "plugins" / "booth_search" / "__init__.py"
SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(scope):
    """作用域自身节点：不进入嵌套函数/lambda 体内（其作用域另行检查），
    但嵌套 def 节点本身保留——它的名字是 enclosing 作用域的绑定。"""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        if isinstance(n, SCOPE):
            yield n
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _bound(nodes):
    names = set()
    for n in nodes:
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
    return names


class TestNoUndefinedNames(unittest.TestCase):
    def test_no_undefined_names(self):
        tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
        parent = {}
        for node in ast.walk(tree):
            for ch in ast.iter_child_nodes(node):
                parent[ch] = node

        # 模块级绑定只取模块自身节点（不吃函数内局部，否则跨函数泄漏成假阴性）
        base = _bound(_own_nodes(tree)) | set(dir(builtins))
        errors = []

        def ancestors(node):
            while node in parent:
                node = parent[node]
                yield node

        for sc in [n for n in ast.walk(tree) if isinstance(n, SCOPE)]:
            visible = set(base)
            for anc in ancestors(sc):
                if isinstance(anc, SCOPE):
                    visible |= _bound(_own_nodes(anc))
            visible |= _bound(_own_nodes(sc))
            for n in _own_nodes(sc):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                        and n.id not in visible:
                    errors.append(f"line {n.lineno}: 未定义名 {n.id!r}")

        for n in _own_nodes(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                    and n.id not in base:
                errors.append(f"模块级 line {n.lineno}: 未定义名 {n.id!r}")

        self.assertEqual(errors, [],
                         "存在未定义名（漏 import/漏赋值，线上会 NameError）:\n"
                         + "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
