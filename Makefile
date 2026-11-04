# Convenience targets mirroring the CI gates.
PYTHON ?= python3
PYTHONPATH := src

.PHONY: install lint test parity surface build

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check src tests

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -q

surface:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m x86_mir_corpus dsl audit \
		--target x86 --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp --strict
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m x86_mir_corpus dsl audit \
		--target aarch64 --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-AArch64LegalizerInfo.cpp --strict

parity:
	X86_MIR_PARITY=1 PYTHONPATH=$(PYTHONPATH) \
		$(PYTHON) -m unittest discover -s tests -p 'test_sidecar_parity.py' -v

build:
	$(PYTHON) -m build --wheel
