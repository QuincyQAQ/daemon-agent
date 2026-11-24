.PHONY: demo test check
ifdef PYTHON
export BEAR_PYTHON := $(PYTHON)
endif

demo:
	./bear.sh demo
test:
	./scripts/python.sh -m unittest discover -s tests -v
check:
	./scripts/check.sh
