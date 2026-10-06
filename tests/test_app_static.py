"""Static guard for src/main.py: every Flet call must match the installed Flet's real signatures.

Device-only code paths (Android share sheet, save dialog, streaming video) can't run in CI, so a wrong
keyword argument or a misspelled enum would only show up on a phone. This walks the UI source and checks, against
the installed flet / flet_video packages:
  * every call rooted at ft.* / fv.* (including static/class methods like ShareFile.from_path):
    keyword names and positional-argument count
  * every attribute chain such as ft.Icons.SHARE or ft.FontWeight.W_600 actually exists
  * method calls on the UI's service/page attributes (self.share.*, self.picker.*, self.page.*)
"""
import ast
import inspect
import os

import importlib

import flet as ft
import flet_video as fv

from core.service import DownloadManager, Library

SRC = os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py')
ROOTS = {'ft': ft, 'fv': fv}
# self.<attr> -> class whose methods are being called
SELF_ATTRS = {'share': ft.Share, 'picker': ft.FilePicker, 'page': ft.Page, 'lib': Library, 'manager': DownloadManager}


def chain(node):
    """ft.Icons.SHARE -> ['ft', 'Icons', 'SHARE']; None if the expression isn't a plain dotted name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    return None


def resolve(parts):
    obj = ROOTS[parts[0]]
    for p in parts[1:]:
        obj = getattr(obj, p)
    return obj


def check_signature(obj, call, where, problems):
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return
    params = sig.parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        accepts_any_kw = True
    else:
        accepts_any_kw = False
    names = {n for n, p in params.items() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    if inspect.isclass(obj) or not inspect.ismethod(obj):
        names.discard('self')
    for kw in call.keywords:
        if kw.arg is not None and kw.arg not in names and not accepts_any_kw:
            problems.append(f'{where}: unexpected keyword {kw.arg!r} for {getattr(obj, "__qualname__", obj)}{sig}')
    positional = [p for n, p in params.items() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and n != 'self']
    has_varargs = any(p.kind is p.VAR_POSITIONAL for p in params.values())
    if not has_varargs and len(call.args) > len(positional) and not any(isinstance(a, ast.Starred) for a in call.args):
        problems.append(f'{where}: too many positional args ({len(call.args)}) for {getattr(obj, "__qualname__", obj)}{sig}')


def audit():
    tree = ast.parse(open(SRC, encoding='utf-8').read())
    problems, calls, attrs = [], 0, 0
    # names imported from our own core package (add_course, Site, DownloadManager, ...): check calls to them too
    core_names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('core'):
            mod = importlib.import_module(node.module)
            for a in node.names:
                core_names[a.asname or a.name] = getattr(mod, a.name)
    for node in ast.walk(tree):
        where = f'main.py:{getattr(node, "lineno", "?")}'
        if isinstance(node, ast.Attribute):
            parts = chain(node)
            if parts and parts[0] in ROOTS and isinstance(node.ctx, ast.Load):
                attrs += 1
                try:
                    resolve(parts)
                except AttributeError:
                    problems.append(f'{where}: {".".join(parts)} does not exist')
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in core_names:
                calls += 1
                check_signature(core_names[node.func.id], node, where, problems)
                continue
            parts = chain(node.func)
            if not parts:
                continue
            if parts[0] in ROOTS:
                try:
                    obj = resolve(parts)
                except AttributeError:
                    continue  # reported by the attribute check
                calls += 1
                check_signature(obj, node, where, problems)
            elif parts[0] == 'self' and len(parts) == 3 and parts[1] in SELF_ATTRS:
                cls = SELF_ATTRS[parts[1]]
                method = getattr(cls, parts[2], None)
                if method is None:
                    problems.append(f'{where}: {cls.__name__} has no method {parts[2]!r}')
                else:
                    calls += 1
                    check_signature(method, node, where, problems)
    return problems, calls, attrs


def test_flet_calls_match_installed_signatures():
    problems, calls, attrs = audit()
    assert calls > 100 and attrs > 100, 'audit did not look at the UI code'  # sanity: it really scanned main.py
    assert not problems, '\n' + '\n'.join(problems)
