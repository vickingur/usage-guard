import re, unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parent.parent / "hooks" / "register.tsx").read_text()

# A helper handed `$` must be declared once in the module. Claude Code before
# 2.1.29x refuses to load a module that reuses such a name, even as a parameter
# elsewhere, and Paperclip's agents run one of those releases.
HELPER = re.compile(r"(?:const\s+(\w+)\s*=\s*(?:async\s*)?|function\s+(\w+)\s*)\(\$\s*:")


def declarations(name):
    local = re.findall(rf"\b(?:const|let|var|function)\s+{name}\b", SOURCE)
    param = re.findall(rf"\(\s*(?:[^()]*?,\s*)?{name}\s*\??:\s*[A-Za-z]", SOURCE)
    return len(local) + len(param)


class ModuleSourceTest(unittest.TestCase):
    def test_helpers_handed_dollar_are_declared_once(self):
        names = {a or b for a, b in HELPER.findall(SOURCE)}
        self.assertIn("seconds", names)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertEqual(declarations(name), 1)


if __name__ == "__main__":
    unittest.main()
