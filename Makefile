.PHONY: check test package
OUTPUT ?= .local/packages

check:
	@python3 -B -c 'import ast,pathlib; paths=[p for folder in ("akf","tests","scripts") for p in pathlib.Path(folder).glob("*.py")]; [ast.parse(p.read_text(), filename=str(p)) for p in paths]; print("Syntax checked:", len(paths), "files")'
	@/bin/sh -n Install.command
	@/bin/sh -n scripts/python.sh
	@/bin/sh -n scripts/plugin_hook.sh

test:
	@PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v

package:
	@python3 -B scripts/build_package.py --output "$(OUTPUT)"
