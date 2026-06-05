# SAM Makefile build for both Lambdas.
#
# Without this, `CodeUri: ../` would bundle the entire repo (README.md,
# infrastructure/, scripts/, .git, etc.) into the zip. The Makefile target
# names match the function LogicalIds in infrastructure/template.yaml; SAM
# invokes `make build-<LogicalId>` and expects the built artifact in
# $(ARTIFACTS_DIR).
.PHONY: build-AgendaFunction build-WebUiFunction build-SnoozeFunction build-CoordinatorFunction build-EnrollmentFunction build-DigestFunction

build-DigestFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python3 -m pip install --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet

build-AgendaFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python3 -m pip install --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet

build-WebUiFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python3 -m pip install --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet

build-SnoozeFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python3 -m pip install --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet

build-CoordinatorFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python3 -m pip install --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet

build-EnrollmentFunction:
	@mkdir -p "$(ARTIFACTS_DIR)"
	cp -R src "$(ARTIFACTS_DIR)/"
	python3 -m pip install --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: -r requirements.txt -t "$(ARTIFACTS_DIR)/" --quiet
