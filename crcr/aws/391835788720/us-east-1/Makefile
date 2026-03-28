SHELL=/bin/bash -o pipefail
PROHOME := $(realpath ../../..)

REGION := $(notdir $(CURDIR))
ACCOUNT := $(notdir $(patsubst %/,%,$(dir $(CURDIR))))

ifneq ($(GITHUB_ACTIONS),true)
export AWS_PROFILE := $(ACCOUNT)
endif

$(PROHOME)/venv/bin/pip:
	cd $(PROHOME)/ && make venv/bin/pip

$(PROHOME)/tf-modules/VERSIONS: $(PROHOME)/venv/bin/pip $(PROHOME)/Terrafile
	cd $(PROHOME)/ && make tf-modules/VERSIONS

dyn_locals.tf:
	echo -e "locals {\n  # tflint-ignore: terraform_unused_declarations\n  aws_region = \"$(REGION)\"\n  # tflint-ignore: terraform_unused_declarations\n  aws_account_id = \"$(ACCOUNT)\"\n}\n" >dyn_locals.tf

backend-state.tf: dyn_locals.tf
	sed "s/#AWS_REGION/$(REGION)/g" <$(PROHOME)/modules/backend-file/backend-state.tf >backend-state.tf
	tofu get -update
	tofu init -backend=false
	tofu plan -input=false -out=backend.plan -detailed-exitcode -target=module.backend-state -lock-timeout=15m ; \
		ext_code=$$? ; \
		if [ $$ext_code -eq 2 ] ; then \
			if [ "$$GITHUB_ACTIONS" != "true" ] ; then \
				echo "Backend state does not exist, creating" ; \
				tofu apply ${TERRAFORM_EXTRAS} backend.plan ; \
			else \
				echo "Backend state does not exist, should not do it on a github action!" ; \
				exit 1 ; \
			fi ; \
		elif [ $$ext_code -eq 0 ] ; then \
			echo "Backend state already exists" ; \
		else \
			echo "Unexpected exit code $$ext_code" ; \
			exit 1 ; \
		fi

backend.tf: backend-state.tf
	sed "s/#AWS_REGION/$(REGION)/g" <$(PROHOME)/modules/backend-file/backend.tf >backend.tf
	$(RM) terraform.tfstate

.terraform/modules/modules.json: $(PROHOME)/tf-modules/VERSIONS backend.tf
	tofu init

.PHONY: all
all:
	echo "Please specify a target"
	exit 1

.PHONY: venv
venv: $(PROHOME)/venv/bin/pip

.PHONY: terrafile
terrafile: $(PROHOME)/tf-modules/VERSIONS

.PHONY: init
init: .terraform/modules/modules.json

.PHONY: backend-state
backend-state: backend.tf

.PHONY: tflint
tflint: .terraform/modules/modules.json
	tflint --init
	tflint --call-module-type=all --color --minimum-failure-severity=warning --recursive

.PHONY: plan
plan: .terraform/modules/modules.json
	tofu plan $(TERRAFORM_EXTRAS)

.PHONY: apply
apply: .terraform/modules/modules.json
	tofu apply ${TERRAFORM_EXTRAS}

.PHONY: destroy
destroy: .terraform/modules/modules.json
	echo "To make sure you want to run this, go to the Makefile and comment out the destroy target" ; exit 1

.PHONY: clean
clean:
	$(RM) -rf .terraform
	$(RM) .terraform.lock.hcl
	$(RM) backend-state.tf
	$(RM) backend.tf
	$(RM) dyn_locals.tf
	$(RM) backend.plan
	$(RM) terraform.tfstate
