# SAM Makefile build for both Lambdas.
#
# Without this, `CodeUri: ../` would bundle the entire repo (README.md,
# infrastructure/, scripts/, .git, etc.) into the zip. The Makefile target
# names match the function LogicalIds in infrastructure/template.yaml; SAM
# invokes `make build-<LogicalId>` and expects the built artifact in
# $(ARTIFACTS_DIR).

.PHONY: build-AgendaFunction build-WebUiFunction

build-AgendaFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet

build-WebUiFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet
