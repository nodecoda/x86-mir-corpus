# Convenience targets mirroring the CI gates.
PYTHON ?= python3
PYTHONPATH := src

.PHONY: install lint test parity build

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check src tests

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -q

parity:
	X86_MIR_PARITY=1 PYTHONPATH=$(PYTHONPATH) \
		$(PYTHON) -m unittest discover -s tests -p 'test_sidecar_parity.py' -v

build:
	$(PYTHON) -m build --wheel
