.PHONY: demo test check
ifdef PYTHON
export FOLDJAY_PYTHON := $(PYTHON)
endif

demo:
	./foldjay.sh demo
test:
	./scripts/python.sh -m unittest discover -s tests -v
check:
	./scripts/check.sh
