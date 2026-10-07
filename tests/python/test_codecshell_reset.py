import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


SCRIPT = Path(__file__).resolve().parents[2] / 'proxyclient/tools/codecshell.py'


def reset_block():
    tree = ast.parse(SCRIPT.read_text())
    block = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.If)
                 and isinstance(node.test, (ast.Compare, ast.BoolOp))
                 and '"function-reset"' in ast.unparse(node.test).replace("'", '"'))
    return compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])),
                   str(SCRIPT), 'exec')


def test_no_reset_leaves_gpio_untouched():
    proxy = Mock()
    node = SimpleNamespace(name='audio-codec-output',
                           _properties={'function-reset': True})
    # No reset function or GPIO controller is needed when reset is disabled.
    exec(reset_block(), {'args': SimpleNamespace(no_reset=True),
                         'devnode': node, 'gpios': {}, 'p': proxy})
    proxy.mask32.assert_not_called()


def test_default_reset_sequence_is_preserved():
    proxy = Mock()
    gpio = Mock()
    gpio.get_reg.return_value = (0x230000000, 0x1000)
    node = SimpleNamespace(name='audio-codec-output',
                           _properties={'function-reset': True},
                           function_reset=SimpleNamespace(phandle=250, args=[0x3a]))
    exec(reset_block(), {'args': SimpleNamespace(no_reset=False),
                         'devnode': node, 'gpios': {250: gpio}, 'p': proxy})
    calls = [call.args for call in proxy.mask32.call_args_list]
    assert calls == [(0x2300000e8, 1, 0), (0x2300000e8, 1, 1)]
