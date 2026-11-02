SHELL := /bin/bash
PY := .venv/bin/python
KLAB := .venv/bin/klab
KL_PAGES_URL ?= https://m0-ar.github.io/keylevel-mtf-research

.PHONY: setup download build test run development validation holdout audit freeze live report paper pdf reproduce clean

setup:
	uv venv .venv --python 3.12 --clear
	uv pip install --python .venv/bin/python -r requirements.txt
	uv pip install --python .venv/bin/python -e .

download:
	$(KLAB) download

build:
	$(KLAB) build

test:
	$(PY) -m pytest -q

run development:
	$(KLAB) --config config/base.yaml run --stage development

validation:
	$(KLAB) --config config/base.yaml run --stage validation

holdout:
	$(KLAB) --config config/base.yaml run --stage holdout

audit:
	$(KLAB) --config config/base.yaml audit

freeze:
	$(KLAB) --config config/base.yaml freeze

live:
	$(KLAB) --config config/base.yaml live

report:
	KL_PAGES_URL=$(KL_PAGES_URL) $(KLAB) --config config/base.yaml report

paper: report
	@echo "Paper source is paper/main.tex; RESEARCH.md is the working paper. Run 'make pdf' to compile."

pdf: report
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp \
		-v "$(PWD)/paper:/work" -w /work texlive/texlive:latest \
		latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex

reproduce: setup download build test run validation audit live report
	@echo "Reproduction complete. See results/, README.md, and RESEARCH.md."

clean:
	rm -rf data/processed/* logs/* results/development results/validation results/holdout results/report results/audit paper/figures/* paper/main.pdf README.md RESEARCH.md preview.html index.html
